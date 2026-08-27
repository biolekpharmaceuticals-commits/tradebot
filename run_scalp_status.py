from __future__ import annotations

import argparse
import json
from collections import deque
from datetime import datetime
from pathlib import Path

from src.config import load_config
from src.scalp_shadow import KOLKATA, SCALP_RELEASE, Tick, load_scalp_shadow_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Release 6.1 scalp-shadow status")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument("--events", type=int, default=10, help="Number of recent audit events")
    args = parser.parse_args()
    if not 1 <= args.events <= 100:
        raise ValueError("--events must be between 1 and 100")

    config = load_config(Path(args.config))
    settings = load_scalp_shadow_settings(config.raw.get("scalp_shadow"), config.base_dir)
    latest_tick = _latest_tick(settings.tick_log_dir)
    events = _latest_json_lines(settings.decision_log, args.events)
    portfolio = _read_mapping(settings.state_file)
    now = datetime.now(KOLKATA)
    market_window = now.weekday() < 5 and 9 <= now.hour <= 15
    age_seconds = (
        round((now - latest_tick.received_at).total_seconds(), 3) if latest_tick is not None else None
    )
    if not settings.enabled:
        status = "disabled"
    elif latest_tick is None:
        status = "waiting_for_first_tick"
    elif market_window and age_seconds is not None and age_seconds > settings.max_tick_age_ms / 1000:
        status = "stale"
    else:
        status = "ok"

    print(
        json.dumps(
            {
                "status": status,
                "release": SCALP_RELEASE,
                "mode": "paper_shadow",
                "live_orders_available": False,
                "paper_execution_enabled": settings.paper_execution_enabled,
                "kill_switch_active": config.safety.kill_switch_active,
                "auto_paper_trading_enabled": config.safety.auto_paper_trading_enabled,
                "latest_tick": latest_tick.public_record() if latest_tick else None,
                "latest_tick_age_seconds": age_seconds,
                "portfolio": portfolio,
                "recent_events": events,
            },
            indent=2,
            default=str,
        )
    )


def _latest_tick(directory: Path) -> Tick | None:
    files = sorted(directory.glob("ticks-*.jsonl")) if directory.exists() else []
    if not files:
        return None
    records = _latest_json_lines(files[-1], 1)
    return Tick.from_record(records[0]) if records else None


def _latest_json_lines(path: Path, limit: int) -> list[dict]:
    if not path.exists() or not path.is_file():
        return []
    records: deque[dict] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return list(reversed(records))


def _read_mapping(path: Path) -> dict | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unreadable"}
    return value if isinstance(value, dict) else {"status": "invalid"}


if __name__ == "__main__":
    main()
