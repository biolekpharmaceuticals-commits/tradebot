from __future__ import annotations

import os
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
        client_factory: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lookback_days < 1 or lookback_days > 30:
            raise MarketDataError("market_data.lookback_days must be between 1 and 30")
        self.lookback_days = lookback_days
        self.client_factory = client_factory or self._default_client_factory
        self.clock = clock or self._now_kolkata
        self._client = None

    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        client = self._authenticated_client()
        params = self._candle_params(symbol_config)

        try:
            response = client.getCandleData(params)
        except Exception as exc:
            raise MarketDataError("Angel One candle retrieval failed") from exc

        return self._candles_to_frame(response)

    def _authenticated_client(self):
        if self._client is not None:
            return self._client

        values = self._read_environment()
        client = self.client_factory(values["ANGEL_ONE_API_KEY"])

        try:
            import pyotp

            totp_value = pyotp.TOTP(values["ANGEL_ONE_TOTP_SECRET"]).now()
            response = client.generateSession(
                values["ANGEL_ONE_CLIENT_CODE"],
                values["ANGEL_ONE_PIN"],
                totp_value,
            )
        except Exception as exc:
            raise MarketDataError("Angel One authentication failed") from exc

        if not isinstance(response, dict) or response.get("status") is not True:
            raise MarketDataError("Angel One authentication failed")

        self._client = client
        return client

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
            except (TypeError, ValueError) as exc:
                raise MarketDataError("Angel One candle response was malformed") from exc

        return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])

    @staticmethod
    def _default_client_factory(api_key: str):
        try:
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise MarketDataError("smartapi-python is required for Angel One market data") from exc
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
        return AngelOneMarketDataProvider(lookback_days=lookback_days)

    raise SafetyConfigError("market_data.provider must be one of: demo, angel_one")


def validate_market_data_config(config: dict) -> None:
    provider_name = config.get("provider")
    if provider_name not in {"demo", "angel_one"}:
        raise SafetyConfigError("market_data.provider must be one of: demo, angel_one")

    lookback_days = config.get("lookback_days", 5)
    if not isinstance(lookback_days, int) or lookback_days < 1 or lookback_days > 30:
        raise SafetyConfigError("market_data.lookback_days must be between 1 and 30")


def _required_symbol_value(symbol_config: dict, key: str) -> str:
    value = symbol_config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MarketDataError(f"Symbol configuration is missing {key}")
    return value
