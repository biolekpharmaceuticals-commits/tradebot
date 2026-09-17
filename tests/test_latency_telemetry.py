from __future__ import annotations

from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from src.scalp_latency import (
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


def test_latency_monitor_persists_percentiles_atomically(tmp_path: Path):
    path = tmp_path / "scalp_latency.json"
    monitor = LatencyMonitor(path, flush_every_ticks=2)
    for value in (1.0, 2.0, 3.0, 4.0):
        monitor.record("exchange_to_callback_ms", value)
    monitor.flush(force=True)

    snapshot = load_latency_snapshot(path)

    assert snapshot is not None
    assert snapshot["mode"] == "paper_shadow"
    assert snapshot["live_orders_available"] is False
    assert snapshot["phases"]["exchange_to_callback_ms"] == {
        "count": 4,
        "p50_ms": 2.0,
        "p95_ms": 4.0,
        "p99_ms": 4.0,
        "max_ms": 4.0,
    }
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


def test_latency_path_is_kept_with_scalp_logs(tmp_path: Path):
    settings = SimpleNamespace(decision_log=tmp_path / "scalp_decisions.jsonl")
    assert latency_path_for(settings) == tmp_path / "scalp_latency.json"


def test_unreadable_latency_snapshot_fails_closed(tmp_path: Path):
    path = tmp_path / "scalp_latency.json"
    path.write_text("{broken", encoding="utf-8")
    assert load_latency_snapshot(path) == {"status": "unreadable"}
