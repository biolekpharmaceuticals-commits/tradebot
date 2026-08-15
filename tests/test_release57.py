from __future__ import annotations

import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

from src.agent import TradingAgent
from src.config import load_config
from src.option_paper import DefinedRiskOptionPaperBroker
from src.safety import SafetyConfigError
from src.strategy import TradeSignal


def structure(*, max_loss: float = 1950) -> dict:
    long_strike = 24150 if max_loss > 4500 else 24200
    return {
        "name": "bull_put_credit_spread",
        "underlying": "NIFTY 50",
        "expiry": "2026-08-20",
        "lot_size": 65,
        "lots": 1,
        "max_profit": 1300,
        "max_loss": max_loss,
        "risk_budget": 4500,
        "risk_eligible": True,
        "legs": [
            {
                "side": "BUY",
                "symbol": "NIFTY20AUG2624200PE",
                "exchange": "NFO",
                "token": "101",
                "expiry": "2026-08-20",
                "strike": long_strike,
                "lot_size": 65,
                "derivative_type": "put",
                "price": 20,
            },
            {
                "side": "SELL",
                "symbol": "NIFTY20AUG2624250PE",
                "exchange": "NFO",
                "token": "102",
                "expiry": "2026-08-20",
                "strike": 24250,
                "lot_size": 65,
                "derivative_type": "put",
                "price": 40,
            },
        ],
    }


def broker(tmp_path, clock):
    return DefinedRiskOptionPaperBroker(
        state_path=tmp_path / "option-paper.json",
        initial_balance=300000,
        risk_limit_pct=1.5,
        daily_loss_limit_pct=1.5,
        max_trades_per_day=2,
        stop_after_consecutive_losses=3,
        slippage_bps=5,
        fee_bps=10,
        entry_start="09:30",
        entry_end="14:30",
        force_exit="15:10",
        clock=clock,
        id_factory=lambda: "FIXED",
    )


def quotes(short_ask: float, long_bid: float) -> list[dict]:
    return [
        {"exchange": "NFO", "token": "101", "best_bid": long_bid, "best_ask": long_bid + 1},
        {"exchange": "NFO", "token": "102", "best_bid": short_ask - 1, "best_ask": short_ask},
    ]


def test_option_paper_structure_persists_marks_and_force_exits(tmp_path):
    now = [datetime(2026, 8, 17, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))]
    paper = broker(tmp_path, lambda: now[0])

    entry = paper.place_structure(structure(), "2026-08-14|bull_put|2026-08-20")
    marked = paper.reconcile(quotes(short_ask=30, long_bid=15))

    assert entry.accepted is True
    assert marked is None
    assert paper.snapshot()["open_position_count"] == 1
    assert paper.snapshot()["unrealized_pnl"] > 0
    assert stat.S_IMODE((tmp_path / "option-paper.json").stat().st_mode) == 0o600

    now[0] = datetime(2026, 8, 17, 15, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
    exit_order = paper.reconcile(quotes(short_ask=30, long_bid=15))
    snapshot = paper.snapshot()

    assert exit_order is not None and exit_order.accepted is True
    assert "force_exit" in exit_order.message
    assert snapshot["open_position_count"] == 0
    assert snapshot["closed_trade_count"] == 1
    assert snapshot["realized_pnl"] > 0


def test_option_paper_blocks_duplicate_open_and_excess_maximum_loss(tmp_path):
    paper = broker(
        tmp_path,
        lambda: datetime(2026, 8, 17, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    excessive = paper.place_structure(structure(max_loss=5000), "risk")
    first = paper.place_structure(structure(), "one")
    duplicate = paper.place_structure(structure(), "two")

    assert excessive.accepted is False
    assert "risk limit" in excessive.message
    assert first.accepted is True
    assert duplicate.accepted is False
    assert "Maximum one" in duplicate.message


def test_option_paper_requires_frozen_spot_gate(tmp_path):
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("option_selling:\n  enabled: false", "option_selling:\n  enabled: true")
    text = text.replace("derivatives:\n  enabled: false", "derivatives:\n  enabled: true")
    text = text.replace("market_data:\n  provider: demo", "market_data:\n  provider: angel_one")
    text = text.replace("paper_execution:\n  enabled: false", "paper_execution:\n  enabled: true")
    text = text.replace("  paper_execution_enabled: false", "  paper_execution_enabled: true")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(SafetyConfigError, match="frozen spot_trend"):
        load_config(path)


def test_option_paper_module_has_no_live_order_api_calls():
    source = (Path(__file__).resolve().parents[1] / "src" / "option_paper.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("placeOrder", "place_order", "modifyOrder", "cancelOrder"):
        assert forbidden not in source


def test_agent_routes_eligible_structure_only_to_option_paper_simulator(tmp_path, monkeypatch):
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    replacements = (
        ("scanner:\n  enabled: false", "scanner:\n  enabled: true"),
        ("derivatives:\n  enabled: false", "derivatives:\n  enabled: true"),
        ("option_selling:\n  enabled: false", "option_selling:\n  enabled: true"),
        ("market_data:\n  provider: demo", "market_data:\n  provider: angel_one"),
        ("paper_execution:\n  enabled: false", "paper_execution:\n  enabled: true"),
        ("  paper_execution_enabled: false", "  paper_execution_enabled: true"),
        ("  spot_trend:\n    enabled: false", "  spot_trend:\n    enabled: true"),
        ("  require_manual_approval: true", "  require_manual_approval: false"),
        ("  max_risk_per_trade_pct: 0.5", "  max_risk_per_trade_pct: 1.5"),
        ("  max_daily_loss_pct: 1.0", "  max_daily_loss_pct: 1.5"),
        ("    - iron_fly\n", ""),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "true")

    intraday = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-08-17 09:15", periods=100, freq="5min"),
            "open": [100.0] * 100,
            "high": [101.0] * 100,
            "low": [99.0] * 100,
            "close": [100.0] * 100,
            "volume": [200000] * 100,
        }
    )
    sessions = pd.bdate_range(end="2026-08-14", periods=25)
    daily = pd.DataFrame(
        {
            "timestamp": sessions,
            "close": [100 + 0.5 * index + (1 if index % 2 else -1) for index in range(25)],
        }
    )

    class Market:
        def get_candles(self, symbol_config):
            return daily if symbol_config.get("timeframe") == "ONE_DAY" else intraday

        def get_full_quotes(self, contracts):
            return []

    class Strategy:
        def evaluate(self, candles, news, bulk_deals=None, instrument_type="equity"):
            return TradeSignal("BUY", 90, "Bullish", 100, 98, 103, 1.5, "test")

    class OptionEngine:
        enabled = True

        def propose(self, underlying, spot_price, regime=None):
            return {
                "status": "candidate",
                "selected": structure(),
                "paper_execution_allowed": True,
                "structures": [structure()],
            }

    class NoStandardOrderBroker:
        def place_order(self, *args, **kwargs):
            raise AssertionError("Option structures must never use the standard broker")

    agent = TradingAgent(load_config(path))
    agent.market_data = Market()
    agent.strategy = Strategy()
    agent.option_selling = OptionEngine()
    agent.option_paper_broker.clock = lambda: datetime(
        2026, 8, 17, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")
    )
    agent.broker = NoStandardOrderBroker()

    agent.run_once()

    record = (tmp_path / "logs" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"mode": "oi_defined_risk_option_selling_paper"' in record
    assert '"executed": true' in record
    assert '"order_id": "OPTION-PAPER-' in record
    assert agent.option_paper_broker.snapshot()["open_position_count"] == 1
