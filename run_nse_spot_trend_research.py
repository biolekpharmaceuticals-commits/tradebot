from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from src.config import load_config
from src.nse_option_backtest import NSEFODailyArchive, load_nse_window
from src.spot_trend_research import run_walk_forward_research


def compact_result(result: dict[str, object] | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {key: value for key, value in result.items() if key != "trade_ledger"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean NSE spot-trend option strategy research")
    parser.add_argument("--config", default=os.getenv("TRADEBOT_CONFIG", "/etc/tradebot/config.yaml"))
    parser.add_argument("--calendar-days", type=int, default=100)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path("/opt/tradebot/logs/nse_fo_cache"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/opt/tradebot/logs/nse-spot-trend-research-100-days.json"),
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    option_config = config.section("option_selling")
    paper = config.section("paper_execution")
    archive = NSEFODailyArchive(args.cache_dir)
    daily_bars, coverage = load_nse_window(
        archive,
        end_date=args.end_date,
        calendar_days=args.calendar_days,
        minimum_sessions=45,
    )
    research = run_walk_forward_research(
        daily_bars,
        option_config,
        initial_balance=float(paper.get("initial_balance", 300000)),
        slippage_bps=float(paper.get("slippage_bps", 5)),
        fee_bps=float(paper.get("fee_bps", 10)),
    )
    report = {
        "source": "NSE F&O UDiFF Common Bhavcopy Final",
        "coverage": coverage,
        "research": research,
        "limitations": (
            "End-of-day research only. A passing result permits shadow implementation, not paper or live orders."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "coverage": coverage,
                "data_quality": {
                    key: value
                    for key, value in research["data_quality"].items()
                    if key != "rejected"
                },
                "training_period": research["training_period"],
                "holdout_period": research["holdout_period"],
                "candidate_count": research["candidate_count"],
                "training_leaderboard": [
                    {
                        "name": item["settings"]["name"],
                        **compact_result(item["result"]),
                    }
                    for item in research["training_results"]
                ],
                "selected_candidate": research["selected_candidate"],
                "selected_training_result": compact_result(research["selected_training_result"]),
                "holdout_result": compact_result(research["holdout_result"]),
                "combined_result": compact_result(research["combined_result"]),
                "deployment_gate_passed": research["deployment_gate_passed"],
                "deployment_recommendation": research["deployment_recommendation"],
                "gate_reasons": research["gate_reasons"],
                "report_file": str(args.output),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
