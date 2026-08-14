from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from src.config import load_config
from src.historical_backtest import build_historical_backtest_report, maximum_history_days
from src.market_data import AngelOneMarketDataProvider, build_market_data_provider


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only maximum-window strategy backtest")
    parser.add_argument("--config", default=os.getenv("TRADEBOT_CONFIG", "/etc/tradebot/config.yaml"))
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--symbol", action="append", dest="symbols")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    configured = [dict(item) for item in config.section("trading").get("symbols", [])]
    if args.symbols:
        requested = {name.strip().upper() for name in args.symbols}
        selected = [item for item in configured if str(item.get("symbol", "")).upper() in requested]
    else:
        selected = [item for item in configured if str(item.get("instrument_type", "")).lower() == "index"]
    if not selected:
        raise ValueError("No matching configured index symbols were found")

    maximum = min(maximum_history_days(str(item.get("timeframe", ""))) for item in selected)
    days = maximum if args.days is None else args.days
    if days < 1 or days > maximum:
        raise ValueError(f"Requested history must be between 1 and {maximum} days")

    provider = build_market_data_provider(config.section("market_data"))
    if not isinstance(provider, AngelOneMarketDataProvider):
        raise ValueError("Maximum-window backtest requires market_data.provider angel_one")

    report = build_historical_backtest_report(config, provider, selected, days=days)
    rendered = json.dumps(report, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
