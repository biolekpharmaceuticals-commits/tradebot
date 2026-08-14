from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.broker import PaperBroker
from src.config import load_config
from src.dashboard import create_app
from src.live_market import CachedIndexMarketFeed, validate_dashboard_market_config


SYMBOLS = [
    {
        "exchange": "NSE",
        "symbol": "NIFTY 50",
        "token": "99926000",
        "quantity": 1,
        "timeframe": "FIVE_MINUTE",
        "instrument_type": "index",
    },
    {
        "exchange": "NSE",
        "symbol": "NIFTY BANK",
        "token": "99926009",
        "quantity": 1,
        "timeframe": "FIVE_MINUTE",
        "instrument_type": "index",
    },
]


def candle_frame(base: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-08-12T15:29:00+05:30",
                    "2026-08-13T09:15:00+05:30",
                    "2026-08-13T09:16:00+05:30",
                    "2026-08-13T09:17:00+05:30",
                ]
            ),
            "open": [base - 10, base, base + 2, base + 4],
            "high": [base, base + 5, base + 7, base + 12],
            "low": [base - 15, base - 2, base, base + 3],
            "close": [base - 5, base + 2, base + 4, base + 11],
            "volume": [0, 0, 0, 0],
        }
    )


class CountingProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        self.calls.append(dict(symbol_config))
        base = 24400 if symbol_config["symbol"] == "NIFTY 50" else 55800
        return candle_frame(base)


def test_live_index_feed_returns_price_action_and_caches_both_indices():
    provider = CountingProvider()
    now = [100.0]
    feed = CachedIndexMarketFeed(
        provider_factory=lambda: provider,
        symbols=SYMBOLS,
        refresh_seconds=15,
        candle_limit=60,
        monotonic=lambda: now[0],
    )

    first = feed.snapshot()
    second = feed.snapshot()

    assert first == second
    assert first["status"] == "available"
    assert [item["symbol"] for item in first["indices"]] == ["NIFTY 50", "NIFTY BANK"]
    assert len(provider.calls) == 2
    assert all(call["timeframe"] == "ONE_MINUTE" for call in provider.calls)
    nifty = first["indices"][0]
    assert nifty["price"] == 24411
    assert nifty["previous_close"] == 24395
    assert nifty["change"] == 16
    assert nifty["price_action"] == "Bullish breakout"
    assert len(nifty["candles"]) == 4

    now[0] += 16
    feed.snapshot()
    assert len(provider.calls) == 4


class SecretMarketFeed:
    def snapshot(self):
        return {
            "status": "available",
            "access_token": "SENTINEL_TOKEN",
            "indices": [{"symbol": "NIFTY 50", "price": 24400}],
            "errors": [],
        }


def test_market_dashboard_api_is_sanitized_and_get_only(tmp_path):
    log = tmp_path / "decisions.jsonl"
    log.write_text(json.dumps({"mode": "paper", "live_trading_enabled": False}) + "\n", encoding="utf-8")
    client = TestClient(create_app(log, market_feed=SecretMarketFeed()))

    response = client.get("/api/market")

    assert response.status_code == 200
    assert response.json()["indices"][0]["symbol"] == "NIFTY 50"
    assert "SENTINEL" not in response.text
    assert client.post("/api/market").status_code == 405
    index = client.get("/").text
    assert "nifty50Chart" in index
    assert "bankNiftyChart" in index


def test_market_dashboard_settings_are_bounded_and_require_both_indices():
    settings = {
        "enabled": True,
        "refresh_seconds": 15,
        "timeframe": "ONE_MINUTE",
        "candle_limit": 120,
        "lookback_days": 2,
    }
    validate_dashboard_market_config(settings, SYMBOLS, {"provider": "angel_one"})

    with pytest.raises(ValueError, match="requires configured"):
        validate_dashboard_market_config(settings, SYMBOLS[:1], {"provider": "angel_one"})
    with pytest.raises(ValueError, match="refresh_seconds"):
        validate_dashboard_market_config({**settings, "refresh_seconds": 1}, SYMBOLS, {"provider": "angel_one"})
    with pytest.raises(ValueError, match="ONE_MINUTE"):
        validate_dashboard_market_config(
            {**settings, "timeframe": "FIVE_MINUTE"}, SYMBOLS, {"provider": "angel_one"}
        )
    with pytest.raises(ValueError, match="provider angel_one"):
        validate_dashboard_market_config(settings, SYMBOLS, {"provider": "demo"})


def test_release52_example_uses_three_lakh_paper_and_risk_capital(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "true")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "false")
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")

    assert config.section("paper_execution")["initial_balance"] == 300000
    assert config.section("risk")["capital"] == 300000
    assert config.section("market_dashboard")["refresh_seconds"] == 15


def test_three_lakh_balance_preserves_existing_realized_pnl(tmp_path):
    state = {
        "version": 1,
        "initial_balance": 100000,
        "realized_pnl": 1250.5,
        "open_positions": [],
        "closed_positions": [],
        "orders": [],
    }
    path = tmp_path / "paper.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    snapshot = PaperBroker(state_path=path, initial_balance=300000).snapshot()

    assert snapshot["starting_balance"] == 300000
    assert snapshot["paper_balance"] == 301250.5
    assert snapshot["realized_pnl"] == 1250.5


def test_dashboard_service_loads_configured_read_only_credentials():
    service = (Path(__file__).resolve().parents[1] / "deploy" / "tradebot-dashboard.service").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=/etc/tradebot/tradebot.env" in service
    assert "--config /etc/tradebot/config.yaml" in service
    assert "--host 127.0.0.1" in service
    assert "WorkingDirectory=/opt/tradebot" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/opt/tradebot/logs" in service
