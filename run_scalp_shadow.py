from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import load_config
from src.scalp_stream import AngelOneScalpRuntime

KOLKATA = ZoneInfo("Asia/Kolkata")
MARKET_START = time(9, 15)
MARKET_STOP = time(15, 31)


def market_session_open(now: datetime) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=KOLKATA)
    local = now.astimezone(KOLKATA)
    return local.weekday() < 5 and MARKET_START <= local.time().replace(tzinfo=None) < MARKET_STOP


def main() -> None:
    parser = argparse.ArgumentParser(description="Release 6 paper-only NIFTY futures scalp shadow")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Authenticate, resolve instruments and validate configuration without opening the stream",
    )
    parser.add_argument(
        "--market-open-check",
        action="store_true",
        help="Exit successfully only during the guarded NSE scalp service window",
    )
    args = parser.parse_args()

    if args.market_open_check:
        raise SystemExit(0 if market_session_open(datetime.now(KOLKATA)) else 1)

    config = load_config(Path(args.config))
    runtime = AngelOneScalpRuntime(config)
    manifest = runtime.prepare()
    print(json.dumps(manifest, indent=2, default=str))
    if not args.prepare_only:
        runtime.run_forever()


if __name__ == "__main__":
    main()
