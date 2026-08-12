from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.agent import TradingAgent
from src.broker import PaperBroker
from src.config import load_config
from src.dashboard import create_app
from src.news import NewsSignal
from src.risk import RiskDecision
from src.safety import SafetyConfigError
from src.strategy import TradeSignal


CONFIG = """
trading:
  mode: paper
  require_manual_approval: false
  confidence_threshold: 75
  symbols:
    - exchange: NSE
      symbol: TEST-EQ
      token: "1"
      quantity: 1
      timeframe: FIVE_MINUTE
      instrument_type: equity

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

paper_execution:
  enabled: true
  initial_balance: 100000
  max_open_positions: 2
  slippage_bps: 5
  fee_bps: 10
  max_holding_minutes: 120
  state_file: paper-portfolio.json

logging:
  decision_log: decisions.jsonl
"""


def write_config(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def signal(decision: str = "BUY", confidence: int = 90, entry: float = 100.0) -> TradeSignal:
    if decision == "BUY":
        stop, target, bias = entry - 1, entry + 2, "Bullish"
    else:
        stop, target, bias = entry + 1, entry - 2, "Bearish"
    return TradeSignal(decision, confidence, bias, entry, stop, target, 2.0, "release 5 test")


def candles(*, low: float = 100, high: float = 101, close: float = 100) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-08-11 09:15", periods=3, freq="5min", tz="Asia/Kolkata"),
            "open": [100, 100, 100],
            "high": [101, high, high],
            "low": [99.5, low, low],
            "close": [100, close, close],
            "volume": [200000, 200000, 200000],
        }
    )


class FixedStrategy:
    def __init__(self, value: TradeSignal) -> None:
        self.value = value

    def evaluate(self, candles, news, bulk_deals=None, instrument_type="equity"):
        return self.value


class FixedNews:
    def analyze(self, headlines):
        return NewsSignal(0, "Low", [], "No high-impact news")


@dataclass
class FixedRisk:
    value: RiskDecision

    def evaluate(self, trade_signal, requested_quantity, news):
        return self.value


def test_auto_paper_requires_explicit_environment_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    monkeypatch.delenv("AUTO_PAPER_TRADING_ENABLED", raising=False)
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.strategy = FixedStrategy(signal())
    agent.news_analyzer = FixedNews()
    agent.risk = FixedRisk(RiskDecision(True, 1, "Risk checks passed"))
    agent.paper_broker.clock = lambda: datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)

    agent.run_once_with_candles(candles())

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["executed"] is False
    assert record["order"] is None
    assert "AUTO_PAPER_TRADING_ENABLED is false" in record["execution_blockers"]


def test_auto_paper_fills_only_after_all_gates_approve(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.strategy = FixedStrategy(signal())
    agent.news_analyzer = FixedNews()
    agent.risk = FixedRisk(RiskDecision(True, 1, "Risk checks passed"))
    agent.paper_broker.clock = lambda: datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)

    agent.run_once_with_candles(candles())

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["executed"] is True
    assert record["order"]["order_id"].startswith("PAPER-")
    assert record["order"]["status"] == "FILLED"
    assert record["live_trading_enabled"] is False
    assert record["paper_portfolio"]["open_position_count"] == 1
    assert (tmp_path / "paper-portfolio.json").exists()


def test_kill_switch_still_blocks_auto_paper(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "true")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "true")
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.strategy = FixedStrategy(signal())
    agent.news_analyzer = FixedNews()
    agent.risk = FixedRisk(RiskDecision(True, 1, "Risk checks passed"))

    agent.run_once_with_candles(candles())

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["executed"] is False
    assert "Kill switch is active" in record["execution_blockers"]


def test_paper_broker_blocks_duplicate_decision_and_same_symbol_position(tmp_path):
    broker = PaperBroker(state_path=tmp_path / "state.json", id_factory=lambda: "ENTRY1")
    config = {
        "symbol": "NIFTY26AUGFUT",
        "paper_decision_id": "decision-1",
        "paper_candle_timestamp": "2026-08-11T09:15:00+05:30",
    }

    first = broker.place_order(config, signal(), 75)
    duplicate = broker.place_order(config, signal(), 75)
    second_decision = broker.place_order({**config, "paper_decision_id": "decision-2"}, signal(), 75)

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.message == "Duplicate paper decision blocked"
    assert second_decision.accepted is False
    assert second_decision.message == "Paper position already open"


def test_option_selling_is_rejected_inside_paper_broker(tmp_path):
    broker = PaperBroker(state_path=tmp_path / "state.json")
    result = broker.place_order(
        {"symbol": "NIFTYCE", "derivative_type": "call", "paper_decision_id": "one"},
        signal("SELL"),
        75,
    )

    assert result.accepted is False
    assert result.message == "Option selling is prohibited"
    assert not (tmp_path / "state.json").exists()


def test_stop_is_resolved_before_target_and_loss_persists(tmp_path):
    ids = iter(["ENTRY", "EXIT"])
    broker = PaperBroker(
        state_path=tmp_path / "state.json",
        slippage_bps=0,
        fee_bps=0,
        clock=lambda: datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc),
        id_factory=lambda: next(ids),
    )
    config = {
        "exchange": "NFO",
        "symbol": "NIFTYCE",
        "token": "1",
        "timeframe": "FIVE_MINUTE",
        "derivative_type": "call",
        "required_decision": "BUY",
        "paper_decision_id": "one",
        "paper_candle_timestamp": "2026-08-11T09:15:00+05:30",
    }
    broker.place_order(config, signal("BUY"), 75)

    exits = broker.reconcile(config, candles(low=98, high=103))
    snapshot = broker.snapshot()
    risk = broker.risk_snapshot()

    assert len(exits) == 1
    assert exits[0].message == "Paper position closed: stop"
    assert snapshot["open_position_count"] == 0
    assert snapshot["realized_pnl"] == -75
    assert risk["trades_today"] == 1
    assert risk["realized_pnl"] == -75
    assert risk["consecutive_losses"] == 1


def test_paper_state_is_private_and_survives_new_broker_instance(tmp_path):
    path = tmp_path / "state.json"
    first = PaperBroker(state_path=path, id_factory=lambda: "ONE")
    first.place_order(
        {"symbol": "SBIN-EQ", "paper_decision_id": "one", "paper_candle_timestamp": "2026-08-11"},
        signal(),
        1,
    )
    second = PaperBroker(state_path=path)

    assert second.snapshot()["open_position_count"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_paper_execution_config_fails_closed(tmp_path):
    invalid = CONFIG.replace("max_open_positions: 2", "max_open_positions: 99")
    with pytest.raises(SafetyConfigError, match="max_open_positions"):
        load_config(write_config(tmp_path, invalid))


def test_stale_candle_is_rejected_for_unattended_paper_entry(tmp_path):
    broker = PaperBroker(
        state_path=tmp_path / "state.json",
        market_hours_only=True,
        max_candle_age_minutes=10,
        clock=lambda: datetime(2026, 8, 11, 4, 30, tzinfo=timezone.utc),
    )
    result = broker.place_order(
        {
            "symbol": "NIFTYFUT",
            "paper_decision_id": "stale",
            "paper_candle_timestamp": "2026-08-11T09:15:00+05:30",
        },
        signal(),
        75,
    )

    assert result.accepted is False
    assert result.message == "Latest candle is stale for paper execution"


def test_auto_paper_environment_must_be_boolean(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "maybe")
    with pytest.raises(SafetyConfigError, match="AUTO_PAPER_TRADING_ENABLED"):
        load_config(write_config(tmp_path))


def test_persistent_daily_trade_limit_blocks_new_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "true")
    timestamp = "2026-08-11T04:00:00+00:00"
    state = {
        "version": 1,
        "initial_balance": 100000,
        "realized_pnl": 0,
        "open_positions": [],
        "closed_positions": [],
        "orders": [
            {
                "order_id": f"PAPER-{index}",
                "kind": "ENTRY",
                "status": "FILLED",
                "symbol": f"TEST-{index}",
                "timestamp": timestamp,
                "decision_id": f"decision-{index}",
            }
            for index in range(5)
        ],
    }
    (tmp_path / "paper-portfolio.json").write_text(json.dumps(state), encoding="utf-8")
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.paper_broker.clock = lambda: datetime(2026, 8, 11, 4, 5, tzinfo=timezone.utc)
    agent.strategy = FixedStrategy(signal())
    agent.news_analyzer = FixedNews()

    agent.run_once_with_candles(candles())

    record = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["executed"] is False
    assert record["risk"]["reason"] == "Max trades per day reached"


def test_corrupt_paper_state_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "true")
    (tmp_path / "paper-portfolio.json").write_text("not-json", encoding="utf-8")
    agent = TradingAgent(load_config(write_config(tmp_path)))
    agent.strategy = FixedStrategy(signal())
    agent.news_analyzer = FixedNews()

    with pytest.raises(RuntimeError, match="state could not be read"):
        agent.run_once_with_candles(candles())


def test_dashboard_reports_automatic_paper_portfolio(tmp_path):
    log = tmp_path / "decisions.jsonl"
    log.write_text(
        json.dumps(
            {
                "mode": "paper",
                "live_trading_enabled": False,
                "kill_switch_active": False,
                "auto_paper_trading_enabled": True,
                "paper_execution_configured": True,
                "executed": True,
                "paper_portfolio": {
                    "paper_balance": 100100,
                    "realized_pnl": 100,
                    "open_position_count": 1,
                    "closed_trade_count": 2,
                    "open_positions": [],
                    "recent_orders": [],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(log))

    health = client.get("/api/health").json()
    assert health["auto_paper_trading_enabled"] is True
    assert health["paper_execution_configured"] is True
    assert client.get("/api/latest").json()["paper_portfolio"]["realized_pnl"] == 100
    assert "paperPositionRows" in client.get("/").text
    assert client.post("/api/latest").status_code == 405


def test_release5_source_still_has_no_live_order_endpoints():
    repo = Path(__file__).resolve().parents[1]
    source = "\n".join(path.read_text(encoding="utf-8") for path in (repo / "src").glob("*.py"))
    for forbidden in (
        "placeOrder",
        "placeOrderFullResponse",
        "modifyOrder",
        "cancelOrder",
        "gttCreateRule",
        "gttModifyRule",
        "gttCancelRule",
    ):
        assert forbidden not in source
