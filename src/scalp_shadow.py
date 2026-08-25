from __future__ import annotations

import json
import math
import os
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KOLKATA = ZoneInfo("Asia/Kolkata")


class ScalpShadowError(RuntimeError):
    """Raised when the Release 6 scalp-shadow path must fail closed."""


@dataclass(frozen=True)
class Tick:
    role: str
    symbol: str
    exchange: str
    token: str
    timestamp: datetime
    received_at: datetime
    last_price: float
    bid: float
    ask: float
    cumulative_volume: int = 0
    last_quantity: int = 0
    lot_size: int = 1
    tick_size: float = 0.05

    @property
    def spread_bps(self) -> float:
        midpoint = (self.bid + self.ask) / 2
        if self.bid <= 0 or self.ask < self.bid or midpoint <= 0:
            return math.inf
        return (self.ask - self.bid) / midpoint * 10000

    def public_record(self) -> dict:
        record = asdict(self)
        record["timestamp"] = self.timestamp.isoformat()
        record["received_at"] = self.received_at.isoformat()
        return record

    @classmethod
    def from_record(cls, value: dict) -> Tick:
        return cls(
            role=str(value["role"]),
            symbol=str(value["symbol"]),
            exchange=str(value["exchange"]),
            token=str(value["token"]),
            timestamp=_datetime(value["timestamp"]),
            received_at=_datetime(value.get("received_at", value["timestamp"])),
            last_price=float(value["last_price"]),
            bid=float(value.get("bid", 0)),
            ask=float(value.get("ask", 0)),
            cumulative_volume=int(value.get("cumulative_volume", 0)),
            last_quantity=int(value.get("last_quantity", 0)),
            lot_size=int(value.get("lot_size", 1)),
            tick_size=float(value.get("tick_size", 0.05)),
        )


@dataclass(frozen=True)
class Bar:
    timeframe_seconds: int
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    ticks: int


@dataclass(frozen=True)
class ScalpSignal:
    signal_id: str
    direction: str
    generated_at: datetime
    signal_price: float
    stop_bps: float
    target_bps: float
    reason: str


@dataclass(frozen=True)
class ScalpShadowSettings:
    enabled: bool
    paper_execution_enabled: bool
    signal_instrument: dict
    capital: float
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_trades_per_day: int
    stop_after_consecutive_losses: int
    max_open_positions: int
    max_spread_bps: float
    max_tick_age_ms: int
    slippage_bps: float
    fee_bps: float
    stop_bps: float
    target_bps: float
    max_holding_seconds: int
    cooldown_seconds: int
    min_one_minute_bars: int
    min_five_minute_bars: int
    one_minute_ema_fast: int
    one_minute_ema_slow: int
    five_minute_ema_fast: int
    five_minute_ema_slow: int
    atr_period: int
    minimum_atr_bps: float
    maximum_atr_bps: float
    max_notional_to_capital_ratio: float
    entry_start: time
    entry_end: time
    force_exit: time
    state_file: Path
    tick_log_dir: Path
    decision_log: Path


def load_scalp_shadow_settings(config: object, base_dir: Path) -> ScalpShadowSettings:
    values = validate_scalp_shadow_config(config)

    def path(key: str, default: str) -> Path:
        configured = Path(str(values.get(key, default)))
        return configured if configured.is_absolute() else base_dir / configured

    signal = dict(values.get("signal_instrument") or {})
    return ScalpShadowSettings(
        enabled=bool(values.get("enabled", False)),
        paper_execution_enabled=bool(values.get("paper_execution_enabled", False)),
        signal_instrument=signal,
        capital=float(values.get("capital", 300000)),
        risk_per_trade_pct=float(values.get("risk_per_trade_pct", 0.25)),
        max_daily_loss_pct=float(values.get("max_daily_loss_pct", 0.5)),
        max_trades_per_day=int(values.get("max_trades_per_day", 8)),
        stop_after_consecutive_losses=int(values.get("stop_after_consecutive_losses", 3)),
        max_open_positions=int(values.get("max_open_positions", 1)),
        max_spread_bps=float(values.get("max_spread_bps", 4)),
        max_tick_age_ms=int(values.get("max_tick_age_ms", 2500)),
        slippage_bps=float(values.get("slippage_bps", 0.5)),
        fee_bps=float(values.get("fee_bps", 0.25)),
        stop_bps=float(values.get("stop_bps", 3)),
        target_bps=float(values.get("target_bps", 6)),
        max_holding_seconds=int(values.get("max_holding_seconds", 180)),
        cooldown_seconds=int(values.get("cooldown_seconds", 120)),
        min_one_minute_bars=int(values.get("min_one_minute_bars", 20)),
        min_five_minute_bars=int(values.get("min_five_minute_bars", 8)),
        one_minute_ema_fast=int(values.get("one_minute_ema_fast", 5)),
        one_minute_ema_slow=int(values.get("one_minute_ema_slow", 13)),
        five_minute_ema_fast=int(values.get("five_minute_ema_fast", 3)),
        five_minute_ema_slow=int(values.get("five_minute_ema_slow", 8)),
        atr_period=int(values.get("atr_period", 10)),
        minimum_atr_bps=float(values.get("minimum_atr_bps", 2)),
        maximum_atr_bps=float(values.get("maximum_atr_bps", 35)),
        max_notional_to_capital_ratio=float(values.get("max_notional_to_capital_ratio", 6)),
        entry_start=_clock_time(str(values.get("entry_start", "09:20"))),
        entry_end=_clock_time(str(values.get("entry_end", "15:00"))),
        force_exit=_clock_time(str(values.get("force_exit", "15:10"))),
        state_file=path("state_file", "logs/scalp_shadow_portfolio.json"),
        tick_log_dir=path("tick_log_dir", "logs/scalp_ticks"),
        decision_log=path("decision_log", "logs/scalp_decisions.jsonl"),
    )


def validate_scalp_shadow_config(config: object) -> dict:
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("scalp_shadow must be a mapping")
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("scalp_shadow.enabled must be a boolean")
    for key in ("paper_only", "shadow_mode", "defined_risk_only"):
        if config.get(key, True) is not True:
            raise ValueError(f"scalp_shadow.{key} must remain true")
    if config.get("live_order_enabled", False) is not False:
        raise ValueError("scalp_shadow.live_order_enabled must remain false")
    if not isinstance(config.get("paper_execution_enabled", False), bool):
        raise ValueError("scalp_shadow.paper_execution_enabled must be a boolean")

    signal = config.get("signal_instrument", {})
    if enabled:
        if not isinstance(signal, dict):
            raise ValueError("scalp_shadow.signal_instrument must be a mapping")
        expected = {"exchange": "NSE", "symbol": "NIFTY 50", "token": "99926000"}
        for key, value in expected.items():
            if str(signal.get(key, "")).strip().upper() != value.upper():
                raise ValueError("Release 6 signal instrument must remain NSE NIFTY 50 token 99926000")
        if config.get("execution_instrument", "nearest_nifty_future") != "nearest_nifty_future":
            raise ValueError("Release 6 supports only dynamically resolved nearest NIFTY futures")

    _number(config, "capital", 100000, 10000000, 300000)
    _number(config, "risk_per_trade_pct", 0.05, 0.25, 0.25)
    _number(config, "max_daily_loss_pct", 0.1, 0.75, 0.5)
    _integer(config, "max_trades_per_day", 1, 20, 8)
    _integer(config, "stop_after_consecutive_losses", 1, 5, 3)
    if config.get("max_open_positions", 1) != 1:
        raise ValueError("scalp_shadow.max_open_positions must remain 1")
    _number(config, "max_spread_bps", 0.5, 10, 4)
    _integer(config, "max_tick_age_ms", 250, 5000, 2500)
    _number(config, "slippage_bps", 0, 5, 0.5)
    _number(config, "fee_bps", 0, 5, 0.25)
    _number(config, "stop_bps", 2, 20, 3)
    _number(config, "target_bps", 2, 40, 6)
    if float(config.get("target_bps", 6)) < float(config.get("stop_bps", 3)) * 1.5:
        raise ValueError("scalp_shadow target_bps must be at least 1.5 times stop_bps")
    _integer(config, "max_holding_seconds", 30, 600, 180)
    _integer(config, "cooldown_seconds", 30, 900, 120)
    _integer(config, "min_one_minute_bars", 15, 120, 20)
    _integer(config, "min_five_minute_bars", 6, 60, 8)
    _integer(config, "one_minute_ema_fast", 2, 20, 5)
    _integer(config, "one_minute_ema_slow", 5, 50, 13)
    _integer(config, "five_minute_ema_fast", 2, 20, 3)
    _integer(config, "five_minute_ema_slow", 5, 50, 8)
    if int(config.get("one_minute_ema_fast", 5)) >= int(config.get("one_minute_ema_slow", 13)):
        raise ValueError("scalp_shadow one-minute fast EMA must be below slow EMA")
    if int(config.get("five_minute_ema_fast", 3)) >= int(config.get("five_minute_ema_slow", 8)):
        raise ValueError("scalp_shadow five-minute fast EMA must be below slow EMA")
    _integer(config, "atr_period", 5, 30, 10)
    _number(config, "minimum_atr_bps", 0.5, 20, 2)
    _number(config, "maximum_atr_bps", 5, 100, 35)
    if float(config.get("minimum_atr_bps", 2)) >= float(config.get("maximum_atr_bps", 35)):
        raise ValueError("scalp_shadow ATR bounds are invalid")
    _number(config, "max_notional_to_capital_ratio", 1, 6, 6)
    if float(config.get("max_daily_loss_pct", 0.5)) < float(config.get("risk_per_trade_pct", 0.25)):
        raise ValueError("scalp_shadow max_daily_loss_pct must be at least risk_per_trade_pct")
    _number(config, "execution_tick_size", 0.01, 1, 0.05)
    _number(config, "price_scale", 1, 10000, 100)
    _integer(config, "instrument_master_timeout_seconds", 1, 30, 10)

    entry_start = _clock_time(str(config.get("entry_start", "09:20")))
    entry_end = _clock_time(str(config.get("entry_end", "15:00")))
    force_exit = _clock_time(str(config.get("force_exit", "15:10")))
    if not time(9, 15) <= entry_start < entry_end <= time(15, 5):
        raise ValueError("scalp_shadow entry window is invalid")
    if not entry_end < force_exit <= time(15, 20):
        raise ValueError("scalp_shadow force_exit is invalid")
    for key in ("state_file", "tick_log_dir", "decision_log"):
        value = config.get(key)
        if enabled and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"scalp_shadow.{key} is required when enabled")
    return config


class MultiTimeframeBars:
    def __init__(self, history_size: int = 240) -> None:
        self.history: dict[int, deque[Bar]] = {
            60: deque(maxlen=history_size),
            300: deque(maxlen=history_size),
        }
        self.current: dict[int, dict] = {}
        self.last_timestamp: datetime | None = None
        self.last_cumulative_volume = 0
        self.gap_count = 0
        self.out_of_order_count = 0

    def on_tick(self, tick: Tick) -> dict[int, Bar]:
        timestamp = tick.timestamp.astimezone(KOLKATA)
        if self.last_timestamp is not None and timestamp < self.last_timestamp:
            self.out_of_order_count += 1
            return {}
        self.last_timestamp = timestamp
        volume = _volume_delta(tick, self.last_cumulative_volume)
        if tick.cumulative_volume > 0:
            self.last_cumulative_volume = tick.cumulative_volume

        closed: dict[int, Bar] = {}
        for seconds in (60, 300):
            bucket = _bucket(timestamp, seconds)
            current = self.current.get(seconds)
            if current is None:
                self.current[seconds] = _new_bar(bucket, seconds, tick.last_price, volume)
                continue
            if bucket < current["start"]:
                self.out_of_order_count += 1
                continue
            if bucket > current["start"]:
                bar = Bar(**current)
                expected = current["start"].timestamp() + seconds
                if bucket.timestamp() > expected:
                    self.gap_count += int((bucket.timestamp() - expected) // seconds)
                self.history[seconds].append(bar)
                closed[seconds] = bar
                self.current[seconds] = _new_bar(bucket, seconds, tick.last_price, volume)
                continue
            current["high"] = max(current["high"], tick.last_price)
            current["low"] = min(current["low"], tick.last_price)
            current["close"] = tick.last_price
            current["volume"] += volume
            current["ticks"] += 1
        return closed

    def bars(self, seconds: int) -> list[Bar]:
        return list(self.history[seconds])


class MultiTimeframeScalpStrategy:
    def __init__(self, settings: ScalpShadowSettings) -> None:
        self.settings = settings
        self.last_signal_at: datetime | None = None

    def evaluate(self, bars: MultiTimeframeBars, now: datetime) -> ScalpSignal | None:
        one = bars.bars(60)
        five = bars.bars(300)
        if len(one) < self.settings.min_one_minute_bars:
            return None
        if len(five) < self.settings.min_five_minute_bars:
            return None
        if self.last_signal_at is not None:
            if (now - self.last_signal_at).total_seconds() < self.settings.cooldown_seconds:
                return None

        one_closes = [bar.close for bar in one]
        five_closes = [bar.close for bar in five]
        one_fast = _ema(one_closes, self.settings.one_minute_ema_fast)
        one_slow = _ema(one_closes, self.settings.one_minute_ema_slow)
        five_fast = _ema(five_closes, self.settings.five_minute_ema_fast)
        five_slow = _ema(five_closes, self.settings.five_minute_ema_slow)
        atr_bps = _atr_bps(one, self.settings.atr_period)
        if not self.settings.minimum_atr_bps <= atr_bps <= self.settings.maximum_atr_bps:
            return None

        latest, previous = one[-1], one[-2]
        direction = None
        if one_fast > one_slow and five_fast > five_slow and latest.close > previous.high:
            direction = "BUY"
        elif one_fast < one_slow and five_fast < five_slow and latest.close < previous.low:
            direction = "SELL"
        if direction is None:
            return None

        self.last_signal_at = now
        return ScalpSignal(
            signal_id=f"{latest.end.isoformat()}|{direction}|{latest.close:.4f}",
            direction=direction,
            generated_at=now,
            signal_price=latest.close,
            stop_bps=self.settings.stop_bps,
            target_bps=self.settings.target_bps,
            reason=(
                f"1m EMA {self.settings.one_minute_ema_fast}/{self.settings.one_minute_ema_slow} "
                f"and 5m EMA {self.settings.five_minute_ema_fast}/{self.settings.five_minute_ema_slow} "
                f"aligned with one-minute breakout; ATR {atr_bps:.2f} bps"
            ),
        )


class ScalpTickRecorder:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, tick: Tick) -> None:
        path = self.directory / f"ticks-{tick.timestamp.astimezone(KOLKATA).date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(tick.public_record(), separators=(",", ":")) + "\n")
        os.chmod(path, 0o600)


class ScalpAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, payload: dict) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "release": "6.0",
            "mode": "scalp_shadow_paper",
            "event": event,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
        os.chmod(self.path, 0o600)


class ScalpPaperBroker:
    def __init__(
        self,
        settings: ScalpShadowSettings,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(KOLKATA))
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex[:12].upper())
        self.state = self._load_state()

    def place(self, signal: ScalpSignal, quote: Tick) -> dict:
        now = self.clock().astimezone(KOLKATA)
        rejection = self._entry_rejection(signal, quote, now)
        if rejection:
            return {"accepted": False, "reason": rejection, "signal_id": signal.signal_id}

        side = signal.direction
        raw_fill = quote.ask if side == "BUY" else quote.bid
        fill = _adverse_price(raw_fill, side, self.settings.slippage_bps, quote.tick_size)
        quantity = quote.lot_size
        stop = fill * (1 - signal.stop_bps / 10000) if side == "BUY" else fill * (
            1 + signal.stop_bps / 10000
        )
        target = fill * (1 + signal.target_bps / 10000) if side == "BUY" else fill * (
            1 - signal.target_bps / 10000
        )
        stop = _round_tick(stop, quote.tick_size)
        target = _round_tick(target, quote.tick_size)
        entry_fee = fill * quantity * self.settings.fee_bps / 10000
        estimated_exit_fee = stop * quantity * self.settings.fee_bps / 10000
        risk_exit_side = "SELL" if side == "BUY" else "BUY"
        risk_exit = _adverse_price(stop, risk_exit_side, self.settings.slippage_bps, quote.tick_size)
        estimated_exit_fee = risk_exit * quantity * self.settings.fee_bps / 10000
        maximum_loss = abs(fill - risk_exit) * quantity + entry_fee + estimated_exit_fee
        risk_budget = self.settings.capital * self.settings.risk_per_trade_pct / 100
        if maximum_loss > risk_budget:
            return {
                "accepted": False,
                "reason": "One-lot maximum loss exceeds scalp risk budget",
                "maximum_loss": round(maximum_loss, 2),
                "risk_budget": round(risk_budget, 2),
                "signal_id": signal.signal_id,
            }
        notional = fill * quantity
        if notional > self.settings.capital * self.settings.max_notional_to_capital_ratio:
            return {
                "accepted": False,
                "reason": "One-lot futures notional exceeds scalp exposure cap",
                "notional": round(notional, 2),
                "maximum_notional": round(
                    self.settings.capital * self.settings.max_notional_to_capital_ratio,
                    2,
                ),
                "signal_id": signal.signal_id,
            }
        daily_realized = self._daily_realized(now)
        daily_limit = self.settings.capital * self.settings.max_daily_loss_pct / 100
        if max(0.0, -daily_realized) + maximum_loss > daily_limit:
            return {
                "accepted": False,
                "reason": "Projected stop would exceed remaining daily scalp loss budget",
                "daily_realized_pnl": round(daily_realized, 2),
                "maximum_loss": round(maximum_loss, 2),
                "daily_loss_limit": round(daily_limit, 2),
                "signal_id": signal.signal_id,
            }

        order_id = f"SCALP-PAPER-{self.id_factory()}"
        position = {
            "order_id": order_id,
            "signal_id": signal.signal_id,
            "symbol": quote.symbol,
            "exchange": quote.exchange,
            "token": quote.token,
            "side": side,
            "quantity": quantity,
            "entry_price": round(fill, 4),
            "entry_time": now.isoformat(),
            "entry_fee": round(entry_fee, 2),
            "stop_price": stop,
            "target_price": target,
            "maximum_loss": round(maximum_loss, 2),
            "last_price": quote.last_price,
            "unrealized_pnl": -round(entry_fee, 2),
        }
        self.state["open_position"] = position
        self.state["seen_signal_ids"].append(signal.signal_id)
        self.state["seen_signal_ids"] = self.state["seen_signal_ids"][-5000:]
        self.state["orders"].append({"event": "ENTRY", **position})
        self._save()
        return {"accepted": True, **position, "risk_budget": round(risk_budget, 2)}

    def mark(self, quote: Tick, *, reason: str | None = None) -> dict | None:
        position = self.state.get("open_position")
        if not isinstance(position, dict) or quote.token != position.get("token"):
            return None
        now = self.clock().astimezone(KOLKATA)
        side = position["side"]
        exit_reference = quote.bid if side == "BUY" else quote.ask
        if exit_reference <= 0:
            return None
        gross = (
            (exit_reference - float(position["entry_price"])) * int(position["quantity"])
            if side == "BUY"
            else (float(position["entry_price"]) - exit_reference) * int(position["quantity"])
        )
        position["last_price"] = quote.last_price
        position["unrealized_pnl"] = round(gross - float(position["entry_fee"]), 2)

        exit_reason = reason
        if exit_reason is None:
            if side == "BUY" and exit_reference <= float(position["stop_price"]):
                exit_reason = "stop_loss"
            elif side == "SELL" and exit_reference >= float(position["stop_price"]):
                exit_reason = "stop_loss"
            elif side == "BUY" and exit_reference >= float(position["target_price"]):
                exit_reason = "profit_target"
            elif side == "SELL" and exit_reference <= float(position["target_price"]):
                exit_reason = "profit_target"
            elif (now - _datetime(position["entry_time"])).total_seconds() >= self.settings.max_holding_seconds:
                exit_reason = "time_exit"
            elif now.time().replace(tzinfo=None) >= self.settings.force_exit:
                exit_reason = "force_exit"

        if exit_reason is None:
            self._save()
            return None
        return self._close(position, quote, now, exit_reason)

    def snapshot(self) -> dict:
        position = self.state.get("open_position")
        unrealized = float(position.get("unrealized_pnl", 0)) if isinstance(position, dict) else 0.0
        return {
            "initial_balance": self.state["initial_balance"],
            "balance": self.state["balance"],
            "equity": round(float(self.state["balance"]) + unrealized, 2),
            "realized_pnl": round(float(self.state["balance"]) - float(self.state["initial_balance"]), 2),
            "unrealized_pnl": round(unrealized, 2),
            "open_position_count": 1 if isinstance(position, dict) else 0,
            "open_position": position,
            "closed_trade_count": len(self.state["trades"]),
            "trades": list(self.state["trades"]),
        }

    def _entry_rejection(self, signal: ScalpSignal, quote: Tick, now: datetime) -> str | None:
        if self.state.get("open_position") is not None:
            return "Maximum one scalp position is already open"
        if signal.signal_id in self.state["seen_signal_ids"]:
            return "Duplicate scalp signal"
        if now.weekday() >= 5 or not self.settings.entry_start <= now.time().replace(tzinfo=None) <= self.settings.entry_end:
            return "Scalp entry is outside the configured market window"
        if quote.bid <= 0 or quote.ask < quote.bid or quote.spread_bps > self.settings.max_spread_bps:
            return "Execution quote spread is missing or too wide"
        today = now.date().isoformat()
        trades_today = [trade for trade in self.state["trades"] if str(trade["exit_time"]).startswith(today)]
        entries_today = [order for order in self.state["orders"] if order["event"] == "ENTRY" and str(order["entry_time"]).startswith(today)]
        if len(entries_today) >= self.settings.max_trades_per_day:
            return "Daily scalp trade limit reached"
        daily_pnl = sum(float(trade["net_pnl"]) for trade in trades_today)
        daily_limit = self.settings.capital * self.settings.max_daily_loss_pct / 100
        if daily_pnl <= -daily_limit:
            return "Daily scalp loss limit reached"
        consecutive = 0
        for trade in reversed(trades_today):
            if float(trade["net_pnl"]) < 0:
                consecutive += 1
            else:
                break
        if consecutive >= self.settings.stop_after_consecutive_losses:
            return "Consecutive scalp loss limit reached"
        return None

    def _daily_realized(self, now: datetime) -> float:
        today = now.date().isoformat()
        return sum(
            float(trade["net_pnl"])
            for trade in self.state["trades"]
            if str(trade.get("exit_time", "")).startswith(today)
        )

    def _close(self, position: dict, quote: Tick, now: datetime, reason: str) -> dict:
        exit_side = "SELL" if position["side"] == "BUY" else "BUY"
        raw_exit = quote.bid if exit_side == "SELL" else quote.ask
        fill = _adverse_price(raw_exit, exit_side, self.settings.slippage_bps, quote.tick_size)
        quantity = int(position["quantity"])
        gross = (
            (fill - float(position["entry_price"])) * quantity
            if position["side"] == "BUY"
            else (float(position["entry_price"]) - fill) * quantity
        )
        exit_fee = fill * quantity * self.settings.fee_bps / 10000
        net = round(gross - float(position["entry_fee"]) - exit_fee, 2)
        trade = {
            **position,
            "exit_price": round(fill, 4),
            "exit_time": now.isoformat(),
            "exit_fee": round(exit_fee, 2),
            "gross_pnl": round(gross, 2),
            "net_pnl": net,
            "exit_reason": reason,
        }
        self.state["balance"] = round(float(self.state["balance"]) + net, 2)
        self.state["trades"].append(trade)
        self.state["orders"].append({"event": "EXIT", **trade})
        self.state["open_position"] = None
        self._save()
        return {"accepted": True, "event": "EXIT", **trade}

    def _load_state(self) -> dict:
        path = self.settings.state_file
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                raise ScalpShadowError("Scalp paper state is unreadable; refusing to reset it") from None
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ScalpShadowError("Scalp paper state version is invalid")
            return state
        return {
            "version": 1,
            "initial_balance": self.settings.capital,
            "balance": self.settings.capital,
            "open_position": None,
            "trades": [],
            "orders": [],
            "seen_signal_ids": [],
        }

    def _save(self) -> None:
        path = self.settings.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)


class ScalpShadowEngine:
    def __init__(
        self,
        settings: ScalpShadowSettings,
        *,
        execution_token: str,
        kill_switch_active: bool,
        auto_paper_enabled: bool,
        clock: Callable[[], datetime] | None = None,
        record_ticks: bool = True,
    ) -> None:
        self.settings = settings
        self.execution_token = execution_token
        self.kill_switch_active = kill_switch_active
        self.auto_paper_enabled = auto_paper_enabled
        self.clock = clock or (lambda: datetime.now(KOLKATA))
        self.record_ticks = record_ticks
        self.bars = MultiTimeframeBars()
        self.strategy = MultiTimeframeScalpStrategy(settings)
        self.recorder = ScalpTickRecorder(settings.tick_log_dir)
        self.audit = ScalpAuditLog(settings.decision_log)
        self.broker = ScalpPaperBroker(settings, clock=self.clock)
        self.latest_execution_tick: Tick | None = None
        self.last_tick_at: datetime | None = None
        self.last_signal_tick_at: datetime | None = None
        self.last_execution_tick_at: datetime | None = None
        self.rejected_stale_ticks = 0

    def on_tick(self, tick: Tick) -> None:
        if self.record_ticks:
            self.recorder.write(tick)
        age_ms = (tick.received_at - tick.timestamp).total_seconds() * 1000
        if age_ms < -250 or age_ms > self.settings.max_tick_age_ms:
            self.rejected_stale_ticks += 1
            self.audit.write("tick_rejected", {"symbol": tick.symbol, "reason": "stale_or_future", "age_ms": round(age_ms, 2)})
            return
        self.last_tick_at = tick.received_at

        if tick.role == "execution" and tick.token == self.execution_token:
            self.last_execution_tick_at = tick.received_at
            self.latest_execution_tick = tick
            exit_order = self.broker.mark(tick)
            if exit_order:
                self.audit.write("paper_exit", exit_order)
            return
        if tick.role != "signal" or tick.token != str(self.settings.signal_instrument["token"]):
            return
        self.last_signal_tick_at = tick.received_at

        closed = self.bars.on_tick(tick)
        if 60 not in closed:
            return
        signal = self.strategy.evaluate(self.bars, tick.timestamp)
        if signal is None:
            return
        payload = asdict(signal)
        payload["generated_at"] = signal.generated_at.isoformat()
        self.audit.write("signal", payload)
        if not self.settings.paper_execution_enabled:
            self.audit.write("paper_entry_blocked", {**payload, "reason": "paper_execution_disabled"})
            return
        if self.kill_switch_active:
            self.audit.write("paper_entry_blocked", {**payload, "reason": "kill_switch_active"})
            return
        if not self.auto_paper_enabled:
            self.audit.write("paper_entry_blocked", {**payload, "reason": "auto_paper_disabled"})
            return
        quote = self.latest_execution_tick
        if quote is None:
            self.audit.write("paper_entry_blocked", {**payload, "reason": "execution_quote_missing"})
            return
        quote_age = (tick.received_at - quote.received_at).total_seconds() * 1000
        if quote_age < -250 or quote_age > self.settings.max_tick_age_ms:
            self.audit.write("paper_entry_blocked", {**payload, "reason": "execution_quote_stale", "age_ms": round(quote_age, 2)})
            return
        order = self.broker.place(signal, quote)
        self.audit.write("paper_entry" if order.get("accepted") else "paper_entry_rejected", order)

    def health(self) -> dict:
        now = self.clock()
        feed_times = (self.last_signal_tick_at, self.last_execution_tick_at)
        stale = any(
            value is None or (now - value).total_seconds() * 1000 > self.settings.max_tick_age_ms
            for value in feed_times
        )
        return {
            "status": "stale" if stale else "ok",
            "release": "6.0",
            "mode": "paper_shadow",
            "live_orders_available": False,
            "paper_execution_enabled": self.settings.paper_execution_enabled,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_signal_tick_at": (
                self.last_signal_tick_at.isoformat() if self.last_signal_tick_at else None
            ),
            "last_execution_tick_at": (
                self.last_execution_tick_at.isoformat() if self.last_execution_tick_at else None
            ),
            "rejected_stale_ticks": self.rejected_stale_ticks,
            "one_minute_bars": len(self.bars.bars(60)),
            "five_minute_bars": len(self.bars.bars(300)),
            "bar_gaps": self.bars.gap_count,
            "out_of_order_ticks": self.bars.out_of_order_count,
            "portfolio": self.broker.snapshot(),
        }


def replay_ticks(
    engine: ScalpShadowEngine,
    ticks: Iterable[Tick],
    *,
    advance_clock: Callable[[datetime], None] | None = None,
) -> dict:
    count = 0
    first = None
    last = None
    last_execution_tick = None
    for tick in sorted(ticks, key=lambda item: (item.timestamp, item.role)):
        if advance_clock is not None:
            advance_clock(tick.received_at)
        count += 1
        first = first or tick.timestamp
        last = tick.timestamp
        if tick.role == "execution":
            last_execution_tick = tick
        engine.on_tick(tick)
    if engine.broker.snapshot()["open_position_count"] and last_execution_tick is not None:
        exit_order = engine.broker.mark(last_execution_tick, reason="end_of_data")
        if exit_order:
            engine.audit.write("paper_exit", exit_order)
    snapshot = engine.broker.snapshot()
    trades = snapshot["trades"]
    wins = sum(1 for trade in trades if float(trade["net_pnl"]) > 0)
    losses = sum(1 for trade in trades if float(trade["net_pnl"]) <= 0)
    gross_profit = sum(max(0.0, float(trade["net_pnl"])) for trade in trades)
    gross_loss = abs(sum(min(0.0, float(trade["net_pnl"])) for trade in trades))
    performance = _replay_performance(trades, float(snapshot["initial_balance"]), first, last)
    return {
        "release": "6.0",
        "mode": "tick_replay_paper",
        "ticks": count,
        "period_start": first.isoformat() if first else None,
        "period_end": last.isoformat() if last else None,
        "initial_balance": snapshot["initial_balance"],
        "ending_balance": snapshot["balance"],
        "net_pnl": snapshot["realized_pnl"],
        "net_return_pct": round(snapshot["realized_pnl"] / snapshot["initial_balance"] * 100, 4),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        **performance,
        "open_position_count": snapshot["open_position_count"],
        "data_quality": engine.health(),
        "trade_ledger": trades,
        "limitations": (
            "Paper replay uses recorded Angel One ticks and modeled bid/ask fills, fees and slippage. "
            "It is not an exchange queue-position or partial-fill reconstruction."
        ),
    }


def _replay_performance(
    trades: list[dict],
    initial_balance: float,
    first: datetime | None,
    last: datetime | None,
) -> dict:
    equity = initial_balance
    peak = initial_balance
    max_drawdown_pct = 0.0
    returns = []
    exposure_seconds = 0.0
    total_fees = 0.0
    for trade in sorted(trades, key=lambda value: str(value.get("exit_time", ""))):
        pnl = float(trade["net_pnl"])
        returns.append(pnl / equity if equity > 0 else 0.0)
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - equity) / peak * 100)
        exposure_seconds += max(
            0.0,
            (_datetime(trade["exit_time"]) - _datetime(trade["entry_time"])).total_seconds(),
        )
        total_fees += float(trade.get("entry_fee", 0)) + float(trade.get("exit_fee", 0))

    mean = sum(returns) / len(returns) if returns else 0.0
    variance = (
        sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        if len(returns) > 1
        else 0.0
    )
    downside = [min(0.0, value) for value in returns]
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    period_seconds = max(0.0, (last - first).total_seconds()) if first and last else 0.0
    return {
        "average_trade_pnl": round(sum(float(trade["net_pnl"]) for trade in trades) / len(trades), 2)
        if trades
        else 0.0,
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "trade_sharpe": round(mean / math.sqrt(variance), 3) if variance > 0 else None,
        "trade_sortino": round(mean / downside_deviation, 3) if downside_deviation > 0 else None,
        "exposure_pct": round(exposure_seconds / period_seconds * 100, 4) if period_seconds else 0.0,
        "total_modeled_fees": round(total_fees, 2),
    }


def _new_bar(start: datetime, seconds: int, price: float, volume: int) -> dict:
    return {
        "timeframe_seconds": seconds,
        "start": start,
        "end": datetime.fromtimestamp(start.timestamp() + seconds, tz=start.tzinfo),
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": volume,
        "ticks": 1,
    }


def _bucket(value: datetime, seconds: int) -> datetime:
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=value.tzinfo)


def _volume_delta(tick: Tick, previous: int) -> int:
    if tick.cumulative_volume > 0:
        return max(0, tick.cumulative_volume - previous) if previous > 0 else 0
    return max(0, tick.last_quantity)


def _ema(values: list[float], period: int) -> float:
    multiplier = 2 / (period + 1)
    value = values[0]
    for item in values[1:]:
        value = item * multiplier + value * (1 - multiplier)
    return value


def _atr_bps(bars: list[Bar], period: int) -> float:
    selected = bars[-period:]
    if not selected or selected[-1].close <= 0:
        return 0.0
    ranges = []
    previous_close = selected[0].open
    for bar in selected:
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
        previous_close = bar.close
    return sum(ranges) / len(ranges) / selected[-1].close * 10000


def _adverse_price(price: float, side: str, slippage_bps: float, tick_size: float) -> float:
    adjusted = price * (1 + slippage_bps / 10000) if side == "BUY" else price * (1 - slippage_bps / 10000)
    return _round_tick(adjusted, tick_size)


def _round_tick(value: float, tick_size: float) -> float:
    return round(round(value / tick_size) * tick_size, 4)


def _clock_time(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise ValueError("scalp_shadow times must use HH:MM format") from None


def _datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=KOLKATA) if parsed.tzinfo is None else parsed.astimezone(KOLKATA)


def _number(config: dict, key: str, minimum: float, maximum: float, default: float) -> None:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= float(value) <= maximum:
        raise ValueError(f"scalp_shadow.{key} must be between {minimum} and {maximum}")


def _integer(config: dict, key: str, minimum: int, maximum: int, default: int) -> None:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"scalp_shadow.{key} must be an integer between {minimum} and {maximum}")
