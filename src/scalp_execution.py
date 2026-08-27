from __future__ import annotations

import heapq
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass


ALLOWED_ACTIONS = frozenset(
    {
        "NEW_ENTRY",
        "ENTRY_MODIFY",
        "RISK_REDUCING_MODIFY",
        "EXIT",
        "CANCEL",
    }
)
RISK_INCREASING_ACTIONS = frozenset({"NEW_ENTRY", "ENTRY_MODIFY"})
ALLOWED_ORDER_TYPES = frozenset({"LIMIT", "STOPLOSS_LIMIT"})
ACTION_PRIORITY = {
    "EXIT": 0,
    "CANCEL": 0,
    "RISK_REDUCING_MODIFY": 1,
    "ENTRY_MODIFY": 2,
    "NEW_ENTRY": 3,
}


@dataclass(frozen=True)
class ExecutionReadinessSettings:
    """Fail-closed controls for a future, separately reviewed order transport."""

    live_transport_enabled: bool = False
    static_ipv4_required: bool = True
    max_actions_per_second: int = 8
    max_entry_actions_per_second: int = 4
    ambiguous_lock_seconds: float = 30.0


@dataclass(frozen=True)
class ExecutionIntent:
    dedupe_key: str
    segment: str
    action: str
    order_type: str
    created_monotonic: float
    duration: str = "DAY"


def load_execution_readiness(config: object) -> ExecutionReadinessSettings:
    if config is None:
        values: dict = {}
    elif isinstance(config, dict):
        values = config
    else:
        raise ValueError("scalp_shadow.execution_readiness must be a mapping")

    if values.get("live_transport_enabled", False) is not False:
        raise ValueError("execution_readiness.live_transport_enabled must remain false")
    if values.get("static_ipv4_required", True) is not True:
        raise ValueError("execution_readiness.static_ipv4_required must remain true")

    maximum = values.get("max_actions_per_second", 8)
    entries = values.get("max_entry_actions_per_second", 4)
    ambiguous = values.get("ambiguous_lock_seconds", 30)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 8:
        raise ValueError("execution_readiness.max_actions_per_second must be from 1 to 8")
    if isinstance(entries, bool) or not isinstance(entries, int) or not 1 <= entries < maximum:
        raise ValueError(
            "execution_readiness.max_entry_actions_per_second must reserve exit capacity"
        )
    if isinstance(ambiguous, bool) or not isinstance(ambiguous, (int, float)):
        raise ValueError("execution_readiness.ambiguous_lock_seconds must be numeric")
    if not 5 <= float(ambiguous) <= 300:
        raise ValueError("execution_readiness.ambiguous_lock_seconds must be from 5 to 300")

    return ExecutionReadinessSettings(
        max_actions_per_second=maximum,
        max_entry_actions_per_second=entries,
        ambiguous_lock_seconds=float(ambiguous),
    )


class SegmentActionLimiter:
    """Rolling one-second limiter with capacity reserved for risk reduction."""

    def __init__(
        self,
        settings: ExecutionReadinessSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._all: dict[str, deque[float]] = defaultdict(deque)
        self._entry: dict[str, deque[float]] = defaultdict(deque)

    def admit(self, segment: str, action: str) -> tuple[bool, str]:
        normalized_segment = segment.strip().upper()
        normalized_action = action.strip().upper()
        if not normalized_segment:
            return False, "exchange segment is required"
        if normalized_action not in ALLOWED_ACTIONS:
            return False, "unsupported execution action"

        now = self.clock()
        all_actions = self._all[normalized_segment]
        entry_actions = self._entry[normalized_segment]
        self._prune(all_actions, now)
        self._prune(entry_actions, now)
        if len(all_actions) >= self.settings.max_actions_per_second:
            return False, "segment action-rate limit reached"
        if (
            normalized_action in RISK_INCREASING_ACTIONS
            and len(entry_actions) >= self.settings.max_entry_actions_per_second
        ):
            return False, "entry capacity exhausted; capacity reserved for exits"

        all_actions.append(now)
        if normalized_action in RISK_INCREASING_ACTIONS:
            entry_actions.append(now)
        return True, "admitted"

    @staticmethod
    def _prune(values: deque[float], now: float) -> None:
        cutoff = now - 1.0
        while values and values[0] <= cutoff:
            values.popleft()


class PriorityIntentQueue:
    """In-memory dry-run queue with duplicate and ambiguous-timeout locks."""

    def __init__(
        self,
        settings: ExecutionReadinessSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._sequence = 0
        self._queue: list[tuple[int, int, ExecutionIntent]] = []
        self._pending: set[str] = set()
        self._ambiguous_until: dict[str, float] = {}

    def enqueue(self, intent: ExecutionIntent) -> tuple[bool, str]:
        action = intent.action.strip().upper()
        order_type = intent.order_type.strip().upper()
        duration = intent.duration.strip().upper()
        key = intent.dedupe_key.strip()
        if action not in ALLOWED_ACTIONS:
            return False, "unsupported execution action"
        if action != "CANCEL" and order_type not in ALLOWED_ORDER_TYPES:
            return False, "only protected limit order types are allowed"
        if action != "CANCEL" and duration != "DAY":
            return False, "IOC and other non-DAY durations are not allowed"
        if not key:
            return False, "dedupe key is required"
        if key in self._pending or key in self._ambiguous_until:
            return False, "duplicate or unresolved execution intent"

        normalized = ExecutionIntent(
            dedupe_key=key,
            segment=intent.segment.strip().upper(),
            action=action,
            order_type=order_type,
            created_monotonic=intent.created_monotonic,
            duration=duration,
        )
        self._sequence += 1
        heapq.heappush(
            self._queue,
            (ACTION_PRIORITY[action], self._sequence, normalized),
        )
        self._pending.add(key)
        return True, "queued"

    def pop(self) -> ExecutionIntent | None:
        if not self._queue:
            return None
        return heapq.heappop(self._queue)[2]

    def complete(self, dedupe_key: str) -> None:
        self._pending.discard(dedupe_key)
        self._ambiguous_until.pop(dedupe_key, None)

    def mark_ambiguous(self, dedupe_key: str) -> None:
        self._pending.add(dedupe_key)
        self._ambiguous_until[dedupe_key] = self.clock() + self.settings.ambiguous_lock_seconds

    def reconcile(self, dedupe_key: str) -> None:
        """Release an intent only after broker status has been reconciled."""

        self.complete(dedupe_key)

    def reconciliation_due(self, dedupe_key: str) -> bool:
        """Report when status should be checked; never unlock automatically."""

        deadline = self._ambiguous_until.get(dedupe_key)
        return deadline is not None and self.clock() >= deadline


class LatencyTelemetry:
    """Bounded latency samples for p50/p95/p99 execution diagnostics."""

    def __init__(self, max_samples: int = 2000) -> None:
        if max_samples < 10:
            raise ValueError("max_samples must be at least 10")
        self._values: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=max_samples)
        )

    def record(self, phase: str, milliseconds: float) -> None:
        if not phase.strip():
            raise ValueError("latency phase is required")
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError("latency must be a non-negative finite number")
        self._values[phase].append(float(milliseconds))

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for phase, samples in sorted(self._values.items()):
            ordered = sorted(samples)
            result[phase] = {
                "count": len(ordered),
                "p50_ms": round(_percentile(ordered, 0.50), 3),
                "p95_ms": round(_percentile(ordered, 0.95), 3),
                "p99_ms": round(_percentile(ordered, 0.99), 3),
                "max_ms": round(ordered[-1], 3),
            }
        return result


def _percentile(ordered: list[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]
