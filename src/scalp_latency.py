from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .scalp_execution import LatencyTelemetry
from .scalp_shadow import ScalpShadowError, ScalpShadowSettings
from .scalp_stream import AngelOneScalpRuntime, parse_smartapi_tick

DIAGNOSTIC_RELEASE = "6.2"

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
    """Bounded latency and execution-feed diagnostics with atomic snapshots."""

    def __init__(self, path: Path, *, flush_every_ticks: int = 100) -> None:
        if flush_every_ticks < 1:
            raise ValueError("flush_every_ticks must be positive")
        self.path = path
        self.flush_every_ticks = flush_every_ticks
        self.telemetry = LatencyTelemetry(max_samples=2000)
        self.execution_timing = LatencyTelemetry(max_samples=5000)
        self.session_started_at = datetime.now(timezone.utc)
        self._ticks_since_flush = 0
        self._lock = threading.RLock()
        self._local = threading.local()

        self._execution_callbacks = 0
        self._execution_accepted = 0
        self._execution_rejected_out_of_order = 0
        self._execution_rejected_stale = 0
        self._execution_equal_timestamp = 0
        self._execution_timestamp_advances = 0
        self._execution_sequence = 0
        self._last_execution_callback_perf: float | None = None
        self._last_accepted_exchange_timestamp_ms: int | None = None
        self._last_accepted_exchange_timestamp_iso: str | None = None
        self._last_accepted_quote: tuple[float, float, float] | None = None
        self._current_reorder_burst = 0
        self._max_reorder_burst = 0
        self._max_reorder_depth_ms = 0
        self._reordered_quote_change_any = 0
        self._reordered_last_price_changes = 0
        self._reordered_bid_changes = 0
        self._reordered_ask_changes = 0
        self._recent_reorders: deque[dict] = deque(maxlen=25)

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

    def observe_execution_tick(
        self,
        tick,
        *,
        raw_exchange_timestamp_ms: int,
        callback_started_perf: float,
        max_tick_age_ms: int,
    ) -> str:
        """Mirror the engine ordering guard without changing its accept/reject policy."""
        with self._lock:
            self._execution_sequence += 1
            sequence = self._execution_sequence
            self._execution_callbacks += 1

            if self._last_execution_callback_perf is not None:
                interarrival_ms = (callback_started_perf - self._last_execution_callback_perf) * 1000
                if interarrival_ms >= 0:
                    self.execution_timing.record("callback_interarrival_ms", interarrival_ms)
            self._last_execution_callback_perf = callback_started_perf

            age_ms = (tick.received_at - tick.timestamp).total_seconds() * 1000
            previous_timestamp_ms = self._last_accepted_exchange_timestamp_ms
            previous_quote = self._last_accepted_quote

            if age_ms < -250 or age_ms > max_tick_age_ms:
                self._execution_rejected_stale += 1
                self._current_reorder_burst = 0
                return "stale_or_future"

            if previous_timestamp_ms is not None and raw_exchange_timestamp_ms < previous_timestamp_ms:
                self._execution_rejected_out_of_order += 1
                self._current_reorder_burst += 1
                self._max_reorder_burst = max(self._max_reorder_burst, self._current_reorder_burst)
                reorder_delta_ms = raw_exchange_timestamp_ms - previous_timestamp_ms
                self._max_reorder_depth_ms = max(self._max_reorder_depth_ms, abs(reorder_delta_ms))

                quote = (float(tick.last_price), float(tick.bid), float(tick.ask))
                quote_changes = {
                    "last_price": bool(previous_quote and quote[0] != previous_quote[0]),
                    "bid": bool(previous_quote and quote[1] != previous_quote[1]),
                    "ask": bool(previous_quote and quote[2] != previous_quote[2]),
                }
                if any(quote_changes.values()):
                    self._reordered_quote_change_any += 1
                if quote_changes["last_price"]:
                    self._reordered_last_price_changes += 1
                if quote_changes["bid"]:
                    self._reordered_bid_changes += 1
                if quote_changes["ask"]:
                    self._reordered_ask_changes += 1

                self._recent_reorders.append(
                    {
                        "execution_callback_sequence": sequence,
                        "received_at": tick.received_at.isoformat(),
                        "incoming_exchange_timestamp_ms": raw_exchange_timestamp_ms,
                        "incoming_exchange_timestamp": tick.timestamp.isoformat(),
                        "previous_accepted_exchange_timestamp_ms": previous_timestamp_ms,
                        "previous_accepted_exchange_timestamp": self._last_accepted_exchange_timestamp_iso,
                        "reorder_delta_ms": reorder_delta_ms,
                        "reorder_depth_ms": abs(reorder_delta_ms),
                        "last_price": float(tick.last_price),
                        "bid": float(tick.bid),
                        "ask": float(tick.ask),
                        "quote_changed_vs_last_accepted": quote_changes,
                        "consecutive_reorder_burst": self._current_reorder_burst,
                    }
                )
                return "out_of_order"

            self._execution_accepted += 1
            self._current_reorder_burst = 0
            if previous_timestamp_ms is not None:
                exchange_step_ms = raw_exchange_timestamp_ms - previous_timestamp_ms
                self.execution_timing.record("accepted_exchange_timestamp_step_ms", exchange_step_ms)
                if exchange_step_ms == 0:
                    self._execution_equal_timestamp += 1
                else:
                    self._execution_timestamp_advances += 1
            self._last_accepted_exchange_timestamp_ms = raw_exchange_timestamp_ms
            self._last_accepted_exchange_timestamp_iso = tick.timestamp.isoformat()
            self._last_accepted_quote = (
                float(tick.last_price),
                float(tick.bid),
                float(tick.ask),
            )
            return "accepted"

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

    def _execution_feed_snapshot(self) -> dict:
        callbacks = self._execution_callbacks
        accepted = self._execution_accepted
        reordered = self._execution_rejected_out_of_order
        stale = self._execution_rejected_stale
        timing = self.execution_timing.snapshot()
        return {
            "status": "ok",
            "ordering_policy": "Reject stale/future ticks first, then reject execution ticks older than the latest accepted exchange timestamp. Equal timestamps remain accepted.",
            "execution_callbacks": callbacks,
            "accepted_ticks": accepted,
            "rejected_out_of_order_ticks": reordered,
            "rejected_stale_or_future_ticks": stale,
            "acceptance_rate_pct": round(accepted / callbacks * 100, 4) if callbacks else None,
            "out_of_order_rate_pct": round(reordered / callbacks * 100, 4) if callbacks else None,
            "stale_or_future_rate_pct": round(stale / callbacks * 100, 4) if callbacks else None,
            "equal_exchange_timestamp_ticks": self._execution_equal_timestamp,
            "exchange_timestamp_advance_ticks": self._execution_timestamp_advances,
            "max_reorder_depth_ms": self._max_reorder_depth_ms,
            "current_consecutive_reorder_burst": self._current_reorder_burst,
            "max_consecutive_reorder_burst": self._max_reorder_burst,
            "last_accepted_exchange_timestamp_ms": self._last_accepted_exchange_timestamp_ms,
            "last_accepted_exchange_timestamp": self._last_accepted_exchange_timestamp_iso,
            "callback_interarrival_ms": timing.get("callback_interarrival_ms"),
            "accepted_exchange_timestamp_step_ms": timing.get("accepted_exchange_timestamp_step_ms"),
            "reordered_quote_changes": {
                "any": self._reordered_quote_change_any,
                "last_price": self._reordered_last_price_changes,
                "bid": self._reordered_bid_changes,
                "ask": self._reordered_ask_changes,
            },
            "recent_reorders": list(self._recent_reorders),
        }

    def snapshot(self) -> dict:
        with self._lock:
            phases = self.telemetry.snapshot()
            execution_feed = self._execution_feed_snapshot()
        return {
            "status": "ok",
            "release": DIAGNOSTIC_RELEASE,
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
            "execution_feed_diagnostics": execution_feed,
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
    """Release 6.2 diagnostics wrapper around the fail-closed paper scalp runtime."""

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
        manifest["release"] = DIAGNOSTIC_RELEASE
        manifest["latency_telemetry"] = {
            "enabled": True,
            "status_file": str(self.latency.path),
            "flush_every_ticks": self.latency.flush_every_ticks,
            "phases": PHASE_DESCRIPTIONS,
            "execution_feed_diagnostics": {
                "enabled": True,
                "ordering_policy_unchanged": True,
                "recent_reorder_samples": 25,
            },
        }
        self._write_manifest(manifest)
        self.engine.audit.write(
            "release_6_2_feed_diagnostics_ready",
            {
                "release": DIAGNOSTIC_RELEASE,
                "live_orders_available": False,
                "paper_execution_enabled": self.settings.paper_execution_enabled,
                "status_file": str(self.latency.path),
                "ordering_policy_unchanged": True,
            },
        )
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
            self.engine.audit.write("stream_message_rejected", {"release": DIAGNOSTIC_RELEASE, "reason": str(exc)})
            return

        self.latency.record("ws_parse_ms", (perf_counter() - parse_started) * 1000)
        transport_ms = (tick.received_at - tick.timestamp).total_seconds() * 1000
        usable_transport = transport_ms if transport_ms >= 0 else None
        if usable_transport is not None:
            self.latency.record("exchange_to_callback_ms", usable_transport)

        if tick.role == "execution":
            raw_timestamp_ms = _raw_exchange_timestamp_ms(message, tick)
            self.latency.observe_execution_tick(
                tick,
                raw_exchange_timestamp_ms=raw_timestamp_ms,
                callback_started_perf=callback_started,
                max_tick_age_ms=self.settings.max_tick_age_ms,
            )

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


def _raw_exchange_timestamp_ms(message: object, tick) -> int:
    if isinstance(message, dict):
        try:
            return int(message["exchange_timestamp"])
        except (KeyError, TypeError, ValueError):
            pass
    return int(round(tick.timestamp.timestamp() * 1000))
