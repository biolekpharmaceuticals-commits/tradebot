from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from src.config import load_config
from src.scalp_shadow import (
    ScalpShadowEngine,
    Tick,
    load_scalp_shadow_settings,
    replay_ticks,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay recorded Release 6.1 Angel One scalp ticks")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument("--ticks", required=True, nargs="+", help="Tick JSONL files")
    parser.add_argument("--output", required=True, help="Backtest report JSON")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    settings = load_scalp_shadow_settings(config.raw.get("scalp_shadow"), config.base_dir)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = replace(
        settings,
        paper_execution_enabled=True,
        state_file=output.with_suffix(".state.json"),
        decision_log=output.with_suffix(".decisions.jsonl"),
        tick_log_dir=output.parent / "replay-ticks-disabled",
    )
    ticks = _load_ticks([Path(item) for item in args.ticks])
    execution_tokens = {tick.token for tick in ticks if tick.role == "execution"}
    if len(execution_tokens) != 1:
        raise ValueError("Replay requires exactly one execution contract token")
    clock = [ticks[0].received_at]
    engine = ScalpShadowEngine(
        settings,
        execution_token=next(iter(execution_tokens)),
        kill_switch_active=False,
        auto_paper_enabled=True,
        clock=lambda: clock[0],
        record_ticks=False,
    )
    report = replay_ticks(engine, ticks, advance_clock=lambda value: clock.__setitem__(0, value))
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


def _load_ticks(paths: list[Path]) -> list[Tick]:
    ticks = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    ticks.append(Tick.from_record(json.loads(line)))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    raise ValueError(f"Malformed tick record at {path}:{line_number}") from None
    if not ticks:
        raise ValueError("No tick records were loaded")
    return ticks


if __name__ == "__main__":
    main()
