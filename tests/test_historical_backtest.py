from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.historical_backtest import fetch_historical_candles, maximum_history_days


class RangeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[datetime, datetime]] = []

    def get_candles_between(self, symbol_config, start, end):
        self.calls.append((start, end))
        return pd.DataFrame(
            {
                "timestamp": [start, end],
                "open": [100, 101],
                "high": [102, 103],
                "low": [99, 100],
                "close": [101, 102],
                "volume": [0, 0],
            }
        )


def test_five_minute_history_uses_official_100_day_maximum_in_chunks():
    provider = RangeProvider()
    end = datetime(2026, 8, 14, 15, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

    frame = fetch_historical_candles(
        provider,
        {"timeframe": "FIVE_MINUTE"},
        days=100,
        end=end,
    )

    assert maximum_history_days("FIVE_MINUTE") == 100
    assert len(provider.calls) == 4
    assert provider.calls[0][0] == end.replace() - pd.Timedelta(days=100)
    assert provider.calls[-1][1] == end
    assert frame["timestamp"].is_monotonic_increasing


def test_history_rejects_ranges_above_interval_limit():
    with pytest.raises(ValueError, match="between 1 and 100"):
        fetch_historical_candles(
            RangeProvider(),
            {"timeframe": "FIVE_MINUTE"},
            days=101,
        )
