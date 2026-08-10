from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from src.dashboard import create_app


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def validate_dashboard_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized not in LOOPBACK_HOSTS:
        raise ValueError("Dashboard must bind to a loopback address and be published through the HTTPS proxy")
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only paper-trading dashboard")
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8000")))
    parser.add_argument(
        "--decision-log",
        default=os.getenv("TRADEBOT_DECISION_LOG", "logs/decisions.jsonl"),
    )
    args = parser.parse_args()

    host = validate_dashboard_host(args.host)
    if args.port < 1024 or args.port > 65535:
        raise ValueError("Dashboard port must be between 1024 and 65535")

    app = create_app(Path(args.decision_log))
    uvicorn.run(app, host=host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
