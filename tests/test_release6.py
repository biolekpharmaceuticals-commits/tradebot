from __future__ import annotations

import json
import stat
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from run_scalp_shadow import market_session_open
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
from src.scalp_execution import (
    ExecutionIntent,
    LatencyTelemetry,
    PriorityIntentQueue,
    SegmentActionLimiter,
    load_execution_readiness,
)
from src.scalp_stream import (
    SMARTAPI_HTTP_POOL,
    _sanitized_response_error,
    _smart_connect_factory,
    parse_smartapi_tick,
)

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
        "cost_model": {
            "brokerage_per_order": 0,
            "stt_sell_bps": 0,
            "exchange_transaction_bps": 0,
            "sebi_turnover_bps": 0,
            "stamp_duty_buy_bps": 0,
            "gst_pct": 0,
            "additional_fee_bps": 0,
        },
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
    with pytest.raises(ValueError, match="live_transport_enabled"):
        validate_scalp_shadow_config(
            {
                **base,
                "execution_readiness": {"live_transport_enabled": True},
            }
        )
    with pytest.raises(ValueError, match="1 to 8"):
        validate_scalp_shadow_config(
            {
                **base,
                "execution_readiness": {"max_actions_per_second": 9},
            }
        )


def test_execution_limiter_reserves_half_capacity_for_exits():
    now = [100.0]
    configured = load_execution_readiness({})
    limiter = SegmentActionLimiter(configured, clock=lambda: now[0])

    entries = [limiter.admit("NFO", "NEW_ENTRY") for _ in range(5)]
    exits = [limiter.admit("NFO", "EXIT") for _ in range(4)]

    assert [allowed for allowed, _ in entries] == [True, True, True, True, False]
    assert all(allowed for allowed, _ in exits)
    assert limiter.admit("NFO", "CANCEL")[0] is False
    assert limiter.admit("NSE", "NEW_ENTRY")[0] is True
    now[0] += 1.001
    assert limiter.admit("NFO", "NEW_ENTRY")[0] is True


def test_execution_queue_prioritizes_risk_reduction_and_blocks_market_orders():
    configured = load_execution_readiness({})
    queue = PriorityIntentQueue(configured, clock=lambda: 100.0)

    entry = ExecutionIntent("entry-1", "NFO", "NEW_ENTRY", "LIMIT", 100.0)
    exit_intent = ExecutionIntent("exit-1", "NFO", "EXIT", "LIMIT", 100.1)
    market = ExecutionIntent("entry-2", "NFO", "NEW_ENTRY", "MARKET", 100.2)
    ioc = ExecutionIntent("entry-3", "NFO", "NEW_ENTRY", "LIMIT", 100.3, "IOC")
    assert queue.enqueue(entry)[0] is True
    assert queue.enqueue(exit_intent)[0] is True
    assert queue.enqueue(market)[0] is False
    assert queue.enqueue(ioc)[0] is False
    assert queue.pop() == exit_intent
    assert queue.pop() == entry


def test_execution_queue_locks_duplicate_and_ambiguous_intents_until_reconciled():
    now = [100.0]
    configured = load_execution_readiness({"ambiguous_lock_seconds": 30})
    queue = PriorityIntentQueue(configured, clock=lambda: now[0])
    intent = ExecutionIntent("signal-1", "NFO", "NEW_ENTRY", "LIMIT", now[0])

    assert queue.enqueue(intent)[0] is True
    assert queue.enqueue(intent)[0] is False
    queue.pop()
    queue.mark_ambiguous(intent.dedupe_key)
    now[0] += 31
    assert queue.reconciliation_due(intent.dedupe_key) is True
    assert queue.enqueue(intent)[0] is False
    queue.reconcile(intent.dedupe_key)
    assert queue.enqueue(intent)[0] is True


def test_latency_telemetry_reports_bounded_percentiles():
    telemetry = LatencyTelemetry(max_samples=10)
    for value in range(1, 11):
        telemetry.record("broker_ack", float(value))

    snapshot = telemetry.snapshot()["broker_ack"]

    assert snapshot == {
        "count": 10,
        "p50_ms": 5.0,
        "p95_ms": 10.0,
        "p99_ms": 10.0,
        "max_ms": 10.0,
    }


def test_smartapi_pool_is_bounded_and_disables_automatic_post_retries(monkeypatch):
    assert SMARTAPI_HTTP_POOL == {
        "pool_connections": 2,
        "pool_maxsize": 4,
        "max_retries": 0,
        "pool_block": True,
    }
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "SmartApi", SimpleNamespace(SmartConnect=fake_connect))
    _smart_connect_factory("redacted-api-key")

    assert captured == {
        "api_key": "redacted-api-key",
        "pool": SMARTAPI_HTTP_POOL,
    }


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
    assert "5-bar" in result.reason
    assert result.stop_bps > configured.stop_bps
    assert result.target_bps >= result.stop_bps * 1.5


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
        replace(settings(tmp_path / "exposure"), slippage_bps=0, stop_bps=2, target_bps=3),
        clock=lambda: now,
    )
    oversized = replace(normal, last_price=28000, bid=27999.5, ask=28000, lot_size=65)
    narrow_signal = replace(signal(now + timedelta(seconds=1)), stop_bps=2, target_bps=3)
    assert "exposure cap" in exposure_broker.place(narrow_signal, oversized)["reason"]


def test_current_futures_costs_reject_one_lot_within_quarter_percent_budget(tmp_path):
    now = datetime(2026, 8, 27, 13, 0, tzinfo=KOLKATA)
    configured = settings(
        tmp_path,
        cost_model={
            "brokerage_per_order": 20,
            "stt_sell_bps": 5,
            "exchange_transaction_bps": 0.18299,
            "sebi_turnover_bps": 0.01,
            "stamp_duty_buy_bps": 0.2,
            "gst_pct": 18,
            "additional_fee_bps": 0,
        },
    )
    broker = ScalpPaperBroker(configured, clock=lambda: now)
    quote = tick(
        now,
        role="execution",
        token="FUT1",
        symbol="NIFTY29SEP26FUT",
        price=24364,
        bid=24363.5,
        ask=24364,
        lot_size=65,
    )

    rejected = broker.place(signal(now), quote)

    assert rejected["accepted"] is False
    assert "risk budget" in rejected["reason"]
    assert rejected["maximum_loss"] > 1490
    assert rejected["risk_budget"] == 750
    assert rejected["entry_costs"]["stamp_duty"] > 0
    assert rejected["risk_exit_costs"]["stt"] > 790


def test_six_bps_target_is_rejected_when_net_of_current_costs(tmp_path):
    now = datetime(2026, 8, 27, 13, 0, tzinfo=KOLKATA)
    configured = settings(
        tmp_path,
        capital=10_000_000,
        cost_model={
            "brokerage_per_order": 20,
            "stt_sell_bps": 5,
            "exchange_transaction_bps": 0.18299,
            "sebi_turnover_bps": 0.01,
            "stamp_duty_buy_bps": 0.2,
            "gst_pct": 18,
            "additional_fee_bps": 0,
        },
    )
    broker = ScalpPaperBroker(configured, clock=lambda: now)
    quote = tick(
        now,
        role="execution",
        token="FUT1",
        symbol="NIFTY29SEP26FUT",
        price=24364,
        bid=24363.5,
        ask=24364,
        lot_size=65,
    )

    rejected = broker.place(signal(now), quote)

    assert rejected["accepted"] is False
    assert "not profitable" in rejected["reason"]
    assert rejected["net_target_pnl"] < 0


def test_spread_must_be_small_relative_to_stop(tmp_path):
    now = datetime(2026, 8, 27, 13, 0, tzinfo=KOLKATA)
    broker = ScalpPaperBroker(settings(tmp_path), clock=lambda: now)
    quote = tick(
        now,
        role="execution",
        token="FUT1",
        symbol="NIFTY29SEP26FUT",
        price=24000,
        bid=23998,
        ask=24000,
        lot_size=65,
    )

    rejected = broker.place(signal(now), quote)

    assert rejected["accepted"] is False
    assert "spread" in rejected["reason"]


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


def test_scalp_market_service_window_is_weekday_only():
    assert market_session_open(datetime(2026, 8, 25, 9, 15, tzinfo=KOLKATA)) is True
    assert market_session_open(datetime(2026, 8, 25, 15, 30, tzinfo=KOLKATA)) is True
    assert market_session_open(datetime(2026, 8, 25, 15, 31, tzinfo=KOLKATA)) is False
    assert market_session_open(datetime(2026, 8, 23, 10, 0, tzinfo=KOLKATA)) is False


def test_sanitized_auth_error_is_bounded_and_single_line():
    detail = _sanitized_response_error({"errorcode": "AB1234/<secret>" + "x" * 200})

    assert detail.startswith("AB1234__secret_")
    assert "\n" not in detail
    assert len(detail) <= 32


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


def test_engine_locks_signal_evaluation_after_bar_gap(tmp_path):
    now = [datetime(2026, 8, 24, 10, 0, tzinfo=KOLKATA)]
    engine = ScalpShadowEngine(
        settings(tmp_path),
        execution_token="FUT1",
        kill_switch_active=False,
        auto_paper_enabled=True,
        clock=lambda: now[0],
        record_ticks=False,
    )
    engine.on_tick(tick(now[0]))
    now[0] += timedelta(minutes=2)
    engine.on_tick(tick(now[0]))

    health = engine.health()

    assert health["bar_gaps"] >= 1
    assert health["gap_lockout_until"] is not None


def test_engine_rejects_out_of_order_execution_quotes(tmp_path):
    now = datetime(2026, 8, 24, 10, 0, tzinfo=KOLKATA)
    engine = ScalpShadowEngine(
        settings(tmp_path),
        execution_token="FUT1",
        kill_switch_active=False,
        auto_paper_enabled=True,
        clock=lambda: now,
        record_ticks=False,
    )
    latest = tick(now, role="execution", token="FUT1", symbol="FUT", lot_size=65)
    older = replace(
        latest,
        timestamp=now - timedelta(seconds=1),
        received_at=now,
    )

    engine.on_tick(latest)
    engine.on_tick(older)

    assert engine.health()["rejected_out_of_order_execution_ticks"] == 1


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
            "src/scalp_execution.py",
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
    assert "--market-open-check" in service
    assert "StartLimitIntervalSec=600" in service
    assert "StartLimitBurst=5" in service
    assert "RestartSec=60" in service
    assert "TimeoutStopSec=20" in service
    assert "WorkingDirectory=/opt/tradebot/logs" in service

    start_timer = (root / "deploy/tradebot-scalp-shadow.timer").read_text(encoding="utf-8")
    stop_timer = (root / "deploy/tradebot-scalp-shadow-stop.timer").read_text(encoding="utf-8")
    stop_service = (root / "deploy/tradebot-scalp-shadow-stop.service").read_text(
        encoding="utf-8"
    )
    assert "09:15:00 Asia/Kolkata" in start_timer
    assert "15:31:00 Asia/Kolkata" in stop_timer
    assert "systemctl stop tradebot-scalp-shadow.service" in stop_service
