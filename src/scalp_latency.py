from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .scalp_execution import LatencyTelemetry
from .scalp_shadow import SCALP_RELEASE, ScalpShadowError, ScalpShadowSettings
from .scalp_stream import AngelOneScalpRuntime, parse_smartapi_tick

PHASE_DESCRIPTIONS = {
    "exchange_to_callback_ms": "Angel One exchange timestamp to VPS WebSocket callback",
    "ws_parse_ms": "WebSocket callback to validated Tick object",
    "engine_tick_processing_ms": "Validated tick through the scalp engine",
    "strategy_evaluation_ms": "Closed-bar strategy evaluation",
    "paper_order_processing_ms": "Paper risk checks, simulated fill, costs, and state persistence",
    "signal_to_paper_result_ms": "Strategy signal ready to paper order accept/reject result",
    "tick_to_paper_result_ms": "WebSocket callback start to paper order accept/reject result",
    "exchange_to_paper_result_ms": "Exchange timestamp to paper order result; depends on clock sync",
    "paper_mark_ms": "Open-position mark/exit checks and paper state persistence",
}


def latency_path_for(settings: ScalpShadowSettings) -> Path:
    return settings.decision_log.with_name("scalp_latency.json")


def load_latency_snapshot(path: Path) -> dict | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unreadable"}
    return value if isinstance(value, dict) else {"status": "invalid"}


class LatencyMonitor:
    """Bounded in-memory latency statistics with an atomic read-only status snapshot."""

    def __init__(self, path: Path, *, flush_every_ticks: int = 100) -> None:
        if flush_every_ticks < 1:
            raise ValueError("flush_every_ticks must be positive")
        self.path = path
        self.flush_every_ticks = flush_every_ticks
        self.telemetry = LatencyTelemetry(max_samples=2000)
        self.session_started_at = datetime.now(timezone.utc)
        self._ticks_since_flush = 0
        self._lock = threading.RLock()
        self._local = threading.local()

    def record(self, phase: str, milliseconds: float) -> None:
        with self._lock:
            self.telemetry.record(phase, milliseconds)

    def begin_tick(self, started_perf: float, exchange_to_callback_ms: float | None) -> None:
        self._local.tick_started_perf = started_perf
        self._local.exchange_to_callback_ms = exchange_to_callback_ms
        self._local.signal_id = None
        self._local.signal_ready_perf = None

    def mark_signal_ready(self, signal_id: str) -> None:
        self._local.signal_id = signal_id
        self._local.signal_ready_perf = perf_counter()

    def record_paper_result(self, signal_id: str) -> None:
        finished = perf_counter()
        current_signal = getattr(self._local, "signal_id", None)
        signal_started = getattr(self._local, "signal_ready_perf", None)
        if current_signal == signal_id and isinstance(signal_started, float):
            self.record("signal_to_paper_result_ms", (finished - signal_started) * 1000)
        tick_started = getattr(self._local, "tick_started_perf", None)
        if isinstance(tick_started, float):
            callback_to_result = (finished - tick_started) * 1000
            self.record("tick_to_paper_result_ms", callback_to_result)
            transport = getattr(self._local, "exchange_to_callback_ms", None)
            if isinstance(transport, (int, float)) and transport >= 0:
                self.record("exchange_to_paper_result_ms", float(transport) + callback_to_result)

    def end_tick(self) -> None:
        self._local.tick_started_perf = None
        self._local.exchange_to_callback_ms = None
        self._local.signal_id = None
        self._local.signal_ready_perf = None

    def tick_complete(self) -> None:
        with self._lock:
            self._ticks_since_flush += 1
            should_flush = self._ticks_since_flush >= self.flush_every_ticks
        if should_flush:
            self.flush(force=True)

    def snapshot(self) -> dict:
        with self._lock:
            phases = self.telemetry.snapshot()
        return {
            "status": "ok",
            "release": SCALP_RELEASE,
            "mode": "paper_shadow",
            "live_orders_available": False,
            "session_started_at": self.session_started_at.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "clock_note": (
                "Exchange-derived phases depend on VPS clock synchronization; "
                "negative exchange-to-callback samples are rejected."
            ),
            "phase_descriptions": PHASE_DESCRIPTIONS,
            "phases": phases,
        }

    def flush(self, *, force: bool = False) -> None:
        with self._lock:
            if not force and self._ticks_since_flush < self.flush_every_ticks:
                return
            payload = self.snapshot()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)
            self._ticks_since_flush = 0


class _TimedStrategy:
    def __init__(self, inner: object, monitor: LatencyMonitor) -> None:
        self.inner = inner
        self.monitor = monitor

    def evaluate(self, *args, **kwargs):
        started = perf_counter()
        result = self.inner.evaluate(*args, **kwargs)
        self.monitor.record("strategy_evaluation_ms", (perf_counter() - started) * 1000)
        if result is not None:
            self.monitor.mark_signal_ready(str(result.signal_id))
        return result

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class _TimedBroker:
    def __init__(self, inner: object, monitor: LatencyMonitor) -> None:
        self.inner = inner
        self.monitor = monitor

    def place(self, signal, quote):
        started = perf_counter()
        try:
            return self.inner.place(signal, quote)
        finally:
            self.monitor.record("paper_order_processing_ms", (perf_counter() - started) * 1000)
            self.monitor.record_paper_result(str(signal.signal_id))
            self.monitor.flush(force=True)

    def mark(self, quote, *, reason: str | None = None):
        started = perf_counter()
        result = None
        try:
            result = self.inner.mark(quote, reason=reason)
            return result
        finally:
            self.monitor.record("paper_mark_ms", (perf_counter() - started) * 1000)
            if result is not None:
                self.monitor.flush(force=True)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class LatencyAngelOneScalpRuntime(AngelOneScalpRuntime):
    """Release 6.1 runtime with paper-only end-to-end latency instrumentation."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.latency: LatencyMonitor | None = None

    def prepare(self) -> dict:
        manifest = super().prepare()
        if self.engine is None:
            raise ScalpShadowError("Scalp engine was unavailable after runtime preparation")
        self.latency = LatencyMonitor(latency_path_for(self.settings))
        self.engine.strategy = _TimedStrategy(self.engine.strategy, self.latency)
        self.engine.broker = _TimedBroker(self.engine.broker, self.latency)
        manifest["latency_telemetry"] = {
            "enabled": True,
            "status_file": str(self.latency.path),
            "flush_every_ticks": self.latency.flush_every_ticks,
            "phases": PHASE_DESCRIPTIONS,
        }
        self._write_manifest(manifest)
        self.engine.audit.write("latency_telemetry_ready", manifest["latency_telemetry"])
        self.latency.flush(force=True)
        return manifest

    def _on_data(self, websocket_app, message: object) -> None:
        if self.engine is None or self.latency is None:
            return super()._on_data(websocket_app, message)

        callback_started = perf_counter()
        received_at = self.clock()
        parse_started = perf_counter()
        try:
            tick = parse_smartapi_tick(
                message,
                self.instruments,
                received_at=received_at,
                price_scale=float(self.values.get("price_scale", 100)),
            )
        except ScalpShadowError as exc:
            self.latency.record("ws_parse_ms", (perf_counter() - parse_started) * 1000)
            self.engine.audit.write("stream_message_rejected", {"reason": str(exc)})
            return

        self.latency.record("ws_parse_ms", (perf_counter() - parse_started) * 1000)
        transport_ms = (tick.received_at - tick.timestamp).total_seconds() * 1000
        usable_transport = transport_ms if transport_ms >= 0 else None
        if usable_transport is not None:
            self.latency.record("exchange_to_callback_ms", usable_transport)

        self.latency.begin_tick(callback_started, usable_transport)
        engine_started = perf_counter()
        try:
            self.engine.on_tick(tick)
        finally:
            self.latency.record(
                "engine_tick_processing_ms",
                (perf_counter() - engine_started) * 1000,
            )
            self.latency.end_tick()
            self.latency.tick_complete()
