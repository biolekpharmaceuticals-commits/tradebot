from __future__ import annotations

import threading
import time
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .market_data import MarketDataError, MarketDataProvider, build_market_data_provider
from .safety import SafetyConfigError


INDEX_ALIASES = {
    "NIFTY": "NIFTY 50",
    "NIFTY50": "NIFTY 50",
    "NIFTY 50": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "BANK NIFTY": "NIFTY BANK",
    "NIFTY BANK": "NIFTY BANK",
}
REQUIRED_INDICES = ("NIFTY 50", "NIFTY BANK")


class DashboardMarketFeed(Protocol):
    def snapshot(self) -> dict[str, object]:
        ...


class DisabledDashboardMarketFeed:
    def snapshot(self) -> dict[str, object]:
        return {
            "status": "disabled",
            "refresh_seconds": 15,
            "timeframe": "ONE_MINUTE",
            "indices": [],
            "errors": [],
        }


class CachedIndexMarketFeed:
    """Read-only, rate-limited index candles for the private dashboard."""

    def __init__(
        self,
        *,
        provider_factory: Callable[[], MarketDataProvider],
        symbols: list[dict],
        refresh_seconds: int = 15,
        candle_limit: int = 120,
        timeframe: str = "ONE_MINUTE",
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.provider_factory = provider_factory
        self.provider = provider_factory()
        self.symbols = [dict(symbol) for symbol in symbols]
        self.refresh_seconds = int(refresh_seconds)
        self.candle_limit = int(candle_limit)
        self.timeframe = timeframe
        self.monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._cached: dict[str, object] | None = None
        self._cached_at: float | None = None

    def snapshot(self) -> dict[str, object]:
        now = self.monotonic()
        if self._cache_is_fresh(now):
            return dict(self._cached or {})

        with self._lock:
            now = self.monotonic()
            if self._cache_is_fresh(now):
                return dict(self._cached or {})

            indices: list[dict[str, object]] = []
            errors: list[dict[str, str]] = []
            for configured in self.symbols:
                symbol = dict(configured)
                symbol["timeframe"] = self.timeframe
                try:
                    candles = self._get_candles(symbol)
                    indices.append(_index_snapshot(symbol, candles, self.candle_limit))
                except MarketDataError:
                    errors.append(
                        {
                            "symbol": str(symbol.get("symbol", "UNKNOWN")),
                            "reason": "Market data temporarily unavailable",
                        }
                    )

            status = "available" if len(indices) == len(self.symbols) else "partial" if indices else "unavailable"
            payload: dict[str, object] = {
                "status": status,
                "refresh_seconds": self.refresh_seconds,
                "timeframe": self.timeframe,
                "source": "Angel One SmartAPI read-only market data",
                "as_of": max((str(item["timestamp"]) for item in indices), default=None),
                "indices": indices,
                "errors": errors,
            }
            if indices or self._cached is None:
                self._cached = payload
                self._cached_at = now
                return dict(payload)

            stale = dict(self._cached)
            stale["status"] = "stale"
            stale["errors"] = errors
            self._cached = stale
            self._cached_at = now
            return dict(stale)

    def _cache_is_fresh(self, now: float) -> bool:
        return (
            self._cached is not None
            and self._cached_at is not None
            and now - self._cached_at < self.refresh_seconds
        )

    def _get_candles(self, symbol: dict) -> pd.DataFrame:
        try:
            return self.provider.get_candles(symbol)
        except MarketDataError as exc:
            if "rate limit" in str(exc).lower():
                raise
            self.provider = self.provider_factory()
            return self.provider.get_candles(symbol)


def build_dashboard_market_feed(config) -> DashboardMarketFeed:
    settings = config.raw.get("market_dashboard") or {}
    if not settings.get("enabled", False):
        return DisabledDashboardMarketFeed()

    symbols = _configured_indices(config.section("trading").get("symbols", []))
    market_config = dict(config.section("market_data"))
    market_config["lookback_days"] = int(settings.get("lookback_days", 2))

    return CachedIndexMarketFeed(
        provider_factory=lambda: build_market_data_provider(market_config),
        symbols=symbols,
        refresh_seconds=int(settings.get("refresh_seconds", 15)),
        candle_limit=int(settings.get("candle_limit", 120)),
        timeframe=str(settings.get("timeframe", "ONE_MINUTE")),
    )


def validate_dashboard_market_config(
    config: object,
    trading_symbols: object,
    market_data_config: object | None = None,
) -> None:
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("market_dashboard must be a mapping")
    if not isinstance(config.get("enabled", False), bool):
        raise ValueError("market_dashboard.enabled must be a boolean")
    _bounded_int(config, "refresh_seconds", 10, 60, 15)
    _bounded_int(config, "candle_limit", 30, 300, 120)
    _bounded_int(config, "lookback_days", 1, 5, 2)
    if config.get("timeframe", "ONE_MINUTE") != "ONE_MINUTE":
        raise ValueError("market_dashboard.timeframe must remain ONE_MINUTE")
    if config.get("enabled", False):
        if not isinstance(market_data_config, dict) or market_data_config.get("provider") != "angel_one":
            raise ValueError("market_dashboard requires market_data.provider angel_one")
        names = {str(item.get("symbol", "")).strip().upper() for item in trading_symbols if isinstance(item, dict)}
        canonical = {INDEX_ALIASES[name] for name in names if name in INDEX_ALIASES}
        if canonical != set(REQUIRED_INDICES):
            raise ValueError("market_dashboard requires configured NIFTY 50 and NIFTY BANK indices")


def _configured_indices(symbols: object) -> list[dict]:
    found: dict[str, dict] = {}
    if isinstance(symbols, list):
        for item in symbols:
            if not isinstance(item, dict):
                continue
            name = str(item.get("symbol", "")).strip().upper()
            canonical = INDEX_ALIASES.get(name)
            if canonical:
                found[canonical] = {**item, "symbol": canonical, "instrument_type": "index"}
    missing = [name for name in REQUIRED_INDICES if name not in found]
    if missing:
        raise SafetyConfigError("Dashboard index configuration is incomplete")
    return [found[name] for name in REQUIRED_INDICES]


def _index_snapshot(symbol: dict, candles: pd.DataFrame, candle_limit: int) -> dict[str, object]:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if candles is None or candles.empty or not required.issubset(candles.columns):
        raise MarketDataError("Index candle response was empty")

    frame = candles.loc[:, ["timestamp", "open", "high", "low", "close", "volume"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna().sort_values("timestamp")
    if frame.empty:
        raise MarketDataError("Index candle response was empty")

    last = frame.iloc[-1]
    last_date = _local_date(last["timestamp"])
    dates = frame["timestamp"].map(_local_date)
    session = frame[dates == last_date]
    prior = frame[dates < last_date]
    baseline = float(prior.iloc[-1]["close"]) if not prior.empty else float(session.iloc[0]["open"])
    close = float(last["close"])
    change = close - baseline
    change_pct = change / baseline * 100 if baseline else 0.0
    visible = frame.tail(candle_limit)

    return {
        "symbol": str(symbol.get("symbol")),
        "timestamp": _iso(last["timestamp"]),
        "price": round(close, 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "open": round(float(session.iloc[0]["open"]), 2),
        "high": round(float(session["high"].max()), 2),
        "low": round(float(session["low"].min()), 2),
        "previous_close": round(baseline, 2),
        "price_action": _price_action(session),
        "candles": [
            {
                "timestamp": _iso(row["timestamp"]),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": int(row["volume"]),
            }
            for _, row in visible.iterrows()
        ],
    }


def _price_action(session: pd.DataFrame) -> str:
    last = session.iloc[-1]
    previous = session.iloc[:-1].tail(5)
    close = float(last["close"])
    if not previous.empty and close > float(previous["high"].max()):
        return "Bullish breakout"
    if not previous.empty and close < float(previous["low"].min()):
        return "Bearish breakdown"
    session_open = float(session.iloc[0]["open"])
    if close > session_open:
        return "Above session open"
    if close < session_open:
        return "Below session open"
    return "At session open"


def _local_date(value) -> object:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(ZoneInfo("Asia/Kolkata"))
    else:
        timestamp = timestamp.tz_convert(ZoneInfo("Asia/Kolkata"))
    return timestamp.date()


def _iso(value) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(ZoneInfo("Asia/Kolkata"))
    return timestamp.isoformat()


def _bounded_int(config: dict, key: str, minimum: int, maximum: int, default: int) -> None:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"market_dashboard.{key} must be an integer between {minimum} and {maximum}")
