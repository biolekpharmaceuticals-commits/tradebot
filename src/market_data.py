from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .demo_data import demo_candles
from .safety import SafetyConfigError


class MarketDataError(RuntimeError):
    """Raised when read-only market-data retrieval fails closed."""


class MarketDataProvider(Protocol):
    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        ...


class DemoMarketDataProvider:
    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        return demo_candles()


class AngelOneMarketDataProvider:
    REQUIRED_ENV = (
        "ANGEL_ONE_API_KEY",
        "ANGEL_ONE_CLIENT_CODE",
        "ANGEL_ONE_PIN",
        "ANGEL_ONE_TOTP_SECRET",
    )

    def __init__(
        self,
        lookback_days: int = 5,
        request_interval_seconds: float = 1.2,
        max_retries: int = 2,
        retry_backoff_seconds: float = 3.0,
        client_factory: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if lookback_days < 1 or lookback_days > 30:
            raise MarketDataError("market_data.lookback_days must be between 1 and 30")
        if request_interval_seconds < 0.5 or request_interval_seconds > 10:
            raise MarketDataError("market_data.request_interval_seconds must be between 0.5 and 10")
        if max_retries < 0 or max_retries > 5:
            raise MarketDataError("market_data.max_retries must be between 0 and 5")
        if retry_backoff_seconds < 1 or retry_backoff_seconds > 30:
            raise MarketDataError("market_data.retry_backoff_seconds must be between 1 and 30")
        self.lookback_days = lookback_days
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.client_factory = client_factory or self._default_client_factory
        self.clock = clock or self._now_kolkata
        self.sleeper = sleeper or time.sleep
        self.monotonic = monotonic or time.monotonic
        self._client = None
        self._last_request_at: float | None = None

    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        client = self._authenticated_client()
        params = self._candle_params(symbol_config)
        for attempt in range(self.max_retries + 1):
            try:
                self._respect_rate_limit()
                with _suppress_smartapi_logging():
                    response = client.getCandleData(params)
            except Exception as exc:
                if _is_rate_limit_exception(exc):
                    if attempt >= self.max_retries:
                        raise MarketDataError("Angel One market-data rate limit reached") from None
                    self.sleeper(self.retry_backoff_seconds * (attempt + 1))
                    continue
                raise MarketDataError("Angel One candle retrieval failed") from None

            if not _is_rate_limited(response):
                return self._candles_to_frame(response)
            if attempt >= self.max_retries:
                raise MarketDataError("Angel One market-data rate limit reached")
            self.sleeper(self.retry_backoff_seconds * (attempt + 1))

        raise MarketDataError("Angel One candle retrieval failed")

    def _authenticated_client(self):
        if self._client is not None:
            return self._client

        try:
            values = self._read_environment()
            with _suppress_smartapi_logging():
                client = self.client_factory(values["ANGEL_ONE_API_KEY"])
            import pyotp

            totp_value = pyotp.TOTP(values["ANGEL_ONE_TOTP_SECRET"]).now()
            with _suppress_smartapi_logging():
                response = client.generateSession(
                    values["ANGEL_ONE_CLIENT_CODE"],
                    values["ANGEL_ONE_PIN"],
                    totp_value,
                )
        except MarketDataError:
            raise
        except Exception:
            raise MarketDataError("Angel One authentication failed") from None

        if not isinstance(response, dict) or response.get("status") is not True:
            raise MarketDataError("Angel One authentication failed")

        self._client = client
        return client

    def _respect_rate_limit(self) -> None:
        now = self.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self.request_interval_seconds - elapsed
            if remaining > 0:
                self.sleeper(remaining)
                now = self.monotonic()
        self._last_request_at = now

    def _read_environment(self) -> dict[str, str]:
        missing = [name for name in self.REQUIRED_ENV if not os.getenv(name)]
        if missing:
            raise MarketDataError("Missing required Angel One market-data environment variables")
        return {name: os.environ[name] for name in self.REQUIRED_ENV}

    def _candle_params(self, symbol_config: dict) -> dict[str, str]:
        exchange = _required_symbol_value(symbol_config, "exchange")
        token = _required_symbol_value(symbol_config, "token")
        timeframe = _required_symbol_value(symbol_config, "timeframe")

        to_date = self.clock()
        if to_date.tzinfo is None:
            to_date = to_date.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        from_date = to_date - timedelta(days=self.lookback_days)

        return {
            "exchange": exchange,
            "symboltoken": token,
            "interval": timeframe,
            "fromdate": from_date.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M"),
        }

    @staticmethod
    def _candles_to_frame(response: object) -> pd.DataFrame:
        if not isinstance(response, dict) or response.get("status") is not True:
            raise MarketDataError("Angel One candle response was unsuccessful")

        data = response.get("data")
        if not isinstance(data, list) or not data:
            raise MarketDataError("Angel One candle response was empty")

        rows = []
        for candle in data:
            if not isinstance(candle, list) or len(candle) != 6:
                raise MarketDataError("Angel One candle response was malformed")
            timestamp, open_price, high, low, close, volume = candle
            try:
                rows.append(
                    {
                        "timestamp": pd.to_datetime(timestamp),
                        "open": float(open_price),
                        "high": float(high),
                        "low": float(low),
                        "close": float(close),
                        "volume": int(volume),
                    }
                )
            except (TypeError, ValueError):
                raise MarketDataError("Angel One candle response was malformed") from None

        return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])

    @staticmethod
    def _default_client_factory(api_key: str):
        try:
            from SmartApi import SmartConnect
        except ImportError:
            raise MarketDataError("smartapi-python is required for Angel One market data") from None
        return SmartConnect(api_key=api_key)

    @staticmethod
    def _now_kolkata() -> datetime:
        return datetime.now(ZoneInfo("Asia/Kolkata"))


def build_market_data_provider(config: dict) -> MarketDataProvider:
    provider_name = str(config.get("provider", "demo")).lower()
    lookback_days = int(config.get("lookback_days", 5))

    if provider_name == "demo":
        return DemoMarketDataProvider()
    if provider_name == "angel_one":
        return AngelOneMarketDataProvider(
            lookback_days=lookback_days,
            request_interval_seconds=float(config.get("request_interval_seconds", 1.2)),
            max_retries=int(config.get("max_retries", 2)),
            retry_backoff_seconds=float(config.get("retry_backoff_seconds", 3)),
        )

    raise SafetyConfigError("market_data.provider must be one of: demo, angel_one")


def validate_market_data_config(config: dict) -> None:
    provider_name = config.get("provider")
    if provider_name not in {"demo", "angel_one"}:
        raise SafetyConfigError("market_data.provider must be one of: demo, angel_one")

    lookback_days = config.get("lookback_days", 5)
    if not isinstance(lookback_days, int) or lookback_days < 1 or lookback_days > 30:
        raise SafetyConfigError("market_data.lookback_days must be between 1 and 30")

    interval = config.get("request_interval_seconds", 1.2)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 0.5 <= interval <= 10:
        raise SafetyConfigError("market_data.request_interval_seconds must be between 0.5 and 10")
    retries = config.get("max_retries", 2)
    if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 5:
        raise SafetyConfigError("market_data.max_retries must be an integer between 0 and 5")
    backoff = config.get("retry_backoff_seconds", 3)
    if not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or not 1 <= backoff <= 30:
        raise SafetyConfigError("market_data.retry_backoff_seconds must be between 1 and 30")


def _required_symbol_value(symbol_config: dict, key: str) -> str:
    value = symbol_config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MarketDataError(f"Symbol configuration is missing {key}")
    return value


def _is_rate_limited(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    return response.get("errorcode") == "AB1021" or "too many requests" in str(
        response.get("message", "")
    ).lower()


def _is_rate_limit_exception(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "ab1021",
            "too many requests",
            "access denied because of exceeding access rate",
        )
    )


@contextmanager
def _suppress_smartapi_logging():
    logger = None
    previous_level = None
    try:
        from logzero import logger as smartapi_logger

        logger = smartapi_logger
        previous_level = logger.level
        logger.setLevel(logging.CRITICAL + 1)
    except Exception:
        logger = None

    try:
        yield
    finally:
        if logger is not None and previous_level is not None:
            logger.setLevel(previous_level)
