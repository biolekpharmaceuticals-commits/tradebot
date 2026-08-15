from __future__ import annotations

import math
from datetime import date, datetime
from statistics import pstdev
from zoneinfo import ZoneInfo

import pandas as pd


FROZEN_NIFTY_SPOT_TREND = {
    "underlyings": ["NIFTY 50"],
    "timeframe": "ONE_DAY",
    "lookback_days": 30,
    "ema_fast": 5,
    "ema_slow": 20,
    "momentum_days": 5,
    "momentum_threshold_pct": 0.15,
    "volatility_lookback": 10,
    "minimum_volatility_pct": 5,
    "maximum_volatility_pct": 35,
    "allow_range_condor": True,
}


def evaluate_daily_spot_trend(
    candles: pd.DataFrame,
    config: dict,
    *,
    today: date | None = None,
) -> dict[str, object]:
    today = today or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    required = {"timestamp", "close"}
    if candles is None or candles.empty or not required <= set(candles.columns):
        return _blocked("Daily spot candle data is unavailable")

    frame = candles.loc[:, ["timestamp", "close"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna().sort_values("timestamp")
    if frame.empty:
        return _blocked("Daily spot candle data is invalid")

    timezone = ZoneInfo("Asia/Kolkata")
    timestamps = frame["timestamp"]
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(timezone)
    else:
        timestamps = timestamps.dt.tz_convert(timezone)
    frame["session"] = timestamps.dt.date
    frame = frame[frame["session"] < today]
    closes = frame.groupby("session", sort=True)["close"].last()

    ema_fast = int(config.get("ema_fast", 5))
    ema_slow = int(config.get("ema_slow", 20))
    momentum_days = int(config.get("momentum_days", 5))
    volatility_lookback = int(config.get("volatility_lookback", 10))
    minimum_history = max(ema_slow, momentum_days + 1, volatility_lookback + 1)
    if len(closes) < minimum_history:
        return _blocked(
            f"At least {minimum_history} completed daily spot sessions are required",
            completed_sessions=len(closes),
        )

    latest = float(closes.iloc[-1])
    fast_value = float(closes.ewm(span=ema_fast, adjust=False).mean().iloc[-1])
    slow_value = float(closes.ewm(span=ema_slow, adjust=False).mean().iloc[-1])
    momentum_pct = (latest / float(closes.iloc[-momentum_days - 1]) - 1) * 100
    recent = closes.iloc[-volatility_lookback - 1 :].astype(float).tolist()
    log_returns = [math.log(current / previous) for previous, current in zip(recent, recent[1:])]
    realized_volatility = pstdev(log_returns) * math.sqrt(252) * 100
    minimum_volatility = float(config.get("minimum_volatility_pct", 5))
    maximum_volatility = float(config.get("maximum_volatility_pct", 35))
    tradable = minimum_volatility <= realized_volatility <= maximum_volatility
    threshold = float(config.get("momentum_threshold_pct", 0.15))

    if latest > slow_value and fast_value > slow_value and momentum_pct >= threshold:
        regime = "bullish"
        allowed_structure = "bull_put_credit_spread"
    elif latest < slow_value and fast_value < slow_value and momentum_pct <= -threshold:
        regime = "bearish"
        allowed_structure = "bear_call_credit_spread"
    else:
        regime = "range"
        allowed_structure = "iron_condor" if config.get("allow_range_condor") is True else None

    blockers = []
    if not tradable:
        blockers.append("Realized volatility is outside the validated 5% to 35% range")
    if allowed_structure is None:
        blockers.append("The detected regime has no validated structure")
    return {
        "enabled": True,
        "eligible": not blockers,
        "regime": regime,
        "allowed_structure": allowed_structure,
        "signal_session": closes.index[-1].isoformat(),
        "completed_sessions": len(closes),
        "spot_close": round(latest, 2),
        "ema_fast": round(fast_value, 2),
        "ema_slow": round(slow_value, 2),
        "momentum_pct": round(momentum_pct, 4),
        "realized_volatility_pct": round(realized_volatility, 4),
        "blockers": blockers,
    }


def validate_spot_trend_config(config: object, structures: set[str]) -> None:
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("option_selling.spot_trend must be a mapping")
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("option_selling.spot_trend.enabled must be a boolean")
    if not enabled:
        return
    for key, expected in FROZEN_NIFTY_SPOT_TREND.items():
        if config.get(key) != expected:
            raise ValueError(f"option_selling.spot_trend.{key} must remain {expected!r}")
    if structures != {"credit_spread", "iron_condor"}:
        raise ValueError(
            "option_selling trend deployment permits only credit_spread and iron_condor"
        )


def _blocked(reason: str, *, completed_sessions: int = 0) -> dict[str, object]:
    return {
        "enabled": True,
        "eligible": False,
        "regime": None,
        "allowed_structure": None,
        "signal_session": None,
        "completed_sessions": completed_sessions,
        "blockers": [reason],
    }
