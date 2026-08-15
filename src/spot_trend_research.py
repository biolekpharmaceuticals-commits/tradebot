from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from statistics import median, pstdev
from zoneinfo import ZoneInfo

from .nse_option_backtest import (
    NSEBacktestError,
    OptionDailyBar,
    SUPPORTED_UNDERLYINGS,
    _proposals_for_symbol,
)


@dataclass(frozen=True)
class SpotTrendSettings:
    name: str
    underlyings: tuple[str, ...]
    ema_fast: int
    ema_slow: int
    momentum_days: int
    momentum_threshold_pct: float
    volatility_lookback: int
    minimum_volatility_pct: float
    maximum_volatility_pct: float
    minimum_expiry_days: int
    maximum_expiry_days: int
    wing_width_strikes: int
    allow_range_condor: bool


RESEARCH_CANDIDATES = (
    SpotTrendSettings(
        "nifty_balanced_spreads",
        ("NIFTY",),
        5,
        20,
        5,
        0.15,
        10,
        5,
        35,
        3,
        10,
        1,
        False,
    ),
    SpotTrendSettings(
        "nifty_strict_spreads",
        ("NIFTY",),
        5,
        20,
        5,
        0.35,
        10,
        8,
        30,
        4,
        10,
        1,
        False,
    ),
    SpotTrendSettings(
        "nifty_balanced_with_condor",
        ("NIFTY",),
        5,
        20,
        5,
        0.15,
        10,
        5,
        35,
        3,
        10,
        1,
        True,
    ),
    SpotTrendSettings(
        "nifty_fast_spreads",
        ("NIFTY",),
        3,
        10,
        3,
        0.25,
        10,
        5,
        35,
        3,
        7,
        1,
        False,
    ),
    SpotTrendSettings(
        "indices_balanced_spreads",
        ("NIFTY", "BANKNIFTY"),
        5,
        20,
        5,
        0.15,
        10,
        5,
        35,
        3,
        10,
        1,
        False,
    ),
    SpotTrendSettings(
        "indices_strict_spreads",
        ("NIFTY", "BANKNIFTY"),
        5,
        20,
        5,
        0.35,
        10,
        8,
        30,
        4,
        10,
        1,
        False,
    ),
)


def extract_clean_spot_closes(
    daily_bars: dict[date, list[OptionDailyBar]],
    *,
    maximum_dispersion_bps: float = 5,
    maximum_daily_move_pct: float = 20,
) -> tuple[dict[str, list[tuple[date, float]]], dict[str, object]]:
    clean: dict[str, list[tuple[date, float]]] = {symbol: [] for symbol in SUPPORTED_UNDERLYINGS}
    rejected: list[dict[str, object]] = []
    previous: dict[str, float] = {}
    for trade_date in sorted(daily_bars):
        for symbol in SUPPORTED_UNDERLYINGS:
            values = [
                float(item.underlying)
                for item in daily_bars[trade_date]
                if item.symbol == symbol and item.underlying > 0
            ]
            if not values:
                rejected.append({"date": trade_date.isoformat(), "symbol": symbol, "reason": "missing"})
                continue
            close = float(median(values))
            dispersion_bps = (max(values) - min(values)) / close * 10000 if close else math.inf
            if dispersion_bps > maximum_dispersion_bps:
                rejected.append(
                    {
                        "date": trade_date.isoformat(),
                        "symbol": symbol,
                        "reason": "underlying_dispersion",
                        "dispersion_bps": round(dispersion_bps, 4),
                    }
                )
                continue
            prior = previous.get(symbol)
            move_pct = abs(close / prior - 1) * 100 if prior else 0.0
            if prior and move_pct > maximum_daily_move_pct:
                rejected.append(
                    {
                        "date": trade_date.isoformat(),
                        "symbol": symbol,
                        "reason": "implausible_daily_move",
                        "move_pct": round(move_pct, 4),
                    }
                )
                continue
            clean[symbol].append((trade_date, close))
            previous[symbol] = close
    return clean, {
        "method": "Median official underlying value across all valid option rows per symbol and session",
        "maximum_dispersion_bps": maximum_dispersion_bps,
        "maximum_daily_move_pct": maximum_daily_move_pct,
        "sessions": {SUPPORTED_UNDERLYINGS[key]: len(value) for key, value in clean.items()},
        "rejected_count": len(rejected),
        "rejected": rejected,
    }


def build_spot_trends(
    clean_closes: dict[str, list[tuple[date, float]]],
    settings: SpotTrendSettings,
) -> dict[tuple[date, str], dict[str, object]]:
    output: dict[tuple[date, str], dict[str, object]] = {}
    for symbol in settings.underlyings:
        observations = clean_closes.get(symbol, [])
        closes: list[float] = []
        fast_ema = 0.0
        slow_ema = 0.0
        fast_alpha = 2 / (settings.ema_fast + 1)
        slow_alpha = 2 / (settings.ema_slow + 1)
        minimum_history = max(
            settings.ema_slow,
            settings.momentum_days + 1,
            settings.volatility_lookback + 1,
        )
        for index, (trade_date, close) in enumerate(observations):
            fast_ema = close if index == 0 else fast_alpha * close + (1 - fast_alpha) * fast_ema
            slow_ema = close if index == 0 else slow_alpha * close + (1 - slow_alpha) * slow_ema
            closes.append(close)
            if len(closes) < minimum_history:
                continue
            momentum_pct = (close / closes[-settings.momentum_days - 1] - 1) * 100
            recent = closes[-settings.volatility_lookback - 1 :]
            returns = [math.log(current / previous) for previous, current in zip(recent, recent[1:])]
            realized_volatility = pstdev(returns) * math.sqrt(252) * 100 if len(returns) > 1 else 0.0
            tradable = settings.minimum_volatility_pct <= realized_volatility <= settings.maximum_volatility_pct
            if (
                close > slow_ema
                and fast_ema > slow_ema
                and momentum_pct >= settings.momentum_threshold_pct
            ):
                trend = "bullish"
            elif (
                close < slow_ema
                and fast_ema < slow_ema
                and momentum_pct <= -settings.momentum_threshold_pct
            ):
                trend = "bearish"
            else:
                trend = "range"
            output[(trade_date, symbol)] = {
                "trend": trend,
                "tradable": tradable,
                "spot_close": round(close, 4),
                "ema_fast": round(fast_ema, 4),
                "ema_slow": round(slow_ema, 4),
                "momentum_pct": round(momentum_pct, 4),
                "realized_volatility_pct": round(realized_volatility, 4),
            }
    return output


def run_trend_aligned_backtest(
    daily_bars: dict[date, list[OptionDailyBar]],
    base_option_config: dict,
    settings: SpotTrendSettings,
    trends: dict[tuple[date, str], dict[str, object]],
    *,
    trade_start: date,
    trade_end: date,
    initial_balance: float,
    slippage_bps: float,
    fee_bps: float,
) -> dict[str, object]:
    dates = sorted(daily_bars)
    risk_budget = initial_balance * float(base_option_config.get("max_risk_per_trade_pct", 0.5)) / 100
    daily_loss_limit = initial_balance * float(base_option_config.get("max_daily_loss_pct", 1.0)) / 100
    option_config = {
        **base_option_config,
        "structures": ["credit_spread", "iron_condor"] if settings.allow_range_condor else ["credit_spread"],
        "wing_width_strikes": settings.wing_width_strikes,
        "minimum_expiry_days": settings.minimum_expiry_days,
        "maximum_expiry_days": settings.maximum_expiry_days,
    }
    equity = float(initial_balance)
    peak = equity
    maximum_drawdown = 0.0
    trades: list[dict[str, object]] = []
    skipped_regime = 0
    rejected_for_risk = 0
    rejected_for_data = 0
    for signal_date, trade_date in zip(dates, dates[1:]):
        if not trade_start <= trade_date <= trade_end:
            continue
        proposals = []
        for symbol in settings.underlyings:
            trend = trends.get((signal_date, symbol))
            if not trend or trend.get("tradable") is not True:
                skipped_regime += 1
                continue
            raw = _proposals_for_symbol(
                daily_bars[signal_date],
                daily_bars[trade_date],
                symbol,
                option_config,
                risk_budget=risk_budget,
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
            )
            expected_structure = {
                "bullish": "bull_put_credit_spread",
                "bearish": "bear_call_credit_spread",
                "range": "iron_condor" if settings.allow_range_condor else None,
            }.get(str(trend.get("trend")))
            for proposal in raw:
                if proposal.get("structure") != expected_structure:
                    continue
                if expected_structure == "iron_condor":
                    pcr = float(proposal.get("put_call_oi_ratio", 0))
                    if not float(option_config.get("bearish_pcr", 0.9)) < pcr < float(
                        option_config.get("bullish_pcr", 1.1)
                    ):
                        continue
                proposal = {**proposal, "spot_trend": trend}
                rejected_for_risk += proposal.get("rejection") == "risk"
                rejected_for_data += proposal.get("rejection") == "data"
                if proposal.get("eligible"):
                    proposals.append(proposal)
        if not proposals:
            continue
        selected = max(proposals, key=lambda item: float(item.get("score", 0)))
        net_pnl = float(selected["net_pnl"])
        net_pnl = max(net_pnl, -float(selected["max_loss"]) - float(selected["fees"]))
        net_pnl = min(net_pnl, float(selected["max_profit"]) - float(selected["fees"]))
        net_pnl = max(net_pnl, -daily_loss_limit)
        equity += net_pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100 if peak else 0.0
        maximum_drawdown = max(maximum_drawdown, drawdown)
        trades.append(
            {
                **{key: value for key, value in selected.items() if key not in {"eligible", "rejection"}},
                "signal_date": signal_date.isoformat(),
                "trade_date": trade_date.isoformat(),
                "net_pnl": round(net_pnl, 2),
                "ending_balance": round(equity, 2),
            }
        )
    return _performance(
        trades,
        initial_balance=initial_balance,
        ending_balance=equity,
        maximum_drawdown=maximum_drawdown,
        rejected_for_risk=rejected_for_risk,
        rejected_for_data=rejected_for_data,
        skipped_regime=skipped_regime,
        trade_start=trade_start,
        trade_end=trade_end,
    )


def run_walk_forward_research(
    daily_bars: dict[date, list[OptionDailyBar]],
    base_option_config: dict,
    *,
    initial_balance: float,
    slippage_bps: float,
    fee_bps: float,
    candidates: tuple[SpotTrendSettings, ...] = RESEARCH_CANDIDATES,
    train_fraction: float = 0.7,
) -> dict[str, object]:
    dates = sorted(daily_bars)
    if len(dates) < 45:
        raise NSEBacktestError("Spot-trend research requires at least 45 clean NSE sessions")
    if not 0.6 <= train_fraction <= 0.8:
        raise ValueError("train_fraction must be between 0.6 and 0.8")
    split_index = min(len(dates) - 1, max(2, int(len(dates) * train_fraction)))
    validation_start = dates[split_index]
    train_end = dates[split_index - 1]
    clean_closes, quality = extract_clean_spot_closes(daily_bars)
    training_results = []
    trend_cache: dict[str, dict[tuple[date, str], dict[str, object]]] = {}
    for settings in candidates:
        trends = build_spot_trends(clean_closes, settings)
        trend_cache[settings.name] = trends
        result = run_trend_aligned_backtest(
            daily_bars,
            base_option_config,
            settings,
            trends,
            trade_start=dates[1],
            trade_end=train_end,
            initial_balance=initial_balance,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
        )
        training_results.append({"settings": asdict(settings), "result": result})
    eligible_training = [item for item in training_results if _training_gate(item["result"])]
    selected = max(eligible_training, key=_training_score, default=None)
    validation = None
    combined = None
    deployment_gate = False
    gate_reasons = []
    if selected is None:
        gate_reasons.append("No candidate was profitable with sufficient training trades after costs")
    else:
        settings = SpotTrendSettings(**selected["settings"])
        trends = trend_cache[settings.name]
        validation = run_trend_aligned_backtest(
            daily_bars,
            base_option_config,
            settings,
            trends,
            trade_start=validation_start,
            trade_end=dates[-1],
            initial_balance=initial_balance,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
        )
        combined = run_trend_aligned_backtest(
            daily_bars,
            base_option_config,
            settings,
            trends,
            trade_start=dates[1],
            trade_end=dates[-1],
            initial_balance=initial_balance,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
        )
        gate_reasons = _validation_gate_reasons(validation, combined)
        deployment_gate = not gate_reasons
    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        "method": "Chronological 70/30 train-holdout; candidate selected on training data only",
        "data_quality": quality,
        "training_period": {"start": dates[0].isoformat(), "end": train_end.isoformat()},
        "holdout_period": {"start": validation_start.isoformat(), "end": dates[-1].isoformat()},
        "candidate_count": len(candidates),
        "training_results": training_results,
        "selected_candidate": selected["settings"] if selected else None,
        "selected_training_result": selected["result"] if selected else None,
        "holdout_result": validation,
        "combined_result": combined,
        "deployment_gate_passed": deployment_gate,
        "deployment_recommendation": (
            "eligible_for_shadow_implementation" if deployment_gate else "reject_keep_current_shadow"
        ),
        "gate_reasons": gate_reasons,
        "safety": {
            "paper_only": True,
            "shadow_only": True,
            "defined_risk_only": True,
            "naked_short_options": False,
            "maximum_risk_per_structure_pct": base_option_config.get("max_risk_per_trade_pct", 0.5),
            "order_execution_allowed": False,
        },
    }


def _performance(
    trades: list[dict[str, object]],
    *,
    initial_balance: float,
    ending_balance: float,
    maximum_drawdown: float,
    rejected_for_risk: int,
    rejected_for_data: int,
    skipped_regime: int,
    trade_start: date,
    trade_end: date,
) -> dict[str, object]:
    wins = [item for item in trades if float(item["net_pnl"]) > 0]
    losses = [item for item in trades if float(item["net_pnl"]) < 0]
    gross_profit = sum(float(item["net_pnl"]) for item in wins)
    gross_loss = abs(sum(float(item["net_pnl"]) for item in losses))
    return {
        "period_start": trade_start.isoformat(),
        "period_end": trade_end.isoformat(),
        "initial_balance": round(initial_balance, 2),
        "ending_balance": round(ending_balance, 2),
        "net_pnl": round(ending_balance - initial_balance, 2),
        "net_return_pct": round((ending_balance / initial_balance - 1) * 100, 2),
        "max_drawdown_pct": round(maximum_drawdown, 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "average_trade_pnl": round((ending_balance - initial_balance) / len(trades), 2) if trades else 0.0,
        "rejected_for_risk": rejected_for_risk,
        "rejected_for_data": rejected_for_data,
        "skipped_regime": skipped_regime,
        "by_structure": _group(trades, "structure"),
        "by_underlying": _group(trades, "underlying"),
        "trade_ledger": trades,
    }


def _group(trades: list[dict[str, object]], key: str) -> dict[str, dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for trade in trades:
        groups.setdefault(str(trade.get(key)), []).append(trade)
    return {
        name: {
            "trades": len(items),
            "wins": sum(float(item["net_pnl"]) > 0 for item in items),
            "net_pnl": round(sum(float(item["net_pnl"]) for item in items), 2),
        }
        for name, items in groups.items()
    }


def _training_gate(result: dict[str, object]) -> bool:
    profit_factor = result.get("profit_factor")
    return (
        int(result.get("trades", 0)) >= 5
        and float(result.get("net_pnl", 0)) > 0
        and (profit_factor is None or float(profit_factor) >= 1.0)
        and float(result.get("max_drawdown_pct", 99)) <= 1.5
    )


def _training_score(item: dict[str, object]) -> tuple[float, float, int]:
    result = item["result"]
    profit_factor = result.get("profit_factor")
    return (
        float(profit_factor) if profit_factor is not None else 10.0,
        float(result.get("net_return_pct", 0)) - float(result.get("max_drawdown_pct", 0)),
        int(result.get("trades", 0)),
    )


def _validation_gate_reasons(validation: dict[str, object], combined: dict[str, object]) -> list[str]:
    reasons = []
    if int(validation.get("trades", 0)) < 4:
        reasons.append("Holdout contains fewer than four trades")
    if float(validation.get("net_pnl", 0)) <= 0:
        reasons.append("Holdout net P&L is not positive")
    profit_factor = validation.get("profit_factor")
    if profit_factor is not None and float(profit_factor) < 1.0:
        reasons.append("Holdout profit factor is below 1.0")
    if float(validation.get("max_drawdown_pct", 99)) > 1.5:
        reasons.append("Holdout drawdown exceeds 1.5%")
    if float(combined.get("net_pnl", 0)) <= 0:
        reasons.append("Combined net P&L is not positive")
    return reasons
