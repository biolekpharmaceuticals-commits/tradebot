from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .live_market import DashboardMarketFeed, DisabledDashboardMarketFeed


SENSITIVE_KEYS = {
    "api_key",
    "client_code",
    "pin",
    "password",
    "totp_secret",
    "access_token",
    "refresh_token",
    "feed_token",
    "jwt_token",
    "authorization",
    "cookie",
}


class DecisionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        if not self.path.exists() or not self.path.is_file():
            return []

        records: deque[dict[str, Any]] = deque(maxlen=limit)
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(_sanitize(record))
        return list(reversed(records))


def create_app(
    decision_log: Path | None = None,
    market_feed: DashboardMarketFeed | None = None,
) -> FastAPI:
    web_dir = Path(__file__).resolve().parent / "web"
    log_path = decision_log or Path(os.getenv("TRADEBOT_DECISION_LOG", "logs/decisions.jsonl"))
    store = DecisionStore(log_path)
    live_market = market_feed or DisabledDashboardMarketFeed()
    app = FastAPI(
        title="Tradebot Paper Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        records = store.read(1)
        latest = records[0] if records else {}
        return {
            "status": "ok",
            "records_available": bool(records),
            "mode": latest.get("mode", "paper"),
            "live_trading_enabled": latest.get("live_trading_enabled", False),
            "kill_switch_active": latest.get("kill_switch_active", True),
            "auto_paper_trading_enabled": latest.get("auto_paper_trading_enabled", False),
            "paper_execution_configured": latest.get("paper_execution_configured", False),
        }

    @app.get("/api/latest")
    def latest() -> dict[str, Any]:
        records = store.read(1)
        return records[0] if records else {}

    @app.get("/api/decisions")
    def decisions(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
        records = store.read(limit)
        return {"count": len(records), "decisions": records}

    @app.get("/api/market")
    def market() -> dict[str, Any]:
        return _sanitize(live_market.snapshot())

    return app


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_KEYS or "secret" in normalized or normalized.endswith("_token"):
                continue
            clean[str(key)] = _sanitize(item)
        return clean
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value
