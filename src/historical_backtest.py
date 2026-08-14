from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from .backtest import load_backtest_settings, run_backtest
from .market_data import AngelOneMarketDataProvider, MarketDataError
from .strategy import TrendStrategy


MAX_DAYS_PER_INTERVAL = {
    "ONE_MINUTE": 30,
    "THREE_MINUTE": 60,
    "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100,
    "FIFTEEN_MINUTE": 200,
    "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400,
    "ONE_DAY": 2000,
}


def maximum_history_days(timeframe: str) -> int:
    try:
        return MAX_DAYS_PER_INTERVAL[str(timeframe).upper()]
    except KeyError:
        raise ValueError(f"Unsupported historical timeframe: {timeframe}") from None


def fetch_historical_candles(
    provider: AngelOneMarketDataProvider,
    symbol_config: dict,
    *,
    days: int,
    end: datetime | None = None,
    chunk_days: int = 25,
) -> pd.DataFrame:
    timeframe = str(symbol_config.get("timeframe", ""))
    maximum = maximum_history_days(timeframe)
    if days < 1 or days > maximum:
        raise ValueError(f"{timeframe} history days must be between 1 and {maximum}")
    if chunk_days < 1 or chunk_days > 30:
        raise ValueError("chunk_days must be between 1 and 30")

    timezone = ZoneInfo("Asia/Kolkata")
    final_end = end or datetime.now(timezone)
    if final_end.tzinfo is None:
        final_end = final_end.replace(tzinfo=timezone)
    final_end = final_end.astimezone(timezone)
    requested_start = final_end - timedelta(days=days)
    cursor = requested_start
    frames: list[pd.DataFrame] = []

    while cursor < final_end:
        chunk_end = min(cursor + timedelta(days=chunk_days), final_end)
        frame = provider.get_candles_between(symbol_config, cursor, chunk_end)
        if frame is not None and not frame.empty:
            frames.append(frame)
        cursor = chunk_end

    if not frames:
        raise MarketDataError("Historical candle response was empty")

    combined = pd.concat(frames, ignore_index=True)
    combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="coerce")
    combined = combined.dropna(subset=["timestamp"])
    combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    if combined.empty:
        raise MarketDataError("Historical candle response was empty")
    return combined


def build_historical_backtest_report(
    config,
    provider: AngelOneMarketDataProvider,
    symbols: Iterable[dict],
    *,
    days: int,
    end: datetime | None = None,
) -> dict[str, object]:
    strategy = TrendStrategy(config.section("strategy"))
    settings = load_backtest_settings(config.raw.get("backtesting", {}))
    trading = config.section("trading")
    derivative_settings = config.raw.get("derivatives") or {}
    reports: list[dict[str, object]] = []

    for configured in symbols:
        symbol = dict(configured)
        candles = fetch_historical_candles(provider, symbol, days=days, end=end)
        result = run_backtest(candles, strategy, symbol, settings)
        instrument_type = str(symbol.get("instrument_type", "equity")).lower()
        threshold = (
            int(derivative_settings.get("confidence_threshold", 55))
            if instrument_type == "derivative"
            else int(trading.get("confidence_threshold", 75))
        )
        reports.append(
            {
                "symbol": symbol.get("symbol"),
                "exchange": symbol.get("exchange"),
                "timeframe": symbol.get("timeframe"),
                "period_start": str(candles.iloc[0]["timestamp"]),
                "period_end": str(candles.iloc[-1]["timestamp"]),
                "configured_execution_confidence_threshold": threshold,
                "result": result,
            }
        )

    paper = config.section("paper_execution")
    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        "mode": config.safety.trading_mode,
        "live_trading_enabled": config.safety.live_trading_enabled,
        "strategy": {"name": strategy.NAME, "version": strategy.VERSION},
        "requested_calendar_days": days,
        "paper_initial_balance": float(paper.get("initial_balance", 0)),
        "methodology": {
            "scope": "Deployed signal-engine backtest on configured instruments",
            "execution_gates_simulated": False,
            "historical_news": "Neutral",
            "historical_bulk_deals": "Neutral",
            "rolling_expired_derivatives": False,
            "warning": (
                "Results are percentage signal returns, not executable F&O portfolio P&L. "
                "Confidence, liquidity, lot-risk, and paper-account gates remain outside this backtest."
            ),
        },
        "symbols": reports,
    }
