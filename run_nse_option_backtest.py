from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import load_config
from src.nse_option_backtest import (
    NSEFODailyArchive,
    build_nse_option_backtest_report,
    load_nse_window,
    render_summary,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="NSE EOD OI defined-risk option strategy backtest")
    parser.add_argument("--config", default=os.getenv("TRADEBOT_CONFIG", "/etc/tradebot/config.yaml"))
    parser.add_argument("--calendar-days", type=int, default=100)
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo("Asia/Kolkata")).date(),
    )
    parser.add_argument("--cache-dir", type=Path, default=Path("/opt/tradebot/logs/nse_fo_cache"))
    parser.add_argument("--output", type=Path, default=Path("/opt/tradebot/logs/nse-option-backtest-100-days.json"))
    parser.add_argument("--minimum-sessions", type=int, default=45)
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    option_config = config.section("option_selling")
    if option_config.get("enabled") is not True:
        raise ValueError("option_selling must be enabled")
    if option_config.get("shadow_mode") is not True or option_config.get("defined_risk_only") is not True:
        raise ValueError("NSE option backtest requires shadow_mode and defined_risk_only")

    paper = config.section("paper_execution")
    archive = NSEFODailyArchive(args.cache_dir)
    daily_bars, coverage = load_nse_window(
        archive,
        end_date=args.end_date,
        calendar_days=args.calendar_days,
        minimum_sessions=args.minimum_sessions,
    )
    report = build_nse_option_backtest_report(
        daily_bars,
        coverage,
        option_config,
        initial_balance=float(paper.get("initial_balance", 300000)),
        slippage_bps=float(paper.get("slippage_bps", 5)),
        fee_bps=float(paper.get("fee_bps", 10)),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    if args.print_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_summary(report, args.output))


if __name__ == "__main__":
    main()
