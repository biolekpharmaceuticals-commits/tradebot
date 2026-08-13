from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.agent import TradingAgent
from src.config import load_config
from src.safety import SafetyConfigError
from src.scanner import build_candidate, rank_candidates
from src.strategy import TradeSignal


RISK = {
    "capital": 100000,
    "risk_per_trade_pct": 0.5,
    "max_position_value_pct": 10,
}


def frame(price: float, volume: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-08-11 09:15", periods=20, freq="5min"),
            "open": [price] * 20,
            "high": [price + 1] * 20,
            "low": [price - 1] * 20,
            "close": [price] * 20,
            "volume": [volume] * 20,
        }
    )


def signal(confidence: int, entry: float, stop: float) -> TradeSignal:
    return TradeSignal("BUY", confidence, "Bullish", entry, stop, entry + 2, 2, "5.1 test")


def derivative(symbol: str, derivative_type: str, lot_size: int) -> dict:
    return {
        "exchange": "NFO",
        "symbol": symbol,
        "token": "500",
        "quantity": lot_size,
        "timeframe": "FIVE_MINUTE",
        "instrument_type": "derivative",
        "derivative_type": derivative_type,
        "underlying": "NIFTY",
        "expiry": "2026-08-27",
        "lot_size": lot_size,
        "required_decision": "BUY",
    }


def test_fno_volume_rule_replaces_equity_threshold_and_unaffordable_future_is_ineligible():
    candidate = build_candidate(
        derivative("BANKNIFTY25AUG26FUT", "future", 30),
        frame(58032, 252),
        signal(70, 58032, 57952.21),
        min_average_volume=100000,
        min_derivative_average_volume=100,
        risk_config=RISK,
    )

    assert "volume" not in candidate.reason.lower()
    assert candidate.eligible is False
    assert "Full F&O lot value exceeds position limit" in candidate.reason
    assert "Full F&O lot risk exceeds per-trade limit" in candidate.reason
    assert candidate.contract_value == 1740960
    assert candidate.lot_risk == pytest.approx(2393.7)


def test_affordable_long_option_is_prioritized_over_higher_confidence_future():
    future = build_candidate(
        derivative("NIFTY25AUG26FUT", "future", 10),
        frame(100, 500),
        signal(90, 100, 99),
        min_average_volume=100000,
        min_derivative_average_volume=100,
        risk_config=RISK,
    )
    option = build_candidate(
        derivative("NIFTY25AUG26100CE", "call", 10),
        frame(100, 500),
        signal(55, 100, 99),
        min_average_volume=100000,
        min_derivative_average_volume=100,
        risk_config=RISK,
    )

    ranked = rank_candidates([future, option])

    assert future.eligible is True
    assert option.eligible is True
    assert ranked[0].symbol_config["derivative_type"] == "call"


CONFIG = """
trading:
  mode: paper
  require_manual_approval: false
  confidence_threshold: 75
  symbols:
    - {exchange: NSE, symbol: NIFTY 50, token: "99926000", quantity: 1, timeframe: FIVE_MINUTE, instrument_type: index}
    - {exchange: NSE, symbol: NIFTY BANK, token: "99926009", quantity: 1, timeframe: FIVE_MINUTE, instrument_type: index}

scanner:
  enabled: true
  max_candidates: 20
  min_average_volume: 100000

derivatives:
  enabled: true
  instruments: [futures, options]
  option_buying_only: true
  prefer_long_options: true
  min_average_volume: 100
  confidence_threshold: 55
  option_strikes: 1
  max_expiry_days: 45
  max_contracts: 6
  timeframe: FIVE_MINUTE
  timeout_seconds: 10

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

paper_execution:
  enabled: true
  initial_balance: 100000
  max_open_positions: 2
  slippage_bps: 5
  fee_bps: 10
  max_holding_minutes: 120
  market_hours_only: false
  max_candle_age_minutes: 10
  state_file: portfolio.json

logging:
  decision_log: decisions.jsonl
"""


class FixedMarketData:
    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        return frame(100, 200)


class FixedStrategy:
    def evaluate(self, candles, news, bulk_deals=None, instrument_type="equity"):
        return signal(60, 100, 99)


class OptionOnlyDiscovery:
    enabled = True

    def contracts_for(self, underlying, spot_price, direction):
        if underlying != "NIFTY 50":
            return []
        return [derivative("NIFTY25AUG26100CE", "call", 75)]


def write_config(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_fno_threshold_allows_affordable_long_option_paper_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "true")
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.market_data = FixedMarketData()
    agent.strategy = FixedStrategy()
    agent.derivatives = OptionOnlyDiscovery()

    agent.run_once()

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["symbol"].endswith("CE")
    assert record["confidence_threshold"] == 55
    assert record["scanner"]["minimum_derivative_average_volume"] == 100
    assert record["scanner"]["selected_eligible"] is True
    assert record["executed"] is True
    assert record["order"]["quantity"] == 75
    assert record["live_trading_enabled"] is False


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("prefer_long_options: true", "prefer_long_options: false", "prefer_long_options"),
        ("confidence_threshold: 55", "confidence_threshold: 49", "confidence_threshold"),
        ("min_average_volume: 100\n  confidence", "min_average_volume: -1\n  confidence", "min_average_volume"),
    ],
)
def test_unsafe_fno_execution_settings_fail_closed(tmp_path, old, new, message):
    with pytest.raises(SafetyConfigError, match=message):
        load_config(write_config(tmp_path, CONFIG.replace(old, new)))
