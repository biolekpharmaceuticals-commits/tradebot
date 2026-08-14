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
    auto_paper_trading_enabled: bool


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
        raise SafetyConfigError("Release 5 supports TRADING_MODE=paper only")

    live_trading_enabled = env_bool("LIVE_TRADING_ENABLED", False)
    if live_trading_enabled:
        raise SafetyConfigError("LIVE_TRADING_ENABLED cannot be true in Release 5")

    kill_switch_active = env_bool("KILL_SWITCH_ACTIVE", True)
    auto_paper_trading_enabled = env_bool("AUTO_PAPER_TRADING_ENABLED", False)

    require_bool(trading, "require_manual_approval")
    threshold = require_int(trading, "confidence_threshold")
    if threshold < 1 or threshold > 100:
        raise SafetyConfigError("trading.confidence_threshold must be between 1 and 100")

    symbols = trading.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise SafetyConfigError("trading.symbols must be a non-empty list")
    index_tokens = {
        "NIFTY": "99926000",
        "NIFTY50": "99926000",
        "NIFTY 50": "99926000",
        "BANKNIFTY": "99926009",
        "BANK NIFTY": "99926009",
        "NIFTY BANK": "99926009",
    }
    for symbol in symbols:
        if not isinstance(symbol, dict):
            raise SafetyConfigError("Each trading symbol must be a mapping")
        instrument_type = str(symbol.get("instrument_type", "equity")).lower()
        if instrument_type not in {"equity", "index"}:
            raise SafetyConfigError("instrument_type must be equity or index")
        if instrument_type == "index":
            name = str(symbol.get("symbol", "")).strip().upper()
            if name not in index_tokens:
                raise SafetyConfigError("Only NIFTY 50 and NIFTY BANK spot indices are supported")
            if symbol.get("exchange") != "NSE" or str(symbol.get("token", "")) != index_tokens[name]:
                raise SafetyConfigError("NIFTY index exchange or token is invalid")

    scanner = raw.get("scanner", {})
    if not isinstance(scanner, dict):
        raise SafetyConfigError("scanner must be a mapping")
    scanner_enabled = scanner.get("enabled", False)
    if not isinstance(scanner_enabled, bool):
        raise SafetyConfigError("scanner.enabled must be a boolean")
    if scanner_enabled and len(symbols) < 2:
        raise SafetyConfigError("scanner requires at least two configured symbols")
    max_candidates = scanner.get("max_candidates", 20)
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool) or not 2 <= max_candidates <= 25:
        raise SafetyConfigError("scanner.max_candidates must be an integer between 2 and 25")
    min_average_volume = scanner.get("min_average_volume", 100000)
    if (
        not isinstance(min_average_volume, int)
        or isinstance(min_average_volume, bool)
        or min_average_volume < 0
    ):
        raise SafetyConfigError("scanner.min_average_volume must be a non-negative integer")

    if require_bool(risk, "avoid_high_impact_news") is not True:
        raise SafetyConfigError("risk.avoid_high_impact_news must remain true in Release 5")

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
    from .bulk_deals import validate_bulk_deal_config
    from .derivatives import validate_derivative_config
    from .backtest import validate_backtest_config
    from .broker import validate_paper_execution_config
    from .live_market import validate_dashboard_market_config

    validate_market_data_config(market_data)
    try:
        validate_bulk_deal_config(raw.get("bulk_deals"))
        validate_derivative_config(raw.get("derivatives"))
        validate_backtest_config(raw.get("backtesting"))
        validate_paper_execution_config(raw.get("paper_execution"))
        validate_dashboard_market_config(raw.get("market_dashboard"), symbols, market_data)
    except ValueError as exc:
        raise SafetyConfigError(str(exc)) from None

    return SafetySettings(
        trading_mode=trading_mode,
        live_trading_enabled=live_trading_enabled,
        kill_switch_active=kill_switch_active,
        auto_paper_trading_enabled=auto_paper_trading_enabled,
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
