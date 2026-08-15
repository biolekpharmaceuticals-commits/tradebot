from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from src.nse_option_backtest import OptionDailyBar
from src.spot_trend_research import (
    RESEARCH_CANDIDATES,
    SpotTrendSettings,
    _validation_gate_reasons,
    build_spot_trends,
    extract_clean_spot_closes,
)


def option_bar(day: date, symbol: str, underlying: float) -> OptionDailyBar:
    return OptionDailyBar(
        trade_date=day,
        symbol=symbol,
        expiry=day + timedelta(days=7),
        strike=round(underlying / 50) * 50,
        option_type="call",
        open=10,
        high=12,
        low=8,
        close=9,
        settlement=9,
        underlying=underlying,
        open_interest=2000,
        change_in_open_interest=100,
        volume=500,
        lot_size=10,
    )


def test_clean_spot_series_uses_median_and_rejects_dispersion():
    first = date(2026, 1, 1)
    daily = {
        first: [option_bar(first, "NIFTY", 100), option_bar(first, "NIFTY", 100.01)],
        first + timedelta(days=1): [
            option_bar(first + timedelta(days=1), "NIFTY", 101),
            option_bar(first + timedelta(days=1), "NIFTY", 110),
        ],
    }

    clean, quality = extract_clean_spot_closes(daily, maximum_dispersion_bps=5)

    assert len(clean["NIFTY"]) == 1
    assert clean["NIFTY"][0][1] == 100.005
    assert quality["rejected_count"] >= 1
    assert any(item["reason"] == "underlying_dispersion" for item in quality["rejected"])


def test_spot_trend_requires_history_and_classifies_rising_market_bullish():
    start = date(2026, 1, 1)
    closes = {"NIFTY": [(start + timedelta(days=index), 100 + index) for index in range(25)]}
    settings = replace(
        RESEARCH_CANDIDATES[0],
        ema_fast=3,
        ema_slow=5,
        momentum_days=3,
        volatility_lookback=4,
        minimum_volatility_pct=0,
        maximum_volatility_pct=100,
    )

    trends = build_spot_trends(closes, settings)

    assert (start + timedelta(days=3), "NIFTY") not in trends
    latest = trends[(start + timedelta(days=24), "NIFTY")]
    assert latest["trend"] == "bullish"
    assert latest["tradable"] is True


def test_spot_trend_classifies_falling_market_bearish():
    start = date(2026, 1, 1)
    closes = {"NIFTY": [(start + timedelta(days=index), 150 - index) for index in range(25)]}
    settings = replace(
        RESEARCH_CANDIDATES[0],
        ema_fast=3,
        ema_slow=5,
        momentum_days=3,
        volatility_lookback=4,
        minimum_volatility_pct=0,
        maximum_volatility_pct=100,
    )

    trends = build_spot_trends(closes, settings)

    assert trends[(start + timedelta(days=24), "NIFTY")]["trend"] == "bearish"


def test_holdout_gate_rejects_loss_and_too_few_trades():
    validation = {"trades": 2, "net_pnl": -100, "profit_factor": 0.5, "max_drawdown_pct": 0.5}
    combined = {"net_pnl": -50}

    reasons = _validation_gate_reasons(validation, combined)

    assert "Holdout contains fewer than four trades" in reasons
    assert "Holdout net P&L is not positive" in reasons
    assert "Combined net P&L is not positive" in reasons


def test_research_candidates_never_allow_iron_fly_or_naked_options():
    assert all(candidate.wing_width_strikes >= 1 for candidate in RESEARCH_CANDIDATES)
    assert all(isinstance(candidate, SpotTrendSettings) for candidate in RESEARCH_CANDIDATES)

