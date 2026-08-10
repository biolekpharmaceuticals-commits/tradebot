from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from src.agent import TradingAgent
from src.backtest import BacktestSettings, run_backtest
from src.bulk_deals import NSEBulkDealProvider
from src.config import load_config
from src.demo_data import demo_candles
from src.derivatives import AngelOneDerivativeDiscovery, DerivativeDiscoveryError
from src.market_data import MarketDataError
from src.news import NewsSignal
from src.risk import RiskDecision
from src.safety import SafetyConfigError
from src.scanner import build_candidate, rank_candidates
from src.strategy import TradeSignal, TrendStrategy


CONFIG = """
trading:
  mode: paper
  require_manual_approval: true
  confidence_threshold: 75
  symbols:
    - {exchange: NSE, symbol: ALPHA-EQ, token: "1", quantity: 1, timeframe: FIVE_MINUTE}
    - {exchange: NSE, symbol: BETA-EQ, token: "2", quantity: 1, timeframe: FIVE_MINUTE}

scanner:
  enabled: true
  max_candidates: 10
  min_average_volume: 100000

risk:
  capital: 100000
  risk_per_trade_pct: 0.5
  max_daily_loss_pct: 2.0
  max_trades_per_day: 5
  stop_after_consecutive_losses: 3
  max_position_value_pct: 10
  avoid_high_impact_news: true

strategy:
  ema_fast: 9
  ema_slow: 21
  ema_trend: 50
  rsi_period: 14
  atr_period: 14
  min_reward_risk: 1.5

news:
  enabled: true
  manual_headlines: []

market_data:
  provider: demo
  lookback_days: 5

bulk_deals:
  enabled: false

logging:
  decision_log: decisions.jsonl
"""


def frame(close: float, volume: int = 200000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-08-10 09:15", periods=20, freq="5min"),
            "open": [close] * 20,
            "high": [close + 1] * 20,
            "low": [close - 1] * 20,
            "close": [close] * 20,
            "volume": [volume] * 20,
        }
    )


def trade_signal(confidence: int, decision: str = "BUY") -> TradeSignal:
    return TradeSignal(decision, confidence, "Bullish", 100, 99, 102, 2, "scanner test")


class SymbolMarketData:
    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        if symbol_config["symbol"] == "ALPHA-EQ":
            return frame(100)
        return frame(200)


class PriceStrategy:
    def evaluate(self, candles, news, bulk_deals=None, instrument_type="equity"):
        return trade_signal(60 if candles.iloc[-1]["close"] == 100 else 90)


@dataclass
class NoOrderBroker:
    calls: int = 0

    def place_order(self, symbol_config, trade_signal, quantity):
        self.calls += 1
        raise AssertionError("Scanner must not bypass paper safety gates")


class ApprovedRisk:
    def evaluate(self, trade_signal, requested_quantity, news):
        return RiskDecision(True, requested_quantity, "Risk checks passed")


def write_config(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_scanner_selects_highest_confidence_candidate_and_logs_ranking(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "true")
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.market_data = SymbolMarketData()
    agent.strategy = PriceStrategy()
    broker = NoOrderBroker()
    agent.broker = broker

    agent.run_once()

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["symbol"] == "BETA-EQ"
    assert record["scanner"]["selected_symbol"] == "BETA-EQ"
    assert [item["symbol"] for item in record["scanner"]["ranking"]] == ["BETA-EQ", "ALPHA-EQ"]
    assert record["executed"] is False
    assert broker.calls == 0


def test_scanner_prefers_eligible_liquid_candidate_over_higher_confidence_illiquid_candidate():
    liquid = build_candidate(
        {"symbol": "LIQUID-EQ"}, frame(100, 200000), trade_signal(70), min_average_volume=100000
    )
    illiquid = build_candidate(
        {"symbol": "ILLIQUID-EQ"}, frame(100, 1000), trade_signal(99), min_average_volume=100000
    )

    ranked = rank_candidates([illiquid, liquid])

    assert ranked[0].symbol_config["symbol"] == "LIQUID-EQ"
    assert ranked[0].eligible is True
    assert ranked[1].eligible is False


def test_scanner_skips_unavailable_symbol_with_sanitized_reason(tmp_path, monkeypatch):
    class PartlyUnavailable(SymbolMarketData):
        def get_candles(self, symbol_config):
            if symbol_config["symbol"] == "ALPHA-EQ":
                raise MarketDataError("SENTINEL_SECRET")
            return super().get_candles(symbol_config)

    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "true")
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.market_data = PartlyUnavailable()
    agent.strategy = PriceStrategy()
    agent.run_once()

    record_text = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert record["scanner"]["failures"] == [
        {"symbol": "ALPHA-EQ", "reason": "Market data unavailable"}
    ]
    assert "SENTINEL" not in record_text
    assert "SECRET" not in record_text


def test_scanner_liquidity_rejection_cannot_reach_paper_broker(tmp_path, monkeypatch):
    unsafe_config = CONFIG.replace("require_manual_approval: true", "require_manual_approval: false").replace(
        "min_average_volume: 100000", "min_average_volume: 500000"
    )
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    agent = TradingAgent(load_config(write_config(tmp_path, unsafe_config)))
    agent.market_data = SymbolMarketData()
    agent.strategy = PriceStrategy()
    agent.risk = ApprovedRisk()
    broker = NoOrderBroker()
    agent.broker = broker

    agent.run_once()

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["scanner"]["selected_eligible"] is False
    assert record["executed"] is False
    assert broker.calls == 0


def test_scanner_config_requires_multiple_symbols_and_bounded_candidate_count(tmp_path):
    one_symbol = CONFIG.replace(
        '    - {exchange: NSE, symbol: BETA-EQ, token: "2", quantity: 1, timeframe: FIVE_MINUTE}\n',
        "",
    )
    with pytest.raises(SafetyConfigError, match="at least two"):
        load_config(write_config(tmp_path, one_symbol))

    invalid_limit = CONFIG.replace("max_candidates: 10", "max_candidates: 1000")
    with pytest.raises(SafetyConfigError, match="between 2 and 25"):
        load_config(write_config(tmp_path, invalid_limit))


def test_bulk_deal_snapshot_is_fetched_once_for_multiple_scanner_symbols():
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return {"as_on_date": "10-Aug-2026", "BULK_DEALS_DATA": []}

    provider = NSEBulkDealProvider(fetcher=fetch)
    provider.get_signal("SBIN-EQ")
    provider.get_signal("RELIANCE-EQ")

    assert calls == 1


def test_nifty_index_uses_no_vwap_or_volume_filter():
    candles = demo_candles()
    candles["volume"] = 0
    strategy = TrendStrategy(
        {
            "ema_fast": 9,
            "ema_slow": 21,
            "ema_trend": 50,
            "rsi_period": 14,
            "atr_period": 14,
            "min_reward_risk": 1.5,
        }
    )

    signal = strategy.evaluate(
        candles,
        NewsSignal(0, "Low", [], "No news"),
        instrument_type="index",
    )
    candidate = build_candidate(
        {"symbol": "NIFTY 50", "instrument_type": "index"},
        candles,
        signal,
        min_average_volume=100000,
    )

    vwap = next(item for item in signal.strategy["factors"] if item["name"] == "VWAP")
    assert vwap["points"] == 0
    assert vwap["direction"] == "Not applicable"
    assert signal.strategy["indicators"]["vwap"] is None
    assert candidate.eligible is True
    assert candidate.average_volume == 0


def test_nifty_index_tokens_fail_closed_when_misconfigured(tmp_path):
    index_config = CONFIG.replace(
        '    - {exchange: NSE, symbol: ALPHA-EQ, token: "1", quantity: 1, timeframe: FIVE_MINUTE}',
        '    - {exchange: NSE, symbol: NIFTY 50, token: "WRONG", quantity: 1, timeframe: FIVE_MINUTE, instrument_type: index}',
    )

    with pytest.raises(SafetyConfigError, match="exchange or token is invalid"):
        load_config(write_config(tmp_path, index_config))


def instrument(symbol, token, expiry, instrument_type, *, strike="-1", lot_size="75", name="NIFTY"):
    return {
        "token": token,
        "symbol": symbol,
        "name": name,
        "expiry": expiry,
        "strike": strike,
        "lotsize": lot_size,
        "instrumenttype": instrument_type,
        "exch_seg": "NFO",
    }


def test_futures_and_directional_long_option_contracts_are_discovered_dynamically():
    master = [
        instrument("NIFTY27AUG26FUT", "101", "27AUG2026", "FUTIDX"),
        instrument("NIFTY13AUG2624550CE", "102", "13AUG2026", "OPTIDX", strike="2455000"),
        instrument("NIFTY13AUG2624600CE", "103", "13AUG2026", "OPTIDX", strike="2460000"),
        instrument("NIFTY13AUG2624600PE", "104", "13AUG2026", "OPTIDX", strike="2460000"),
        instrument("NIFTY06AUG2624600CE", "105", "06AUG2026", "OPTIDX", strike="2460000"),
    ]
    discovery = AngelOneDerivativeDiscovery(
        instruments=["futures", "options"],
        option_strikes=1,
        max_expiry_days=45,
        max_contracts=6,
        timeframe="FIVE_MINUTE",
        timeout_seconds=10,
        fetcher=lambda: master,
        today=lambda: pd.Timestamp("2026-08-10").date(),
    )

    contracts = discovery.contracts_for("NIFTY 50", 24583, "BUY")

    assert [item["symbol"] for item in contracts] == ["NIFTY27AUG26FUT", "NIFTY13AUG2624600CE"]
    assert contracts[0]["required_decision"] == "BUY"
    assert contracts[1]["derivative_type"] == "call"
    assert contracts[1]["strike"] == 24600
    assert contracts[1]["quantity"] == 75
    assert all(item["exchange"] == "NFO" for item in contracts)


def test_bearish_index_signal_discovers_put_buying_not_option_selling():
    master = [
        instrument("BANKNIFTY13AUG2652000CE", "201", "13AUG2026", "OPTIDX", strike="5200000", name="BANKNIFTY"),
        instrument("BANKNIFTY13AUG2652000PE", "202", "13AUG2026", "OPTIDX", strike="5200000", name="BANKNIFTY"),
    ]
    discovery = AngelOneDerivativeDiscovery(
        instruments=["futures", "options"],
        option_strikes=1,
        max_expiry_days=45,
        max_contracts=6,
        timeframe="FIVE_MINUTE",
        timeout_seconds=10,
        fetcher=lambda: master,
        today=lambda: pd.Timestamp("2026-08-10").date(),
    )

    contracts = discovery.contracts_for("NIFTY BANK", 52010, "SELL")

    assert len(contracts) == 1
    assert contracts[0]["derivative_type"] == "put"
    assert contracts[0]["required_decision"] == "BUY"
    assert contracts[0]["symbol"].endswith("PE")


def test_derivative_discovery_error_is_sanitized():
    def fail():
        raise RuntimeError("SENTINEL_DERIVATIVE_SECRET")

    discovery = AngelOneDerivativeDiscovery(
        instruments=["futures", "options"],
        option_strikes=1,
        max_expiry_days=45,
        max_contracts=6,
        timeframe="FIVE_MINUTE",
        timeout_seconds=10,
        fetcher=fail,
    )

    with pytest.raises(DerivativeDiscoveryError) as exc_info:
        discovery.contracts_for("NIFTY 50", 24583, "BUY")
    assert "SENTINEL" not in str(exc_info.value)
    assert "SECRET" not in str(exc_info.value)


def test_option_selling_configuration_fails_closed(tmp_path):
    unsafe = CONFIG + "\nderivatives:\n  enabled: true\n  instruments: [futures, options]\n  option_buying_only: false\n"
    with pytest.raises(SafetyConfigError, match="option_buying_only must remain true"):
        load_config(write_config(tmp_path, unsafe))


def test_derivative_discovery_source_is_read_only():
    source = (Path(__file__).resolve().parents[1] / "src" / "derivatives.py").read_text(encoding="utf-8")
    assert ".post(" not in source
    assert "placeOrder" not in source


def test_agent_scanner_selects_only_discovered_nfo_contracts(tmp_path, monkeypatch):
    index_config = CONFIG.replace(
        '    - {exchange: NSE, symbol: ALPHA-EQ, token: "1", quantity: 1, timeframe: FIVE_MINUTE}',
        '    - {exchange: NSE, symbol: NIFTY 50, token: "99926000", quantity: 1, timeframe: FIVE_MINUTE, instrument_type: index}',
    ).replace(
        '    - {exchange: NSE, symbol: BETA-EQ, token: "2", quantity: 1, timeframe: FIVE_MINUTE}',
        '    - {exchange: NSE, symbol: NIFTY BANK, token: "99926009", quantity: 1, timeframe: FIVE_MINUTE, instrument_type: index}',
    )
    index_config += """
derivatives:
  enabled: true
  instruments: [futures, options]
  option_buying_only: true
  option_strikes: 1
  max_expiry_days: 45
  max_contracts: 6
  timeframe: FIVE_MINUTE
  timeout_seconds: 10
"""

    class FakeDiscovery:
        enabled = True

        def contracts_for(self, underlying, spot_price, direction):
            name = "NIFTY" if underlying == "NIFTY 50" else "BANKNIFTY"
            return [
                {
                    "exchange": "NFO",
                    "symbol": f"{name}27AUG26FUT",
                    "token": "500",
                    "quantity": 75,
                    "timeframe": "FIVE_MINUTE",
                    "instrument_type": "derivative",
                    "derivative_type": "future",
                    "underlying": name,
                    "expiry": "2026-08-27",
                    "strike": None,
                    "lot_size": 75,
                    "required_decision": direction,
                }
            ]

    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "true")
    agent = TradingAgent(load_config(write_config(tmp_path, index_config)))
    agent.market_data = SymbolMarketData()
    agent.strategy = PriceStrategy()
    agent.derivatives = FakeDiscovery()

    agent.run_once()

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["symbol"].endswith("FUT")
    assert record["scanner"]["mode"] == "futures_and_long_options"
    assert all(item["instrument_type"] == "derivative" for item in record["scanner"]["ranking"])
    assert record["executed"] is False


class BacktestBuyStrategy:
    def __init__(self):
        self.history_lengths = []
        self.news_scores = []
        self.bulk_scores = []

    def evaluate(self, candles, news, bulk_deals=None, instrument_type="equity"):
        self.history_lengths.append(len(candles))
        self.news_scores.append(news.score)
        self.bulk_scores.append(bulk_deals.score)
        entry = float(candles.iloc[-1]["close"])
        return TradeSignal("BUY", 90, "Bullish", entry, entry - 1, entry + 1, 1, "backtest")


def backtest_candles(*, both_hit: bool = False) -> pd.DataFrame:
    rows = 90
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-08-01 09:15", periods=rows, freq="5min"),
            "open": [100.0] * rows,
            "high": [102.0] * rows,
            "low": ([98.0] * rows) if both_hit else ([100.0] * rows),
            "close": [100.0] * rows,
            "volume": [200000] * rows,
        }
    )


def test_walk_forward_backtest_uses_only_prior_candles_and_neutral_historical_context():
    strategy = BacktestBuyStrategy()
    settings = BacktestSettings(True, 60, 5, 10)

    result = run_backtest(
        backtest_candles(),
        strategy,
        {"instrument_type": "derivative", "required_decision": "BUY"},
        settings,
    )

    assert result["status"] == "complete"
    assert result["trades"] > 0
    assert result["win_rate_pct"] == 100
    assert strategy.history_lengths[0] == 60
    assert max(strategy.history_lengths) < len(backtest_candles())
    assert set(strategy.news_scores) == {0}
    assert set(strategy.bulk_scores) == {0}


def test_backtest_resolves_same_bar_stop_and_target_conservatively_as_stop():
    result = run_backtest(
        backtest_candles(both_hit=True),
        BacktestBuyStrategy(),
        {"instrument_type": "derivative", "required_decision": "BUY"},
        BacktestSettings(True, 60, 5, 0),
    )

    assert result["recent_trades"]
    assert all(item["outcome"] == "stop" for item in result["recent_trades"])
    assert result["net_return_pct"] < 0


def test_backtest_uses_worse_open_price_when_market_gaps_beyond_stop():
    candles = backtest_candles()
    candles.loc[60:, "open"] = 95.0
    candles.loc[60:, "low"] = 94.0
    result = run_backtest(
        candles,
        BacktestBuyStrategy(),
        {"instrument_type": "derivative", "required_decision": "BUY"},
        BacktestSettings(True, 60, 5, 0),
    )

    assert result["recent_trades"][0]["outcome"] == "stop_gap"
    assert result["recent_trades"][0]["exit_price"] == 95.0


def test_backtest_config_is_bounded_and_fails_closed(tmp_path):
    invalid = CONFIG + "\nbacktesting:\n  enabled: true\n  minimum_candles: 5\n"
    with pytest.raises(SafetyConfigError, match="minimum_candles"):
        load_config(write_config(tmp_path, invalid))
