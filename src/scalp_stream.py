from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import AppConfig
from .derivatives import AngelOneDerivativeDiscovery, DerivativeDiscoveryError
from .scalp_shadow import (
    ScalpShadowEngine,
    ScalpShadowError,
    Tick,
    load_scalp_shadow_settings,
)

KOLKATA = ZoneInfo("Asia/Kolkata")
REQUIRED_ENV = (
    "ANGEL_ONE_API_KEY",
    "ANGEL_ONE_CLIENT_CODE",
    "ANGEL_ONE_PIN",
    "ANGEL_ONE_TOTP_SECRET",
)


class AngelOneScalpRuntime:
    """Read-only SmartAPI WebSocket transport for the isolated paper scalp engine."""

    def __init__(
        self,
        config: AppConfig,
        *,
        client_factory: Callable[[str], object] | None = None,
        websocket_factory: Callable[..., object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.values = dict(config.raw.get("scalp_shadow") or {})
        self.settings = load_scalp_shadow_settings(self.values, config.base_dir)
        if not self.settings.enabled:
            raise ScalpShadowError("scalp_shadow.enabled is false")
        if self.settings.paper_execution_enabled and not config.safety.auto_paper_trading_enabled:
            raise ScalpShadowError("AUTO_PAPER_TRADING_ENABLED must be true for scalp paper execution")
        self.client_factory = client_factory or _smart_connect_factory
        self.websocket_factory = websocket_factory or _smart_websocket_factory
        self.clock = clock or (lambda: datetime.now(KOLKATA))
        self.client = None
        self.websocket = None
        self.engine: ScalpShadowEngine | None = None
        self.instruments: dict[str, dict] = {}

    def prepare(self) -> dict:
        credentials = _credentials()
        client, jwt_token, feed_token = self._authenticate(credentials)
        self.client = client
        signal = dict(self.settings.signal_instrument)
        spot_price = self._spot_price(client, signal)
        execution = self._resolve_execution_contract(spot_price)
        self.instruments = {
            str(signal["token"]): {
                **signal,
                "role": "signal",
                "lot_size": 1,
                "tick_size": 0.05,
                "exchange_type": 1,
            },
            str(execution["token"]): {
                **execution,
                "role": "execution",
                "tick_size": float(self.values.get("execution_tick_size", 0.05)),
                "exchange_type": 2,
            },
        }
        self.engine = ScalpShadowEngine(
            self.settings,
            execution_token=str(execution["token"]),
            kill_switch_active=self.config.safety.kill_switch_active,
            auto_paper_enabled=self.config.safety.auto_paper_trading_enabled,
            clock=self.clock,
        )
        self.websocket = self.websocket_factory(
            jwt_token,
            credentials["ANGEL_ONE_API_KEY"],
            credentials["ANGEL_ONE_CLIENT_CODE"],
            feed_token,
        )
        manifest = {
            "release": "6.0",
            "mode": "paper_shadow",
            "prepared_at": self.clock().isoformat(),
            "signal_instrument": signal,
            "execution_contract": execution,
            "paper_execution_enabled": self.settings.paper_execution_enabled,
            "live_orders_available": False,
        }
        self._write_manifest(manifest)
        self.engine.audit.write("runtime_prepared", manifest)
        return manifest

    def run_forever(self) -> None:
        if self.websocket is None or self.engine is None:
            self.prepare()
        websocket = self.websocket
        websocket.on_open = self._on_open
        websocket.on_data = self._on_data
        websocket.on_error = self._on_error
        websocket.on_close = self._on_close
        with _suppress_smartapi_logging():
            websocket.connect()
        raise ScalpShadowError("Angel One scalp WebSocket disconnected")

    def _authenticate(self, credentials: dict[str, str]) -> tuple[object, str, str]:
        try:
            import pyotp

            with _suppress_smartapi_logging():
                client = self.client_factory(credentials["ANGEL_ONE_API_KEY"])
                response = client.generateSession(
                    credentials["ANGEL_ONE_CLIENT_CODE"],
                    credentials["ANGEL_ONE_PIN"],
                    pyotp.TOTP(credentials["ANGEL_ONE_TOTP_SECRET"]).now(),
                )
            data = response.get("data") if isinstance(response, dict) else None
            jwt_token = str((data or {}).get("jwtToken", ""))
            feed_token = str(client.getfeedToken())
        except Exception:
            raise ScalpShadowError("Angel One scalp market-data authentication failed") from None
        if not isinstance(response, dict) or response.get("status") is not True:
            raise ScalpShadowError("Angel One scalp market-data authentication failed")
        if not jwt_token or not feed_token:
            raise ScalpShadowError("Angel One scalp stream credentials were unavailable")
        return client, jwt_token, feed_token

    @staticmethod
    def _spot_price(client: object, signal: dict) -> float:
        try:
            with _suppress_smartapi_logging():
                response = client.getMarketData("FULL", {"NSE": [str(signal["token"])]})
            data = response.get("data") if isinstance(response, dict) else None
            fetched = data.get("fetched") if isinstance(data, dict) else None
            row = fetched[0] if isinstance(fetched, list) and fetched else None
            price = float((row or {}).get("ltp", 0))
        except Exception:
            raise ScalpShadowError("Initial NIFTY spot quote was unavailable") from None
        if price <= 0:
            raise ScalpShadowError("Initial NIFTY spot quote was invalid")
        return price

    def _resolve_execution_contract(self, spot_price: float) -> dict:
        try:
            discovery = AngelOneDerivativeDiscovery(
                instruments=["futures"],
                option_strikes=1,
                max_expiry_days=45,
                max_contracts=1,
                timeframe="ONE_MINUTE",
                timeout_seconds=float(self.values.get("instrument_master_timeout_seconds", 10)),
                minimum_expiry_days=1,
            )
            contracts = discovery.contracts_for("NIFTY 50", spot_price, "BUY")
        except DerivativeDiscoveryError:
            raise ScalpShadowError("Current NIFTY futures discovery failed") from None
        futures = [item for item in contracts if item.get("derivative_type") == "future"]
        if not futures:
            raise ScalpShadowError("No eligible current NIFTY futures contract was found")
        contract = dict(futures[0])
        if (
            contract.get("exchange") != "NFO"
            or not str(contract.get("token", "")).strip()
            or int(contract.get("lot_size", 0)) <= 0
        ):
            raise ScalpShadowError("Resolved NIFTY futures identity was invalid")
        return contract

    def _on_open(self, websocket_app) -> None:
        token_groups: dict[int, list[str]] = {}
        for token, instrument in self.instruments.items():
            token_groups.setdefault(int(instrument["exchange_type"]), []).append(token)
        token_list = [
            {"exchangeType": exchange_type, "tokens": tokens}
            for exchange_type, tokens in sorted(token_groups.items())
        ]
        correlation_id = "release-6-scalp-shadow"
        mode = 3  # FULL mode is required for execution bid/ask validation.
        if self.websocket is None:
            raise ScalpShadowError("Scalp WebSocket was unavailable during subscription")
        self.websocket.subscribe(correlation_id, mode, token_list)
        if self.engine:
            self.engine.audit.write("stream_open", {"subscriptions": token_list, "mode": "FULL"})

    def _on_data(self, websocket_app, message: object) -> None:
        if self.engine is None:
            return
        try:
            tick = parse_smartapi_tick(
                message,
                self.instruments,
                received_at=self.clock(),
                price_scale=float(self.values.get("price_scale", 100)),
            )
        except ScalpShadowError as exc:
            self.engine.audit.write("stream_message_rejected", {"reason": str(exc)})
            return
        self.engine.on_tick(tick)

    def _on_error(self, websocket_app, error: object) -> None:
        if self.engine:
            self.engine.audit.write("stream_error", {"reason": type(error).__name__})

    def _on_close(self, websocket_app, *args) -> None:
        if self.engine:
            self.engine.audit.write("stream_close", {"details": [str(value)[:120] for value in args]})

    def _write_manifest(self, payload: dict) -> None:
        self.settings.tick_log_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.tick_log_dir / f"runtime-{self.clock().date().isoformat()}.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.chmod(path, 0o600)


def parse_smartapi_tick(
    message: object,
    instruments: dict[str, dict],
    *,
    received_at: datetime,
    price_scale: float = 100,
) -> Tick:
    if not isinstance(message, dict):
        raise ScalpShadowError("SmartAPI tick was not a mapping")
    token = str(message.get("token", "")).strip()
    instrument = instruments.get(token)
    if instrument is None:
        raise ScalpShadowError("SmartAPI tick token was not subscribed")
    if price_scale <= 0:
        raise ScalpShadowError("SmartAPI price scale was invalid")
    try:
        timestamp_ms = int(message["exchange_timestamp"])
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=KOLKATA)
        last_price = float(message["last_traded_price"]) / price_scale
        bid = _depth_price(message.get("best_5_buy_data"), price_scale)
        ask = _depth_price(message.get("best_5_sell_data"), price_scale)
        cumulative_volume = int(message.get("volume_trade_for_the_day", 0) or 0)
        last_quantity = int(message.get("last_traded_quantity", 0) or 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ScalpShadowError("SmartAPI tick was malformed") from None
    if last_price <= 0:
        raise ScalpShadowError("SmartAPI tick price was invalid")
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=KOLKATA)
    return Tick(
        role=str(instrument["role"]),
        symbol=str(instrument["symbol"]),
        exchange=str(instrument["exchange"]),
        token=token,
        timestamp=timestamp,
        received_at=received_at.astimezone(KOLKATA),
        last_price=last_price,
        bid=bid,
        ask=ask,
        cumulative_volume=cumulative_volume,
        last_quantity=last_quantity,
        lot_size=int(instrument.get("lot_size", 1)),
        tick_size=float(instrument.get("tick_size", 0.05)),
    )


def _depth_price(value: object, price_scale: float) -> float:
    if not isinstance(value, list) or not value:
        return 0.0
    first = value[0]
    if not isinstance(first, dict):
        return 0.0
    raw = first.get("price", first.get("price_in_paise", 0))
    try:
        return float(raw) / price_scale
    except (TypeError, ValueError):
        return 0.0


def _credentials() -> dict[str, str]:
    missing = [key for key in REQUIRED_ENV if not os.getenv(key)]
    if missing:
        raise ScalpShadowError("Missing required Angel One scalp market-data environment variables")
    return {key: os.environ[key] for key in REQUIRED_ENV}


def _smart_connect_factory(api_key: str):
    try:
        from SmartApi import SmartConnect
    except ImportError:
        raise ScalpShadowError("smartapi-python is required for scalp market data") from None
    return SmartConnect(api_key=api_key)


def _smart_websocket_factory(jwt_token: str, api_key: str, client_code: str, feed_token: str):
    try:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    except ImportError:
        raise ScalpShadowError("SmartAPI WebSocket V2 is required for scalp market data") from None
    return SmartWebSocketV2(jwt_token, api_key, client_code, feed_token)


@contextmanager
def _suppress_smartapi_logging():
    logger_names = ("SmartApi", "smartapi", "websocket")
    old_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        for name in logger_names:
            logging.getLogger(name).setLevel(logging.CRITICAL)
        yield
    finally:
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)
