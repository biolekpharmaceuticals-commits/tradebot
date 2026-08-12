from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from run_dashboard import validate_dashboard_host
from src.bulk_deals import (
    BulkDealSignal,
    DisabledBulkDealProvider,
    NSEBulkDealProvider,
    build_bulk_deal_provider,
)
from src.dashboard import DecisionStore, create_app
from src.demo_data import demo_candles
from src.news import NewsSignal
from src.strategy import TrendStrategy


def bulk_response(*rows, as_on_date="07-Aug-2026"):
    return {"as_on_date": as_on_date, "BULK_DEALS_DATA": list(rows)}


def deal(side, quantity, *, symbol="SBIN", client="PUBLIC CLIENT", price="100"):
    return {
        "buySell": side,
        "clientName": client,
        "date": "07-Aug-2026",
        "name": "State Bank of India",
        "qty": str(quantity),
        "remarks": "-",
        "symbol": symbol,
        "watp": price,
    }


def test_bulk_deal_provider_uses_only_matching_symbol_and_caps_points():
    provider = NSEBulkDealProvider(
        fetcher=lambda: bulk_response(
            deal("BUY", 900),
            deal("SELL", 100),
            deal("BUY", 999999, symbol="OTHER"),
        ),
        today=lambda: date(2026, 8, 10),
        max_confidence_points=10,
    )

    result = provider.get_signal("SBIN-EQ")

    assert result.status == "available"
    assert result.direction == "Accumulation"
    assert 0 < result.score <= 10
    assert result.buy_quantity == 900
    assert result.sell_quantity == 100
    assert result.deal_count == 2
    assert all(item["client_name"] == "PUBLIC CLIENT" for item in result.deals)


def test_stale_bulk_deals_are_neutral_and_contribute_no_points():
    provider = NSEBulkDealProvider(
        fetcher=lambda: bulk_response(deal("SELL", 1000), as_on_date="01-Aug-2026"),
        today=lambda: date(2026, 8, 10),
        max_age_days=3,
    )

    result = provider.get_signal("SBIN")

    assert result.status == "stale"
    assert result.stale is True
    assert result.score == 0
    assert result.direction == "Neutral"


def test_bulk_deal_failure_is_sanitized_and_fail_soft():
    def fail():
        raise RuntimeError("SENTINEL_SECRET should never escape")

    result = NSEBulkDealProvider(fetcher=fail).get_signal("SBIN")

    assert result.status == "unavailable"
    assert result.score == 0
    assert "SENTINEL" not in result.explanation
    assert "SECRET" not in result.explanation


def test_bulk_deals_default_disabled_and_provider_validation():
    assert isinstance(build_bulk_deal_provider({}), DisabledBulkDealProvider)
    with pytest.raises(ValueError, match="provider must be nse"):
        build_bulk_deal_provider({"enabled": True, "provider": "unknown"})


def test_strategy_records_indicator_values_and_point_breakdown():
    bulk = BulkDealSignal(
        status="available",
        direction="Accumulation",
        score=60,
        deal_count=1,
        buy_quantity=1000,
        sell_quantity=0,
        net_quantity=1000,
        latest_date="07-Aug-2026",
        stale=False,
        explanation="Published accumulation",
        source="https://www.nseindia.com/market-data/large-deals",
        deals=[],
    )
    strategy = TrendStrategy(
        {
            "ema_fast": 9,
            "ema_slow": 21,
            "ema_trend": 50,
            "rsi_period": 14,
            "atr_period": 14,
            "min_reward_risk": 1.5,
        }
    )

    signal = strategy.evaluate(demo_candles(), NewsSignal(0, "Low", [], "No news"), bulk)

    explanation = signal.strategy
    assert explanation["name"] == "EMA + VWAP + RSI + MACD + News + Bulk Deals"
    assert explanation["version"] == "4.0"
    assert set(explanation["indicators"]) >= {"ema_fast", "ema_slow", "ema_trend", "vwap", "rsi", "macd", "atr"}
    factors = explanation["factors"]
    assert len(factors) == 6
    assert next(item for item in factors if item["name"] == "NSE bulk deals")["points"] == 10
    assert explanation["raw_edge"] == explanation["bullish_points"] - explanation["bearish_points"]


def test_decision_store_skips_bad_lines_and_removes_secrets(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps(
            {
                "symbol": "SBIN-EQ",
                "api_key": "SENTINEL_API_KEY",
                "nested": {"access_token": "SENTINEL_TOKEN", "safe": "visible"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = DecisionStore(path).read(10)

    assert records == [{"symbol": "SBIN-EQ", "nested": {"safe": "visible"}}]
    assert "SENTINEL" not in json.dumps(records)


def test_dashboard_is_get_only_and_returns_paper_safety_state(tmp_path):
    path = tmp_path / "decisions.jsonl"
    record = {
        "timestamp": "2026-08-10T04:00:00+00:00",
        "symbol": "SBIN-EQ",
        "mode": "paper",
        "live_trading_enabled": False,
        "kill_switch_active": True,
        "executed": False,
        "signal": {"decision": "SELL", "confidence": 19, "strategy": {"factors": []}},
        "bulk_deals": {"status": "no_match", "score": 0, "deals": []},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    client = TestClient(create_app(path))

    assert client.get("/").status_code == 200
    latest = client.get("/api/latest")
    assert latest.status_code == 200
    assert latest.json()["mode"] == "paper"
    health = client.get("/api/health").json()
    assert health == {
        "status": "ok",
        "records_available": True,
        "mode": "paper",
        "live_trading_enabled": False,
        "kill_switch_active": True,
        "auto_paper_trading_enabled": False,
        "paper_execution_configured": False,
    }
    assert client.post("/api/latest").status_code == 405
    assert client.post("/api/decisions").status_code == 405


def test_dashboard_refuses_public_network_bind():
    assert validate_dashboard_host("127.0.0.1") == "127.0.0.1"
    assert validate_dashboard_host("localhost") == "localhost"
    with pytest.raises(ValueError, match="loopback"):
        validate_dashboard_host("0.0.0.0")


def test_release3_sources_contain_no_order_or_write_api_calls():
    repo = Path(__file__).resolve().parents[1]
    source = "\n".join(path.read_text(encoding="utf-8") for path in (repo / "src").glob("*.py"))
    for forbidden in (
        "placeOrder",
        "placeOrderFullResponse",
        "modifyOrder",
        "cancelOrder",
        "gttCreateRule",
        "gttModifyRule",
        "gttCancelRule",
    ):
        assert forbidden not in source

    bulk_source = (repo / "src" / "bulk_deals.py").read_text(encoding="utf-8")
    assert ".post(" not in bulk_source
    dashboard_source = (repo / "src" / "dashboard.py").read_text(encoding="utf-8")
    assert "@app.post" not in dashboard_source


def test_runtime_requirements_include_smartapi_transitive_dependencies():
    repo = Path(__file__).resolve().parents[1]
    requirements = (repo / "requirements.txt").read_text(encoding="utf-8")
    assert "logzero" in requirements
    assert "websocket-client" in requirements
