from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import pandas as pd

from .bulk_deals import BulkDealSignal
from .news import NewsSignal


@dataclass(frozen=True)
class BacktestSettings:
    enabled: bool
    minimum_candles: int
    holding_bars: int
    round_trip_cost_bps: float


def load_backtest_settings(config: object) -> BacktestSettings:
    values = config if isinstance(config, dict) else {}
    return BacktestSettings(
        enabled=bool(values.get("enabled", False)),
        minimum_candles=int(values.get("minimum_candles", 80)),
        holding_bars=int(values.get("holding_bars", 12)),
        round_trip_cost_bps=float(values.get("round_trip_cost_bps", 10)),
    )


def run_backtest(
    candles: pd.DataFrame,
    strategy,
    symbol_config: dict,
    settings: BacktestSettings,
) -> dict[str, object]:
    base = {
        "enabled": settings.enabled,
        "method": "Walk-forward, non-overlapping trades, conservative stop-first fills",
        "news_assumption": "Neutral historical news and bulk-deal inputs",
        "minimum_candles": settings.minimum_candles,
        "holding_bars": settings.holding_bars,
        "round_trip_cost_bps": settings.round_trip_cost_bps,
    }
    if not settings.enabled:
        return {**base, "status": "disabled"}
    if candles is None or len(candles) < settings.minimum_candles + 1:
        return {**base, "status": "insufficient_data", "candles": 0 if candles is None else len(candles)}

    neutral_news = NewsSignal(0, "Low", [], "Neutral input for historical backtest")
    neutral_bulk = BulkDealSignal(
        status="backtest_neutral",
        direction="Neutral",
        score=0,
        deal_count=0,
        buy_quantity=0,
        sell_quantity=0,
        net_quantity=0,
        latest_date=None,
        stale=False,
        explanation="Bulk-deal data excluded from historical backtest",
        source="",
        deals=[],
    )
    required_decision = symbol_config.get("required_decision")
    instrument_type = str(symbol_config.get("instrument_type", "equity"))
    trades: list[dict[str, object]] = []
    index = settings.minimum_candles - 1
    final_signal_bar = len(candles) - 2

    while index <= final_signal_bar:
        history = candles.iloc[: index + 1]
        signal = strategy.evaluate(
            history,
            neutral_news,
            neutral_bulk,
            instrument_type=instrument_type,
        )
        if signal.decision not in {"BUY", "SELL"} or (
            required_decision and signal.decision != required_decision
        ):
            index += 1
            continue

        exit_index = min(len(candles) - 1, index + settings.holding_bars)
        outcome, exit_price, resolved_index = _resolve_trade(
            candles,
            index,
            exit_index,
            signal.decision,
            float(signal.entry_price),
            float(signal.stop_loss),
            float(signal.target),
        )
        gross_return = _return_pct(signal.decision, float(signal.entry_price), exit_price)
        net_return = gross_return - (settings.round_trip_cost_bps / 100)
        trades.append(
            {
                "entry_time": _timestamp(candles.iloc[index]),
                "exit_time": _timestamp(candles.iloc[resolved_index]),
                "decision": signal.decision,
                "entry_price": round(float(signal.entry_price), 2),
                "exit_price": round(exit_price, 2),
                "outcome": outcome,
                "net_return_pct": round(net_return, 4),
            }
        )
        index = resolved_index + 1

    returns = [float(item["net_return_pct"]) for item in trades]
    wins = sum(value > 0 for value in returns)
    losses = sum(value <= 0 for value in returns)
    gross_profit = sum(value for value in returns if value > 0)
    gross_loss = abs(sum(value for value in returns if value < 0))
    equity = 100.0
    peak = equity
    max_drawdown = 0.0
    for value in returns:
        equity *= 1 + (value / 100)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, ((peak - equity) / peak) * 100 if peak else 0)

    profit_factor = gross_profit / gross_loss if gross_loss else None
    return {
        **base,
        "status": "complete",
        "candles": len(candles),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / len(trades)) * 100, 2) if trades else 0.0,
        "net_return_pct": round(equity - 100, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "average_trade_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "profit_factor": round(profit_factor, 2) if profit_factor is not None and isfinite(profit_factor) else None,
        "recent_trades": trades[-20:],
    }


def validate_backtest_config(config: object) -> None:
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("backtesting must be a mapping")
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("backtesting.enabled must be a boolean")
    _bounded_int(config, "minimum_candles", 60, 2000, 80)
    _bounded_int(config, "holding_bars", 1, 100, 12)
    costs = config.get("round_trip_cost_bps", 10)
    if not isinstance(costs, (int, float)) or isinstance(costs, bool) or not 0 <= costs <= 100:
        raise ValueError("backtesting.round_trip_cost_bps must be between 0 and 100")


def _resolve_trade(
    candles: pd.DataFrame,
    entry_index: int,
    final_index: int,
    decision: str,
    entry: float,
    stop: float,
    target: float,
) -> tuple[str, float, int]:
    for index in range(entry_index + 1, final_index + 1):
        row = candles.iloc[index]
        open_price = float(row["open"])
        if decision == "BUY":
            if open_price <= stop:
                return "stop_gap", open_price, index
            if open_price >= target:
                return "target", target, index
            stop_hit = float(row["low"]) <= stop
            target_hit = float(row["high"]) >= target
        else:
            if open_price >= stop:
                return "stop_gap", open_price, index
            if open_price <= target:
                return "target", target, index
            stop_hit = float(row["high"]) >= stop
            target_hit = float(row["low"]) <= target
        if stop_hit:
            return "stop", stop, index
        if target_hit:
            return "target", target, index
    return "time_exit", float(candles.iloc[final_index]["close"]), final_index


def _return_pct(decision: str, entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    move = exit_price - entry if decision == "BUY" else entry - exit_price
    return (move / entry) * 100


def _timestamp(row: pd.Series) -> str | None:
    value = row.get("timestamp")
    return str(value) if value is not None else None


def _bounded_int(config: dict, key: str, minimum: int, maximum: int, default: int) -> None:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"backtesting.{key} must be an integer between {minimum} and {maximum}")
