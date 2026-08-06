from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd


def demo_candles() -> pd.DataFrame:
    start = datetime(2026, 6, 6, 9, 15)
    rows = []
    price = 820.0

    for index in range(80):
        drift = 0.55 if index > 25 else 0.12
        wobble = ((index % 7) - 3) * 0.18
        open_price = price
        close = price + drift + wobble
        high = max(open_price, close) + 1.2
        low = min(open_price, close) - 0.9
        volume = 150000 + index * 1800
        rows.append(
            {
                "timestamp": start + timedelta(minutes=5 * index),
                "open": round(open_price, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "volume": volume,
            }
        )
        price = close

    return pd.DataFrame(rows)
