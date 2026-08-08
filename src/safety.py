from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


class SafetyConfigError(ValueError):
    """Raised when safety-critical configuration is missing or unsafe."""


class LiveTradingDisabledError(RuntimeError):
    """Raised when any Release path attempts live order submission."""


@dataclass(frozen=True)
class SafetySettings:
    trading_mode: str
    live_trading_enabled: bool
    kill_switch_active: bool


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise SafetyConfigError(f"{name} must be a boolean value")


def load_safety_settings(raw: dict[str, Any]) -> SafetySettings:
    trading = require_section(raw, "trading")
    risk = require_section(raw, "risk")
    require_section(raw, "news")
    require_section(raw, "logging")
    market_data = require_section(raw, "market_data")

    configured_mode = require_string(trading, "mode").lower()
    trading_mode = os.getenv("TRADING_MODE", configured_mode).strip().lower()
    if trading_mode != "paper":
        raise SafetyConfigError("Release 2 supports TRADING_MODE=paper only")

    live_trading_enabled = env_bool("LIVE_TRADING_ENABLED", False)
    if live_trading_enabled:
        raise SafetyConfigError("LIVE_TRADING_ENABLED cannot be true in Release 2")

    kill_switch_active = env_bool("KILL_SWITCH_ACTIVE", True)

    require_bool(trading, "require_manual_approval")
    threshold = require_int(trading, "confidence_threshold")
    if threshold < 1 or threshold > 100:
        raise SafetyConfigError("trading.confidence_threshold must be between 1 and 100")

    symbols = trading.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise SafetyConfigError("trading.symbols must be a non-empty list")

    if require_bool(risk, "avoid_high_impact_news") is not True:
        raise SafetyConfigError("risk.avoid_high_impact_news must remain true in Release 2")

    for key in (
        "capital",
        "risk_per_trade_pct",
        "max_daily_loss_pct",
        "max_trades_per_day",
        "stop_after_consecutive_losses",
        "max_position_value_pct",
    ):
        if key not in risk:
            raise SafetyConfigError(f"risk.{key} is required")

    from .market_data import validate_market_data_config

    validate_market_data_config(market_data)

    return SafetySettings(
        trading_mode=trading_mode,
        live_trading_enabled=live_trading_enabled,
        kill_switch_active=kill_switch_active,
    )


def require_section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise SafetyConfigError(f"Config section '{name}' is required and must be a mapping")
    return value


def require_bool(section: dict[str, Any], key: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise SafetyConfigError(f"{key} must be a boolean")
    return value


def require_string(section: dict[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SafetyConfigError(f"{key} must be a non-empty string")
    return value


def require_int(section: dict[str, Any], key: str) -> int:
    value = section.get(key)
    if not isinstance(value, int):
        raise SafetyConfigError(f"{key} must be an integer")
    return value
