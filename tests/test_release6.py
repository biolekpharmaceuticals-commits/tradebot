from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import load_config
from src.scalp_shadow import (
    Bar,
    MultiTimeframeBars,
    MultiTimeframeScalpStrategy,
    ScalpPaperBroker,
    ScalpShadowEngine,
    ScalpSignal,
    Tick,
    load_scalp_shadow_settings,
    replay_ticks,
    validate_scalp_shadow_config,
)
from src.scalp_stream import parse_smartapi_tick

KOLKATA = ZoneInfo("Asia/Kolkata")


def settings(tmp_path: Path, **overrides):
    values = {
        "enabled": True,
        "paper_only": True,
        "shadow_mode": True,
        "defined_risk_only": True,
        "live_order_enabled": False,
        "paper_execution_enabled": True,
        "signal_instrument": {
            "exchange": "NSE",
            "symbol": "NIFTY 50",
            "token": "99926000",
        },
        "execution_instrument": "nearest_nifty_future",
        "state_file": str(tmp_path / "scalp-state.json"),
        "tick_log_dir": str(tmp_path / "ticks"),
        "decision_log": str(tmp_path / "scalp-decisions.jsonl"),
        **overrides,
    }
    return load_scalp_shadow_settings(values, tmp_path)


def tick(
    when: datetime,
    *,
    role: str = "signal",
    token: str = "99926000",
    symbol: str = "NIFTY 50",
    price: float = 24000,
    bid: float = 23999.5,
    ask: float = 24000,
    lot_size: int = 1,
    received_delay_ms: int = 0,
) -> Tick:
    return Tick(
        role=role,
        symbol=symbol,
        exchange="NSE" if role == "signal" else "NFO",
        token=token,
        timestamp=when,
        received_at=when + timedelta(milliseconds=received_delay_ms),
        last_price=price,
        bid=bid,
        ask=ask,
        cumulative_volume=1000,
        last_quantity=1,
        lot_size=lot_size,
        tick_size=0.05,
    )


def signal(when: datetime, direction: str = "BUY") -> ScalpSignal:
    return ScalpSignal(
        signal_id=f"{when.isoformat()}|{direction}",
        direction=direction,
        generated_at=when,
        signal_price=24000,
        stop_bps=3,
        target_bps=6,
        reason="test",
    )


def test_release5_config_still_loads_without_scalp_activation(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "config.example.yaml"
    path = tmp_path / "config.yaml"
    path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "true")

    config = load_config(path)

    assert config.raw["scalp_shadow"]["enabled"] is False
    assert config.raw["option_selling"]["paper_only"] is True


def test_scalp_config_is_paper_only_and_tightly_bounded(tmp_path):
    base = {
        "enabled": True,
        "signal_instrument": {"exchange": "NSE", "symbol": "NIFTY 50", "token": "99926000"},
        "execution_instrument": "nearest_nifty_future",
        "state_file": "state.json",
        "tick_log_dir": "ticks",
        "decision_log": "audit.jsonl",
    }
    with pytest.raises(ValueError, match="live_order_enabled"):
        validate_scalp_shadow_config({**base, "live_order_enabled": True})
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        validate_scalp_shadow_config({**base, "risk_per_trade_pct": 1.5})
    with pytest.raises(ValueError, match="NIFTY 50"):
        validate_scalp_shadow_config(
            {**base, "signal_instrument": {"exchange": "NSE", "symbol": "NIFTY BANK", "token": "99926009"}}
        )


def test_tick_aggregator_closes_one_and_five_minute_bars():
    bars = MultiTimeframeBars()
    start = datetime(2026, 8, 24, 9, 15, tzinfo=KOLKATA)
    closed = {}
    for index in range(6):
        closed = bars.on_tick(tick(start + timedelta(minutes=index), price=24000 + index))

    assert 60 in closed and 300 in closed
    assert len(bars.bars(60)) == 5
    assert len(bars.bars(300)) == 1
    assert bars.bars(60)[0].open == 24000
    assert bars.bars(300)[0].close == 24004


def test_strategy_requires_aligned_one_and_five_minute_breakout(tmp_path):
    configured = replace(
        settings(tmp_path),
        min_one_minute_bars=15,
        min_five_minute_bars=6,
        minimum_atr_bps=0.5,
        maximum_atr_bps=100,
    )
    bars = MultiTimeframeBars()
    start = datetime(2026, 8, 24, 9, 15, tzinfo=KOLKATA)
    for index in range(15):
        close = 100 + index * 0.1
        bars.history[60].append(
            Bar(60, start + timedelta(minutes=index), start + timedelta(minutes=index + 1), close - 0.02, close + 0.02, close - 0.04, close, 100, 5)
        )
    for index in range(6):
        close = 100 + index * 0.4
        bars.history[300].append(
            Bar(300, start + timedelta(minutes=index * 5), start + timedelta(minutes=(index + 1) * 5), close - 0.05, close + 0.05, close - 0.08, close, 500, 25)
        )
    previous = bars.history[60][-2]
    latest = bars.history[60][-1]
    bars.history[60][-2] = replace(previous, high=latest.close - 0.01)

    result = MultiTimeframeScalpStrategy(configured).evaluate(bars, latest.end)

    assert result is not None
    assert result.direction == "BUY"
    assert "5m EMA" in result.reason


def test_paper_broker_persists_one_lot_entry_and_target_exit(tmp_path):
    now = [datetime(2026, 8, 24, 10, 0, tzinfo=KOLKATA)]
    broker = ScalpPaperBroker(settings(tmp_path), clock=lambda: now[0], id_factory=lambda: "FIXED")
    quote = tick(
        now[0],
        role="execution",
        token="FUT1",
        symbol="NIFTY27AUG26FUT",
        price=24000,
        bid=23999.5,
        ask=24000,
        lot_size=65,
    )

    entry = broker.place(signal(now[0]), quote)
    now[0] += timedelta(seconds=30)
    exit_quote = replace(
        quote,
        timestamp=now[0],
        received_at=now[0],
        last_price=float(entry["target_price"]) + 1,
        bid=float(entry["target_price"]) + 1,
        ask=float(entry["target_price"]) + 1.05,
    )
    exit_order = broker.mark(exit_quote)

    assert entry["accepted"] is True
    assert exit_order is not None and exit_order["exit_reason"] == "profit_target"
    assert broker.snapshot()["closed_trade_count"] == 1
    assert stat.S_IMODE((tmp_path / "scalp-state.json").stat().st_mode) == 0o600


def test_paper_broker_rejects_wide_spread_duplicate_and_excess_risk(tmp_path):
    now = datetime(2026, 8, 24, 10, 0, tzinfo=KOLKATA)
    broker = ScalpPaperBroker(settings(tmp_path), clock=lambda: now)
    wide = tick(now, role="execution", token="FUT1", symbol="FUT", bid=23900, ask=24000, lot_size=65)
    assert "spread" in broker.place(signal(now), wide)["reason"]

    constrained = ScalpPaperBroker(replace(settings(tmp_path / "other"), risk_per_trade_pct=0.05), clock=lambda: now)
    normal = tick(now, role="execution", token="FUT1", symbol="FUT", lot_size=65)
    assert "risk budget" in constrained.place(signal(now), normal)["reason"]

    exposure_broker = ScalpPaperBroker(
        replace(settings(tmp_path / "exposure"), slippage_bps=0, fee_bps=0, stop_bps=2, target_bps=3),
        clock=lambda: now,
    )
    oversized = replace(normal, last_price=28000, bid=27999.5, ask=28000, lot_size=65)
    narrow_signal = replace(signal(now + timedelta(seconds=1)), stop_bps=2, target_bps=3)
    assert "exposure cap" in exposure_broker.place(narrow_signal, oversized)["reason"]


def test_stream_parser_requires_timestamp_and_preserves_actual_depth():
    now = datetime(2026, 8, 24, 10, 0, tzinfo=KOLKATA)
    instruments = {
        "123": {
            "role": "execution",
            "symbol": "NIFTY27AUG26FUT",
            "exchange": "NFO",
            "lot_size": 65,
            "tick_size": 0.05,
        }
    }
    message = {
        "token": "123",
        "exchange_timestamp": int(now.timestamp() * 1000),
        "last_traded_price": 2400010,
        "best_5_buy_data": [{"price": 2400000}],
        "best_5_sell_data": [{"price": 2400020}],
        "volume_trade_for_the_day": 5000,
        "last_traded_quantity": 65,
    }

    parsed = parse_smartapi_tick(message, instruments, received_at=now, price_scale=100)

    assert parsed.last_price == 24000.1
    assert parsed.bid == 24000
    assert parsed.ask == 24000.2
    with pytest.raises(Exception, match="malformed"):
        parse_smartapi_tick({"token": "123"}, instruments, received_at=now)


def test_engine_records_ticks_but_rejects_stale_data(tmp_path):
    now = [datetime(2026, 8, 24, 10, 0, tzinfo=KOLKATA)]
    engine = ScalpShadowEngine(
        settings(tmp_path),
        execution_token="FUT1",
        kill_switch_active=False,
        auto_paper_enabled=True,
        clock=lambda: now[0],
    )
    stale = tick(now[0] - timedelta(seconds=5), received_delay_ms=5000)

    engine.on_tick(stale)

    assert engine.health()["rejected_stale_ticks"] == 1
    recorded = next((tmp_path / "ticks").glob("*.jsonl"))
    assert json.loads(recorded.read_text(encoding="utf-8"))["token"] == "99926000"
    assert stat.S_IMODE(recorded.stat().st_mode) == 0o600


def test_tick_replay_uses_same_engine_and_closes_end_of_data(tmp_path):
    configured = replace(
        settings(tmp_path),
        min_one_minute_bars=15,
        min_five_minute_bars=6,
        minimum_atr_bps=0.5,
        maximum_atr_bps=100,
    )
    start = datetime(2026, 8, 24, 9, 20, tzinfo=KOLKATA)
    ticks = []
    for index in range(32):
        when = start + timedelta(minutes=index)
        price = 24000 + index * 5
        ticks.extend(
            (
                tick(
                    when,
                    role="execution",
                    token="FUT1",
                    symbol="NIFTY27AUG26FUT",
                    price=price,
                    bid=price - 0.5,
                    ask=price,
                    lot_size=65,
                ),
                tick(when, price=price),
            )
        )
    clock = [start]
    engine = ScalpShadowEngine(
        configured,
        execution_token="FUT1",
        kill_switch_active=False,
        auto_paper_enabled=True,
        clock=lambda: clock[0],
        record_ticks=False,
    )

    report = replay_ticks(engine, ticks, advance_clock=lambda value: clock.__setitem__(0, value))

    assert report["ticks"] == 64
    assert report["trades"] >= 1
    assert report["open_position_count"] == 0
    assert report["trade_ledger"][-1]["exit_reason"] in {"profit_target", "end_of_data"}
    assert "max_drawdown_pct" in report
    assert "trade_sharpe" in report
    assert "exposure_pct" in report


def test_release6_source_has_no_live_order_transport():
    root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "src/scalp_shadow.py",
            "src/scalp_stream.py",
            "run_scalp_shadow.py",
            "run_scalp_replay.py",
            "run_scalp_status.py",
        )
    )
    for forbidden in ("placeOrder", "modifyOrder", "cancelOrder", "place_order"):
        assert forbidden not in source


def test_service_is_isolated_from_release57_timer_and_agent():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/tradebot-scalp-shadow.service").read_text(encoding="utf-8")
    assert "run_scalp_shadow.py" in service
    assert "run_agent.py" not in service
    assert "EnvironmentFile=/etc/tradebot/tradebot.env" in service
    assert "ReadWritePaths=/opt/tradebot/logs" in service
