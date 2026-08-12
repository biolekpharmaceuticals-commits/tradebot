from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timezone
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from .demo_data import demo_candles
from .safety import LiveTradingDisabledError
from .strategy import TradeSignal


@dataclass(frozen=True)
class OrderResult:
    accepted: bool
    order_id: str
    message: str
    status: str = "FILLED"
    symbol: str = ""
    side: str = ""
    quantity: int = 0
    fill_price: float = 0.0
    fees: float = 0.0
    timestamp: str = ""


@dataclass(frozen=True)
class PaperExecutionSettings:
    enabled: bool
    initial_balance: float
    max_open_positions: int
    slippage_bps: float
    fee_bps: float
    max_holding_minutes: int
    market_hours_only: bool
    max_candle_age_minutes: int
    state_file: str | None


def load_paper_execution_settings(config: object) -> PaperExecutionSettings:
    values = config if isinstance(config, dict) else {}
    return PaperExecutionSettings(
        enabled=bool(values.get("enabled", False)),
        initial_balance=float(values.get("initial_balance", 100000)),
        max_open_positions=int(values.get("max_open_positions", 2)),
        slippage_bps=float(values.get("slippage_bps", 5)),
        fee_bps=float(values.get("fee_bps", 10)),
        max_holding_minutes=int(values.get("max_holding_minutes", 120)),
        market_hours_only=bool(values.get("market_hours_only", True)),
        max_candle_age_minutes=int(values.get("max_candle_age_minutes", 10)),
        state_file=str(values["state_file"]).strip() if values.get("state_file") else None,
    )


def validate_paper_execution_config(config: object) -> None:
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("paper_execution must be a mapping")
    if not isinstance(config.get("enabled", False), bool):
        raise ValueError("paper_execution.enabled must be a boolean")
    _bounded_number(config, "initial_balance", 1000, 100000000, 100000)
    _bounded_integer(config, "max_open_positions", 1, 10, 2)
    _bounded_number(config, "slippage_bps", 0, 100, 5)
    _bounded_number(config, "fee_bps", 0, 100, 10)
    _bounded_integer(config, "max_holding_minutes", 5, 1440, 120)
    if not isinstance(config.get("market_hours_only", True), bool):
        raise ValueError("paper_execution.market_hours_only must be a boolean")
    _bounded_integer(config, "max_candle_age_minutes", 1, 60, 10)
    state_file = config.get("state_file")
    if state_file is not None and (not isinstance(state_file, str) or not state_file.strip()):
        raise ValueError("paper_execution.state_file must be a non-empty path")


class Broker(Protocol):
    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        ...

    def place_order(self, symbol_config: dict, signal: TradeSignal, quantity: int) -> OrderResult:
        ...


class PaperBroker:
    """Persistent paper fill engine. It has no connection to a live order API."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        initial_balance: float = 100000.0,
        max_open_positions: int = 2,
        slippage_bps: float = 5.0,
        fee_bps: float = 10.0,
        max_holding_minutes: int = 120,
        market_hours_only: bool = False,
        max_candle_age_minutes: int = 10,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.state_path = state_path
        self.initial_balance = float(initial_balance)
        self.max_open_positions = int(max_open_positions)
        self.slippage_bps = float(slippage_bps)
        self.fee_bps = float(fee_bps)
        self.max_holding_minutes = int(max_holding_minutes)
        self.market_hours_only = bool(market_hours_only)
        self.max_candle_age_minutes = int(max_candle_age_minutes)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: uuid4().hex[:12].upper())
        self._memory_state = self._empty_state()

    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        return demo_candles()

    def place_order(self, symbol_config: dict, signal: TradeSignal, quantity: int) -> OrderResult:
        symbol = str(symbol_config.get("symbol", "UNKNOWN"))
        now = self._now()
        decision_id = str(
            symbol_config.get("paper_decision_id")
            or f"{symbol}|{signal.decision}|{signal.entry_price}|{symbol_config.get('timeframe', '')}"
        )

        rejection = self._entry_rejection(symbol_config, signal, quantity, decision_id)
        if rejection:
            return OrderResult(
                False,
                "",
                rejection,
                status="REJECTED",
                symbol=symbol,
                side=signal.decision,
                quantity=max(0, int(quantity)),
                timestamp=now.isoformat(),
            )

        state = self._load_state()
        if any(order.get("decision_id") == decision_id for order in state["orders"]):
            return self._rejected(symbol, signal.decision, quantity, now, "Duplicate paper decision blocked")
        if any(position.get("symbol") == symbol for position in state["open_positions"]):
            return self._rejected(symbol, signal.decision, quantity, now, "Paper position already open")
        if len(state["open_positions"]) >= self.max_open_positions:
            return self._rejected(symbol, signal.decision, quantity, now, "Maximum open paper positions reached")

        fill_price = self._entry_fill(float(signal.entry_price), signal.decision)
        fees = self._fees(fill_price, quantity)
        order_id = f"PAPER-{self.id_factory()}"
        timestamp = now.isoformat()
        position = {
            "position_id": order_id,
            "symbol": symbol,
            "side": signal.decision,
            "quantity": int(quantity),
            "entry_price": fill_price,
            "entry_fees": fees,
            "stop_loss": float(signal.stop_loss),
            "target": float(signal.target),
            "opened_at": timestamp,
            "opened_candle_at": symbol_config.get("paper_candle_timestamp"),
            "decision_id": decision_id,
            "instrument": self._instrument_snapshot(symbol_config),
        }
        order = {
            "order_id": order_id,
            "kind": "ENTRY",
            "status": "FILLED",
            "symbol": symbol,
            "side": signal.decision,
            "quantity": int(quantity),
            "fill_price": fill_price,
            "fees": fees,
            "timestamp": timestamp,
            "decision_id": decision_id,
        }
        state["open_positions"].append(position)
        state["orders"].append(order)
        self._save_state(state)
        return OrderResult(
            True,
            order_id,
            "Paper order filled",
            symbol=symbol,
            side=signal.decision,
            quantity=int(quantity),
            fill_price=fill_price,
            fees=fees,
            timestamp=timestamp,
        )

    def open_position_configs(self) -> list[dict]:
        return [dict(item.get("instrument", {})) for item in self._load_state()["open_positions"]]

    def reconcile(self, symbol_config: dict, candles: pd.DataFrame) -> list[OrderResult]:
        if candles is None or candles.empty:
            return []
        symbol = str(symbol_config.get("symbol", ""))
        state = self._load_state()
        position = next((item for item in state["open_positions"] if item.get("symbol") == symbol), None)
        if position is None:
            return []

        opened_candle_at = _timestamp(position.get("opened_candle_at"))
        rows = candles.copy()
        if "timestamp" in rows:
            rows["timestamp"] = pd.to_datetime(rows["timestamp"], errors="coerce", utc=True)
            if opened_candle_at is not None:
                rows = rows[rows["timestamp"] > opened_candle_at]
        if rows.empty:
            return []

        for _, candle in rows.iterrows():
            outcome, raw_exit = self._exit_for_candle(position, candle, opened_candle_at)
            if outcome:
                return [self._close_position(state, position, raw_exit, outcome, candle)]
        return []

    def snapshot(self) -> dict[str, object]:
        state = self._load_state()
        realized = round(float(state.get("realized_pnl", 0.0)), 2)
        return {
            "starting_balance": self.initial_balance,
            "paper_balance": round(self.initial_balance + realized, 2),
            "realized_pnl": realized,
            "open_position_count": len(state["open_positions"]),
            "closed_trade_count": len(state["closed_positions"]),
            "open_positions": list(state["open_positions"]),
            "recent_orders": list(reversed(state["orders"][-20:])),
        }

    def risk_snapshot(self) -> dict[str, float | int]:
        state = self._load_state()
        today = self._now().astimezone(ZoneInfo("Asia/Kolkata")).date()
        entry_orders = [
            item
            for item in state["orders"]
            if item.get("kind") == "ENTRY" and _local_date(item.get("timestamp")) == today
        ]
        closed_today = [
            item for item in state["closed_positions"] if _local_date(item.get("closed_at")) == today
        ]
        consecutive_losses = 0
        for item in reversed(state["closed_positions"]):
            if float(item.get("net_pnl", 0.0)) < 0:
                consecutive_losses += 1
            else:
                break
        return {
            "trades_today": len(entry_orders),
            "realized_pnl": round(sum(float(item.get("net_pnl", 0.0)) for item in closed_today), 2),
            "consecutive_losses": consecutive_losses,
        }

    def _entry_rejection(
        self, symbol_config: dict, signal: TradeSignal, quantity: int, decision_id: str
    ) -> str | None:
        if not decision_id:
            return "Missing paper decision identifier"
        if signal.decision not in {"BUY", "SELL"}:
            return "Paper order requires a directional signal"
        if int(quantity) <= 0:
            return "Paper order quantity must be positive"
        required = symbol_config.get("required_decision")
        if required and signal.decision != required:
            return f"Contract requires {required} confirmation"
        if symbol_config.get("derivative_type") in {"call", "put"} and signal.decision != "BUY":
            return "Option selling is prohibited"
        if self.market_hours_only:
            now_ist = self._now().astimezone(ZoneInfo("Asia/Kolkata"))
            if now_ist.weekday() >= 5 or not wall_time(9, 15) <= now_ist.time().replace(tzinfo=None) <= wall_time(15, 30):
                return "Paper entries are restricted to market hours"
            candle_time = _timestamp(symbol_config.get("paper_candle_timestamp"))
            if candle_time is None:
                return "A current candle is required for paper execution"
            age_minutes = (pd.Timestamp(self._now()) - candle_time).total_seconds() / 60
            if age_minutes < -2 or age_minutes > self.max_candle_age_minutes:
                return "Latest candle is stale for paper execution"
        return None

    def _exit_for_candle(self, position: dict, candle, opened_candle_at) -> tuple[str | None, float]:
        side = position["side"]
        stop = float(position["stop_loss"])
        target = float(position["target"])
        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])

        if side == "BUY":
            if open_price <= stop:
                return "stop_gap", open_price
            if open_price >= target:
                return "target_gap", open_price
            if low <= stop:
                return "stop", stop
            if high >= target:
                return "target", target
        else:
            if open_price >= stop:
                return "stop_gap", open_price
            if open_price <= target:
                return "target_gap", open_price
            if high >= stop:
                return "stop", stop
            if low <= target:
                return "target", target

        candle_time = _timestamp(candle.get("timestamp"))
        if opened_candle_at is not None and candle_time is not None:
            held_minutes = (candle_time - opened_candle_at).total_seconds() / 60
            if held_minutes >= self.max_holding_minutes:
                return "time_exit", float(candle["close"])
        return None, 0.0

    def _close_position(self, state: dict, position: dict, raw_exit: float, outcome: str, candle) -> OrderResult:
        exit_side = "SELL" if position["side"] == "BUY" else "BUY"
        exit_price = self._exit_fill(raw_exit, exit_side)
        quantity = int(position["quantity"])
        exit_fees = self._fees(exit_price, quantity)
        entry = float(position["entry_price"])
        gross = (exit_price - entry) * quantity if position["side"] == "BUY" else (entry - exit_price) * quantity
        net = round(gross - float(position["entry_fees"]) - exit_fees, 2)
        now = self._now()
        order_id = f"PAPER-{self.id_factory()}"
        closed_at = now.isoformat()
        order = {
            "order_id": order_id,
            "kind": "EXIT",
            "status": "FILLED",
            "symbol": position["symbol"],
            "side": exit_side,
            "quantity": quantity,
            "fill_price": exit_price,
            "fees": exit_fees,
            "timestamp": closed_at,
            "position_id": position["position_id"],
            "outcome": outcome,
        }
        closed = {
            **position,
            "exit_order_id": order_id,
            "exit_price": exit_price,
            "exit_fees": exit_fees,
            "outcome": outcome,
            "closed_at": closed_at,
            "closed_candle_at": str(candle.get("timestamp", "")),
            "net_pnl": net,
        }
        state["open_positions"] = [item for item in state["open_positions"] if item is not position]
        state["closed_positions"].append(closed)
        state["orders"].append(order)
        state["realized_pnl"] = round(float(state.get("realized_pnl", 0.0)) + net, 2)
        self._save_state(state)
        return OrderResult(
            True,
            order_id,
            f"Paper position closed: {outcome}",
            symbol=position["symbol"],
            side=exit_side,
            quantity=quantity,
            fill_price=exit_price,
            fees=exit_fees,
            timestamp=closed_at,
        )

    def _load_state(self) -> dict:
        if self.state_path is None:
            return self._memory_state
        if not self.state_path.exists():
            return self._empty_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("Paper portfolio state could not be read") from None
        if not isinstance(value, dict) or value.get("version") != 1:
            raise RuntimeError("Paper portfolio state was invalid")
        for key in ("open_positions", "closed_positions", "orders"):
            if not isinstance(value.get(key), list):
                raise RuntimeError("Paper portfolio state was invalid")
        return value

    def _save_state(self, state: dict) -> None:
        if self.state_path is None:
            self._memory_state = state
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        temporary.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)

    def _empty_state(self) -> dict:
        return {
            "version": 1,
            "initial_balance": self.initial_balance,
            "realized_pnl": 0.0,
            "open_positions": [],
            "closed_positions": [],
            "orders": [],
        }

    def _rejected(self, symbol: str, side: str, quantity: int, now: datetime, reason: str) -> OrderResult:
        return OrderResult(
            False,
            "",
            reason,
            status="REJECTED",
            symbol=symbol,
            side=side,
            quantity=max(0, int(quantity)),
            timestamp=now.isoformat(),
        )

    def _entry_fill(self, price: float, side: str) -> float:
        multiplier = 1 + self.slippage_bps / 10000 if side == "BUY" else 1 - self.slippage_bps / 10000
        return round(price * multiplier, 4)

    def _exit_fill(self, price: float, side: str) -> float:
        multiplier = 1 + self.slippage_bps / 10000 if side == "BUY" else 1 - self.slippage_bps / 10000
        return round(price * multiplier, 4)

    def _fees(self, price: float, quantity: int) -> float:
        return round(price * int(quantity) * self.fee_bps / 10000, 2)

    def _now(self) -> datetime:
        value = self.clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _instrument_snapshot(symbol_config: dict) -> dict:
        allowed = {
            "exchange",
            "symbol",
            "token",
            "quantity",
            "timeframe",
            "instrument_type",
            "derivative_type",
            "underlying",
            "expiry",
            "strike",
            "lot_size",
            "required_decision",
        }
        return {key: symbol_config.get(key) for key in allowed if symbol_config.get(key) is not None}


class AngelOneBroker:
    def __init__(self, config: dict) -> None:
        self.config = config
        self._smart = None

    def _client(self):
        raise LiveTradingDisabledError("Angel One live order API access is disabled in Release 5")

    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        raise NotImplementedError("Live broker candle access is not used")

    def place_order(self, symbol_config: dict, signal: TradeSignal, quantity: int) -> OrderResult:
        raise LiveTradingDisabledError("Live order submission is disabled in Release 5")


def _timestamp(value) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(parsed) else parsed


def _local_date(value):
    parsed = _timestamp(value)
    return None if parsed is None else parsed.tz_convert(ZoneInfo("Asia/Kolkata")).date()


def _bounded_number(config: dict, key: str, minimum: float, maximum: float, default: float) -> None:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"paper_execution.{key} must be between {minimum} and {maximum}")


def _bounded_integer(config: dict, key: str, minimum: int, maximum: int, default: int) -> None:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"paper_execution.{key} must be an integer between {minimum} and {maximum}")
