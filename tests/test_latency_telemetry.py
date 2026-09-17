from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from src.scalp_latency import (
    DIAGNOSTIC_RELEASE,
    LatencyMonitor,
    _TimedBroker,
    _TimedStrategy,
    latency_path_for,
    load_latency_snapshot,
)


class FakeSignal:
    signal_id = "signal-1"


class FakeStrategy:
    def evaluate(self, *args, **kwargs):
        return FakeSignal()


class FakeBroker:
    def place(self, signal, quote):
        return {"accepted": True, "signal_id": signal.signal_id}

    def mark(self, quote, *, reason=None):
        return None

    def snapshot(self):
        return {"open_position_count": 0}


def _execution_tick(timestamp_ms: int, *, received_delay_ms: int = 100, price: float = 23000.0):
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return SimpleNamespace(
        role="execution",
        timestamp=timestamp,
        received_at=timestamp + timedelta(milliseconds=received_delay_ms),
        last_price=price,
        bid=price - 0.05,
        ask=price + 0.05,
    )


def test_latency_monitor_persists_percentiles_atomically(tmp_path: Path):
    path = tmp_path / "scalp_latency.json"
    monitor = LatencyMonitor(path, flush_every_ticks=2)
    for value in (1.0, 2.0, 3.0, 4.0):
        monitor.record("exchange_to_callback_ms", value)
    monitor.flush(force=True)

    snapshot = load_latency_snapshot(path)

    assert snapshot is not None
    assert snapshot["release"] == DIAGNOSTIC_RELEASE
    assert snapshot["mode"] == "paper_shadow"
    assert snapshot["live_orders_available"] is False
    assert snapshot["phases"]["exchange_to_callback_ms"] == {
        "count": 4,
        "p50_ms": 2.0,
        "p95_ms": 4.0,
        "p99_ms": 4.0,
        "max_ms": 4.0,
    }
    assert snapshot["execution_feed_diagnostics"]["execution_callbacks"] == 0
    assert path.stat().st_mode & 0o777 == 0o600


def test_timed_strategy_and_broker_record_signal_to_paper_path(tmp_path: Path):
    monitor = LatencyMonitor(tmp_path / "latency.json")
    strategy = _TimedStrategy(FakeStrategy(), monitor)
    broker = _TimedBroker(FakeBroker(), monitor)
    monitor.begin_tick(perf_counter(), 7.5)

    signal = strategy.evaluate(None, None)
    order = broker.place(signal, object())
    monitor.end_tick()

    phases = monitor.snapshot()["phases"]
    assert order["accepted"] is True
    assert phases["strategy_evaluation_ms"]["count"] == 1
    assert phases["paper_order_processing_ms"]["count"] == 1
    assert phases["signal_to_paper_result_ms"]["count"] == 1
    assert phases["tick_to_paper_result_ms"]["count"] == 1
    assert phases["exchange_to_paper_result_ms"]["count"] == 1


def test_execution_feed_diagnostics_measure_reorders_duplicates_and_bursts(tmp_path: Path):
    monitor = LatencyMonitor(tmp_path / "latency.json")
    base = 1_700_000_000_000

    assert monitor.observe_execution_tick(
        _execution_tick(base, price=23000.0),
        raw_exchange_timestamp_ms=base,
        callback_started_perf=10.000,
        max_tick_age_ms=2500,
    ) == "accepted"
    assert monitor.observe_execution_tick(
        _execution_tick(base, price=23000.1),
        raw_exchange_timestamp_ms=base,
        callback_started_perf=10.010,
        max_tick_age_ms=2500,
    ) == "accepted"
    assert monitor.observe_execution_tick(
        _execution_tick(base - 100, price=22999.8),
        raw_exchange_timestamp_ms=base - 100,
        callback_started_perf=10.020,
        max_tick_age_ms=2500,
    ) == "out_of_order"
    assert monitor.observe_execution_tick(
        _execution_tick(base - 200, price=22999.7),
        raw_exchange_timestamp_ms=base - 200,
        callback_started_perf=10.030,
        max_tick_age_ms=2500,
    ) == "out_of_order"
    assert monitor.observe_execution_tick(
        _execution_tick(base + 100, price=23000.2),
        raw_exchange_timestamp_ms=base + 100,
        callback_started_perf=10.040,
        max_tick_age_ms=2500,
    ) == "accepted"

    diagnostics = monitor.snapshot()["execution_feed_diagnostics"]
    assert diagnostics["execution_callbacks"] == 5
    assert diagnostics["accepted_ticks"] == 3
    assert diagnostics["rejected_out_of_order_ticks"] == 2
    assert diagnostics["equal_exchange_timestamp_ticks"] == 1
    assert diagnostics["max_reorder_depth_ms"] == 200
    assert diagnostics["max_consecutive_reorder_burst"] == 2
    assert diagnostics["current_consecutive_reorder_burst"] == 0
    assert diagnostics["reordered_quote_changes"]["any"] == 2
    assert diagnostics["callback_interarrival_ms"]["count"] == 4
    assert diagnostics["recent_reorders"][-1]["reorder_delta_ms"] == -200
    assert diagnostics["recent_reorders"][-1]["execution_callback_sequence"] == 4


def test_execution_feed_diagnostics_keep_stale_separate_from_ordering(tmp_path: Path):
    monitor = LatencyMonitor(tmp_path / "latency.json")
    timestamp_ms = 1_700_000_000_000
    stale = _execution_tick(timestamp_ms, received_delay_ms=3000)

    result = monitor.observe_execution_tick(
        stale,
        raw_exchange_timestamp_ms=timestamp_ms,
        callback_started_perf=20.0,
        max_tick_age_ms=2500,
    )

    diagnostics = monitor.snapshot()["execution_feed_diagnostics"]
    assert result == "stale_or_future"
    assert diagnostics["execution_callbacks"] == 1
    assert diagnostics["accepted_ticks"] == 0
    assert diagnostics["rejected_stale_or_future_ticks"] == 1
    assert diagnostics["rejected_out_of_order_ticks"] == 0
    assert diagnostics["last_accepted_exchange_timestamp_ms"] is None


def test_latency_path_is_kept_with_scalp_logs(tmp_path: Path):
    settings = SimpleNamespace(decision_log=tmp_path / "scalp_decisions.jsonl")
    assert latency_path_for(settings) == tmp_path / "scalp_latency.json"


def test_unreadable_latency_snapshot_fails_closed(tmp_path: Path):
    path = tmp_path / "scalp_latency.json"
    path.write_text("{broken", encoding="utf-8")
    assert load_latency_snapshot(path) == {"status": "unreadable"}
