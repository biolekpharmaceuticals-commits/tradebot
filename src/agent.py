from __future__ import annotations

import json
from dataclasses import asdict

import pandas as pd

from .broker import PaperBroker
from .bulk_deals import build_bulk_deal_provider
from .config import AppConfig
from .logger import DecisionLogger
from .market_data import build_market_data_provider
from .news import NewsAnalyzer
from .risk import RiskManager
from .strategy import TrendStrategy


class TradingAgent:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        trading = config.section("trading")
        self.broker = PaperBroker()
        self.market_data = build_market_data_provider(config.section("market_data"))
        self.bulk_deals = build_bulk_deal_provider(config.raw.get("bulk_deals", {}))
        self.strategy = TrendStrategy(config.section("strategy"))
        self.news_analyzer = NewsAnalyzer()
        self.risk = RiskManager(config.section("risk"))

        log_path = config.base_dir / config.section("logging").get("decision_log", "logs/decisions.jsonl")
        self.logger = DecisionLogger(log_path)

    def run_once(self) -> None:
        symbols = self.config.section("trading").get("symbols", [])
        if not symbols:
            raise ValueError("No symbols configured")
        for symbol_config in symbols:
            candles = self.market_data.get_candles(symbol_config)
            self._evaluate_symbol(symbol_config, candles)

    def run_once_with_candles(self, candles: pd.DataFrame) -> None:
        symbols = self.config.section("trading").get("symbols", [])
        symbol_config = symbols[0] if symbols else {"symbol": "DEMO", "quantity": 1}
        self._evaluate_symbol(symbol_config, candles)

    def _evaluate_symbol(self, symbol_config: dict, candles: pd.DataFrame) -> None:
        trading = self.config.section("trading")
        headlines = self.config.section("news").get("manual_headlines", [])
        news = self.news_analyzer.analyze(list(headlines))
        bulk_deals = self.bulk_deals.get_signal(str(symbol_config.get("symbol", "")))
        signal = self.strategy.evaluate(candles, news, bulk_deals)

        threshold = int(trading.get("confidence_threshold", 75))
        requested_quantity = int(symbol_config.get("quantity", 1))
        risk_decision = self.risk.evaluate(signal, requested_quantity, news)

        should_execute = (
            not self.config.safety.kill_switch_active
            and signal.confidence >= threshold
            and risk_decision.approved
            and not trading.get("require_manual_approval", True)
        )

        order = None
        if should_execute:
            order = self.broker.place_order(symbol_config, signal, risk_decision.quantity)

        signal_payload = asdict(signal)
        strategy_payload = signal_payload.get("strategy")
        if isinstance(strategy_payload, dict):
            strategy_payload["timeframe"] = symbol_config.get("timeframe")
            strategy_payload["candle_count"] = len(candles) if candles is not None else 0
            if candles is not None and "timestamp" in candles.columns and not candles.empty:
                strategy_payload["latest_candle"] = str(candles.iloc[-1]["timestamp"])

        payload = {
            "symbol": symbol_config.get("symbol"),
            "mode": self.config.safety.trading_mode,
            "signal": signal_payload,
            "news": asdict(news),
            "bulk_deals": asdict(bulk_deals),
            "risk": asdict(risk_decision),
            "manual_approval_required": trading.get("require_manual_approval", True),
            "confidence_threshold": threshold,
            "kill_switch_active": self.config.safety.kill_switch_active,
            "live_trading_enabled": self.config.safety.live_trading_enabled,
            "executed": bool(order),
            "order": asdict(order) if order else None,
        }
        self.logger.write(payload)
        print(json.dumps(payload, indent=2, default=str))
