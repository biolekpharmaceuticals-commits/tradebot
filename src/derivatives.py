from __future__ import annotations

from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo

import requests


INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)
UNDERLYING_NAMES = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY 50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "BANK NIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
}


class DerivativeDiscoveryError(RuntimeError):
    """Raised when read-only F&O contract discovery cannot produce safe candidates."""


class DisabledDerivativeDiscovery:
    enabled = False

    def contracts_for(self, *args, **kwargs) -> list[dict]:
        return []


class AngelOneDerivativeDiscovery:
    enabled = True

    def __init__(
        self,
        *,
        instruments: list[str],
        option_strikes: int,
        max_expiry_days: int,
        max_contracts: int,
        timeframe: str,
        timeout_seconds: float,
        fetcher: Callable[[], object] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.instruments = instruments
        self.option_strikes = option_strikes
        self.max_expiry_days = max_expiry_days
        self.max_contracts = max_contracts
        self.timeframe = timeframe
        self.timeout_seconds = timeout_seconds
        self.fetcher = fetcher or self._fetch_master
        self.today = today or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")).date())
        self._master: list[dict] | None = None

    def contracts_for(self, underlying: str, spot_price: float, direction: str) -> list[dict]:
        normalized = UNDERLYING_NAMES.get(underlying.strip().upper())
        if normalized is None or direction not in {"BUY", "SELL"} or spot_price <= 0:
            return []

        rows = self._load_master()
        today = self.today()
        matching = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("exch_seg", "")).upper() != "NFO":
                continue
            if str(row.get("name", "")).upper() != normalized:
                continue
            expiry = _parse_expiry(row.get("expiry"))
            if expiry is None:
                continue
            days_to_expiry = (expiry - today).days
            if days_to_expiry < 0 or days_to_expiry > self.max_expiry_days:
                continue
            matching.append((expiry, row))

        contracts: list[dict] = []
        if "futures" in self.instruments:
            futures = [item for item in matching if item[1].get("instrumenttype") == "FUTIDX"]
            if futures:
                expiry, row = min(futures, key=lambda item: item[0])
                contracts.append(self._config(row, normalized, expiry, "future", None, direction))

        if "options" in self.instruments:
            option_rows = [item for item in matching if item[1].get("instrumenttype") == "OPTIDX"]
            if option_rows:
                nearest_expiry = min(item[0] for item in option_rows)
                option_side = "CE" if direction == "BUY" else "PE"
                same_expiry_side = [
                    (expiry, row)
                    for expiry, row in option_rows
                    if expiry == nearest_expiry and str(row.get("symbol", "")).upper().endswith(option_side)
                ]
                by_distance = sorted(
                    same_expiry_side,
                    key=lambda item: (abs(_strike(item[1]) - spot_price), _strike(item[1])),
                )
                for expiry, row in by_distance[: self.option_strikes]:
                    derivative_type = "call" if option_side == "CE" else "put"
                    contracts.append(
                        self._config(row, normalized, expiry, derivative_type, _strike(row), "BUY")
                    )

        return contracts[: self.max_contracts]

    def _load_master(self) -> list[dict]:
        if self._master is not None:
            return self._master
        try:
            payload = self.fetcher()
        except Exception:
            raise DerivativeDiscoveryError("Angel One instrument master is unavailable") from None
        if not isinstance(payload, list):
            raise DerivativeDiscoveryError("Angel One instrument master was malformed")
        self._master = payload
        return payload

    def _config(
        self,
        row: dict,
        underlying: str,
        expiry: date,
        derivative_type: str,
        strike: float | None,
        required_decision: str,
    ) -> dict:
        symbol = str(row.get("symbol", "")).strip()
        token = str(row.get("token", "")).strip()
        lot_size = _positive_int(row.get("lotsize"))
        if not symbol or not token or lot_size <= 0:
            raise DerivativeDiscoveryError("Angel One instrument master contained an invalid contract")
        return {
            "exchange": "NFO",
            "symbol": symbol,
            "token": token,
            "quantity": lot_size,
            "timeframe": self.timeframe,
            "instrument_type": "derivative",
            "derivative_type": derivative_type,
            "underlying": underlying,
            "expiry": expiry.isoformat(),
            "strike": strike,
            "lot_size": lot_size,
            "required_decision": required_decision,
        }

    def _fetch_master(self) -> object:
        response = requests.get(
            INSTRUMENT_MASTER_URL,
            headers={"Accept": "application/json", "User-Agent": "tradebot-read-only/4"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()


def build_derivative_discovery(config: object):
    if not isinstance(config, dict) or not config.get("enabled", False):
        return DisabledDerivativeDiscovery()
    return AngelOneDerivativeDiscovery(
        instruments=list(config.get("instruments", ["futures", "options"])),
        option_strikes=int(config.get("option_strikes", 1)),
        max_expiry_days=int(config.get("max_expiry_days", 45)),
        max_contracts=int(config.get("max_contracts", 6)),
        timeframe=str(config.get("timeframe", "FIVE_MINUTE")),
        timeout_seconds=float(config.get("timeout_seconds", 10)),
    )


def validate_derivative_config(config: object) -> None:
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("derivatives must be a mapping")
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("derivatives.enabled must be a boolean")
    instruments = config.get("instruments", ["futures", "options"])
    if not isinstance(instruments, list) or not instruments or not set(instruments) <= {"futures", "options"}:
        raise ValueError("derivatives.instruments may contain only futures and options")
    if config.get("option_buying_only", True) is not True:
        raise ValueError("derivatives.option_buying_only must remain true")
    _bounded_int(config, "option_strikes", 1, 3, 1)
    _bounded_int(config, "max_expiry_days", 1, 60, 45)
    _bounded_int(config, "max_contracts", 1, 10, 6)
    timeout = config.get("timeout_seconds", 10)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30:
        raise ValueError("derivatives.timeout_seconds must be between 0 and 30")
    timeframe = config.get("timeframe", "FIVE_MINUTE")
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("derivatives.timeframe must be a non-empty string")


def _parse_expiry(value: object) -> date | None:
    text = str(value or "").strip().upper()
    for pattern in ("%d%b%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _strike(row: dict) -> float:
    try:
        return float(row.get("strike", 0)) / 100
    except (TypeError, ValueError):
        return 0.0


def _positive_int(value: object) -> int:
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return 0


def _bounded_int(config: dict, key: str, minimum: int, maximum: int, default: int) -> None:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"derivatives.{key} must be an integer between {minimum} and {maximum}")
