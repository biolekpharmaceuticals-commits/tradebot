from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .broker import PaperBroker, load_paper_execution_settings
from .backtest import load_backtest_settings, run_backtest
from .bulk_deals import build_bulk_deal_provider
from .config import AppConfig
from .derivatives import DerivativeDiscoveryError, build_derivative_discovery
from .logger import DecisionLogger
from .market_data import MarketDataError, build_market_data_provider
from .news import NewsAnalyzer
from .risk import RiskDecision, RiskManager
from .scanner import build_candidate, rank_candidates
from .strategy import TrendStrategy


class TradingAgent:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        trading = config.section("trading")
        log_path = config.base_dir / config.section("logging").get("decision_log", "logs/decisions.jsonl")
        self.paper_settings = load_paper_execution_settings(config.raw.get("paper_execution", {}))
        configured_state = self.paper_settings.state_file
        state_path = Path(configured_state) if configured_state else log_path.with_name("paper_portfolio.json")
        if not state_path.is_absolute():
            state_path = config.base_dir / state_path
        self.paper_broker = PaperBroker(
            state_path=state_path,
            initial_balance=self.paper_settings.initial_balance,
            max_open_positions=self.paper_settings.max_open_positions,
            slippage_bps=self.paper_settings.slippage_bps,
            fee_bps=self.paper_settings.fee_bps,
            max_holding_minutes=self.paper_settings.max_holding_minutes,
            market_hours_only=self.paper_settings.market_hours_only,
            max_candle_age_minutes=self.paper_settings.max_candle_age_minutes,
        )
        self.broker = self.paper_broker
        self.market_data = build_market_data_provider(config.section("market_data"))
        self.bulk_deals = build_bulk_deal_provider(config.raw.get("bulk_deals", {}))
        self.derivatives = build_derivative_discovery(config.raw.get("derivatives", {}))
        self.strategy = TrendStrategy(config.section("strategy"))
        self.news_analyzer = NewsAnalyzer()
        self.risk = RiskManager(config.section("risk"))
        self.backtest_settings = load_backtest_settings(config.raw.get("backtesting", {}))

        self.logger = DecisionLogger(log_path)
        self.reconciliation_orders = []

    def run_once(self) -> None:
        self.reconciliation_orders = self._reconcile_paper_positions()
        symbols = self.config.section("trading").get("symbols", [])
        if not symbols:
            raise ValueError("No symbols configured")

        scanner = self.config.raw.get("scanner", {})
        if scanner.get("enabled", False):
            self._run_scanner(symbols, scanner)
            return

        for symbol_config in symbols:
            candles = self.market_data.get_candles(symbol_config)
            self._evaluate_symbol(symbol_config, candles)

    def _run_scanner(self, symbols: list[dict], scanner_config: dict) -> None:
        headlines = self.config.section("news").get("manual_headlines", [])
        news = self.news_analyzer.analyze(list(headlines))
        min_average_volume = int(scanner_config.get("min_average_volume", 100000))
        derivative_config = self.config.raw.get("derivatives") or {}
        min_derivative_average_volume = int(derivative_config.get("min_average_volume", 100))
        max_candidates = int(scanner_config.get("max_candidates", 20))
        candidates = []
        failures: list[dict[str, str]] = []

        for symbol_config in symbols[:max_candidates]:
            symbol = str(symbol_config.get("symbol", "UNKNOWN"))
            try:
                candles = self.market_data.get_candles(symbol_config)
                bulk_deals = self.bulk_deals.get_signal(symbol)
                signal = self.strategy.evaluate(
                    candles,
                    news,
                    bulk_deals,
                    instrument_type=str(symbol_config.get("instrument_type", "equity")),
                )
                candidate = build_candidate(
                    symbol_config,
                    candles,
                    signal,
                    min_average_volume=min_average_volume,
                )
                candidates.append((candidate, bulk_deals))
            except MarketDataError:
                failures.append({"symbol": symbol, "reason": "Market data unavailable"})

        if not candidates:
            raise MarketDataError("Scanner could not evaluate any configured symbols")

        underlying_summaries = [
            candidate.public_summary(index)
            for index, (candidate, _) in enumerate(candidates, 1)
            if str(candidate.symbol_config.get("instrument_type", "equity")).lower() == "index"
        ]
        scanner_mode = "cash_and_indices"
        if self.derivatives.enabled:
            derivative_candidates = []
            discovered_contracts: list[dict] = []
            for candidate, _ in candidates:
                if str(candidate.symbol_config.get("instrument_type", "equity")).lower() != "index":
                    continue
                try:
                    discovered_contracts.extend(
                        self.derivatives.contracts_for(
                            str(candidate.symbol_config.get("symbol", "")),
                            candidate.signal.entry_price,
                            candidate.signal.decision,
                        )
                    )
                except DerivativeDiscoveryError:
                    failures.append(
                        {
                            "symbol": str(candidate.symbol_config.get("symbol", "UNKNOWN")),
                            "reason": "F&O contract discovery unavailable",
                        }
                    )

            for contract in discovered_contracts[:max_candidates]:
                symbol = str(contract.get("symbol", "UNKNOWN"))
                try:
                    candles = self.market_data.get_candles(contract)
                    bulk_deals = self.bulk_deals.get_signal(str(contract.get("underlying", "")))
                    signal = self.strategy.evaluate(candles, news, bulk_deals, instrument_type="derivative")
                    derivative_candidates.append(
                        (
                            build_candidate(
                                contract,
                                candles,
                                signal,
                                min_average_volume=min_average_volume,
                                min_derivative_average_volume=min_derivative_average_volume,
                                risk_config=self.config.section("risk"),
                            ),
                            bulk_deals,
                        )
                    )
                except MarketDataError:
                    failures.append({"symbol": symbol, "reason": "Market data unavailable"})

            if not derivative_candidates:
                raise MarketDataError("Scanner could not evaluate any current F&O contracts")
            candidates = derivative_candidates
            scanner_mode = "futures_and_long_options"

        ranked = rank_candidates([item[0] for item in candidates])
        selected = ranked[0]
        selected_bulk = next(bulk for candidate, bulk in candidates if candidate is selected)
        scanner_payload = {
            "enabled": True,
            "mode": scanner_mode,
            "universe_size": min(len(symbols), max_candidates),
            "evaluated": len(ranked),
            "selected_symbol": selected.symbol_config.get("symbol"),
            "selected_eligible": selected.eligible,
            "selection_reason": (
                "Long options first, then confidence and recent turnover among eligible candidates"
                if self.derivatives.enabled
                else "Highest confidence, then highest recent average turnover among eligible candidates"
            ),
            "minimum_average_volume": min_average_volume,
            "minimum_derivative_average_volume": min_derivative_average_volume,
            "derivative_confidence_threshold": int(
                derivative_config.get("confidence_threshold", 55)
            ),
            "underlying_signals": underlying_summaries,
            "ranking": [candidate.public_summary(index) for index, candidate in enumerate(ranked, 1)],
            "failures": failures,
        }
        self._finalize_symbol(
            selected.symbol_config,
            selected.candles,
            news,
            selected_bulk,
            selected.signal,
            scanner_payload=scanner_payload,
            execution_eligible=selected.eligible,
        )

    def run_once_with_candles(self, candles: pd.DataFrame) -> None:
        self.reconciliation_orders = []
        symbols = self.config.section("trading").get("symbols", [])
        symbol_config = symbols[0] if symbols else {"symbol": "DEMO", "quantity": 1}
        self._evaluate_symbol(symbol_config, candles)

    def _evaluate_symbol(self, symbol_config: dict, candles: pd.DataFrame) -> None:
        headlines = self.config.section("news").get("manual_headlines", [])
        news = self.news_analyzer.analyze(list(headlines))
        bulk_deals = self.bulk_deals.get_signal(str(symbol_config.get("symbol", "")))
        signal = self.strategy.evaluate(
            candles,
            news,
            bulk_deals,
            instrument_type=str(symbol_config.get("instrument_type", "equity")),
        )

        self._finalize_symbol(symbol_config, candles, news, bulk_deals, signal)

    def _finalize_symbol(
        self,
        symbol_config: dict,
        candles: pd.DataFrame,
        news,
        bulk_deals,
        signal,
        *,
        scanner_payload: dict | None = None,
        execution_eligible: bool = True,
    ) -> None:
        trading = self.config.section("trading")

        is_derivative = str(symbol_config.get("instrument_type", "")).lower() == "derivative"
        threshold = (
            int((self.config.raw.get("derivatives") or {}).get("confidence_threshold", 55))
            if is_derivative
            else int(trading.get("confidence_threshold", 75))
        )
        requested_quantity = int(symbol_config.get("quantity", 1))
        if isinstance(self.risk, RiskManager):
            self.risk.update_state(**self.paper_broker.risk_snapshot())
        risk_decision = self.risk.evaluate(signal, requested_quantity, news)
        if (
            is_derivative
            and risk_decision.approved
            and risk_decision.quantity != requested_quantity
        ):
            risk_decision = RiskDecision(False, 0, "Full F&O lot exceeds risk limits")

        should_execute = (
            execution_eligible
            and self.paper_settings.enabled
            and self.config.safety.auto_paper_trading_enabled
            and not self.config.safety.kill_switch_active
            and signal.confidence >= threshold
            and risk_decision.approved
            and not trading.get("require_manual_approval", True)
        )

        order = None
        if should_execute:
            execution_config = dict(symbol_config)
            candle_timestamp = None
            if candles is not None and not candles.empty and "timestamp" in candles:
                candle_timestamp = str(candles.iloc[-1]["timestamp"])
            execution_config["paper_candle_timestamp"] = candle_timestamp
            execution_config["paper_decision_id"] = (
                f"{symbol_config.get('symbol')}|{candle_timestamp}|{signal.decision}"
            )
            order = self.broker.place_order(execution_config, signal, risk_decision.quantity)

        signal_payload = asdict(signal)
        backtest_payload = run_backtest(
            candles,
            self.strategy,
            symbol_config,
            self.backtest_settings,
        )
        strategy_payload = signal_payload.get("strategy")
        if isinstance(strategy_payload, dict):
            strategy_payload["timeframe"] = symbol_config.get("timeframe")
            strategy_payload["candle_count"] = len(candles) if candles is not None else 0
            if candles is not None and "timestamp" in candles.columns and not candles.empty:
                strategy_payload["latest_candle"] = str(candles.iloc[-1]["timestamp"])

        blockers = self._execution_blockers(
            execution_eligible=execution_eligible,
            confidence=signal.confidence,
            threshold=threshold,
            risk_decision=risk_decision,
            manual_approval=trading.get("require_manual_approval", True),
        )
        payload = {
            "symbol": symbol_config.get("symbol"),
            "mode": self.config.safety.trading_mode,
            "signal": signal_payload,
            "news": asdict(news),
            "bulk_deals": asdict(bulk_deals),
            "scanner": scanner_payload or {"enabled": False},
            "backtest": backtest_payload,
            "risk": asdict(risk_decision),
            "manual_approval_required": trading.get("require_manual_approval", True),
            "confidence_threshold": threshold,
            "kill_switch_active": self.config.safety.kill_switch_active,
            "live_trading_enabled": self.config.safety.live_trading_enabled,
            "auto_paper_trading_enabled": self.config.safety.auto_paper_trading_enabled,
            "paper_execution_configured": self.paper_settings.enabled,
            "execution_blockers": blockers,
            "executed": bool(order and order.accepted),
            "order": asdict(order) if order else None,
            "paper_portfolio": self.paper_broker.snapshot(),
            "paper_exit_orders": [asdict(item) for item in self.reconciliation_orders],
        }
        self.logger.write(payload)
        print(json.dumps(payload, indent=2, default=str))

    def _reconcile_paper_positions(self) -> list:
        if not self.paper_settings.enabled:
            return []
        exit_orders = []
        for symbol_config in self.paper_broker.open_position_configs():
            try:
                candles = self.market_data.get_candles(symbol_config)
            except MarketDataError:
                continue
            exit_orders.extend(self.paper_broker.reconcile(symbol_config, candles))
        return exit_orders

    def _execution_blockers(
        self,
        *,
        execution_eligible: bool,
        confidence: int,
        threshold: int,
        risk_decision,
        manual_approval: bool,
    ) -> list[str]:
        blockers = []
        if not execution_eligible:
            blockers.append("Scanner candidate is ineligible")
        if not self.paper_settings.enabled:
            blockers.append("Paper execution is not configured")
        if not self.config.safety.auto_paper_trading_enabled:
            blockers.append("AUTO_PAPER_TRADING_ENABLED is false")
        if self.config.safety.kill_switch_active:
            blockers.append("Kill switch is active")
        if confidence < threshold:
            blockers.append("Confidence is below threshold")
        if not risk_decision.approved:
            blockers.append(risk_decision.reason)
        if manual_approval:
            blockers.append("Manual approval is required")
        return blockers
