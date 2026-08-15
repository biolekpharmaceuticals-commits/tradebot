from __future__ import annotations

import json
import os
from datetime import datetime, time as wall_time, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .broker import OrderResult


class DefinedRiskOptionPaperBroker:
    """Persistent multi-leg simulator with no live order transport."""

    def __init__(
        self,
        *,
        state_path: Path,
        initial_balance: float,
        risk_limit_pct: float,
        daily_loss_limit_pct: float,
        max_trades_per_day: int,
        stop_after_consecutive_losses: int,
        slippage_bps: float,
        fee_bps: float,
        entry_start: str,
        entry_end: str,
        force_exit: str,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.state_path = state_path
        self.initial_balance = float(initial_balance)
        self.risk_limit = self.initial_balance * float(risk_limit_pct) / 100
        self.daily_loss_limit = self.initial_balance * float(daily_loss_limit_pct) / 100
        self.max_trades_per_day = int(max_trades_per_day)
        self.stop_after_consecutive_losses = int(stop_after_consecutive_losses)
        self.slippage_bps = float(slippage_bps)
        self.fee_bps = float(fee_bps)
        self.entry_start = _clock_time(entry_start)
        self.entry_end = _clock_time(entry_end)
        self.force_exit = _clock_time(force_exit)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: uuid4().hex[:12].upper())

    def place_structure(self, structure: dict, decision_id: str) -> OrderResult:
        now = self._now()
        local = now.astimezone(ZoneInfo("Asia/Kolkata"))
        symbol = str(structure.get("underlying", "NIFTY 50"))
        lot_size = int(structure.get("lot_size", 0))
        if local.weekday() >= 5 or not self.entry_start <= local.time().replace(tzinfo=None) <= self.entry_end:
            return self._rejected(symbol, lot_size, now, "Option paper entry is outside market hours")
        if not decision_id:
            return self._rejected(symbol, lot_size, now, "Missing option paper decision identifier")
        if structure.get("risk_eligible") is not True or int(structure.get("lots", 0)) != 1:
            return self._rejected(symbol, lot_size, now, "Structure is not eligible for one-lot paper entry")
        legs = structure.get("legs")
        if not isinstance(legs, list) or len(legs) not in {2, 4}:
            return self._rejected(symbol, lot_size, now, "A complete hedged structure is required")
        sides = [str(leg.get("side")) for leg in legs if isinstance(leg, dict)]
        if "SELL" not in sides or "BUY" not in sides or lot_size <= 0:
            return self._rejected(symbol, lot_size, now, "Naked option paper positions are prohibited")

        state = self._load_state()
        if state["open_positions"]:
            return self._rejected(symbol, lot_size, now, "Maximum one open option structure reached")
        if any(item.get("decision_id") == decision_id for item in state["orders"]):
            return self._rejected(symbol, lot_size, now, "Duplicate option paper decision blocked")
        today_entries = [
            item
            for item in state["orders"]
            if item.get("kind") == "ENTRY" and _local_date(item.get("timestamp")) == local.date()
        ]
        if len(today_entries) >= self.max_trades_per_day:
            return self._rejected(symbol, lot_size, now, "Option paper daily trade limit reached")
        realized_today = sum(
            float(item.get("net_pnl", 0))
            for item in state["closed_positions"]
            if _local_date(item.get("closed_at")) == local.date()
        )
        if realized_today <= -self.daily_loss_limit:
            return self._rejected(symbol, lot_size, now, "Option paper daily loss limit reached")
        consecutive_losses = 0
        for item in reversed(state["closed_positions"]):
            if float(item.get("net_pnl", 0)) < 0:
                consecutive_losses += 1
            else:
                break
        if consecutive_losses >= self.stop_after_consecutive_losses:
            return self._rejected(symbol, lot_size, now, "Option paper consecutive loss limit reached")

        slip = self.slippage_bps / 10000
        stored_legs = []
        entry_fees = 0.0
        credit_points = 0.0
        for leg in legs:
            side = str(leg.get("side"))
            raw_price = float(leg.get("price", 0))
            if raw_price <= 0 or not leg.get("exchange") or not leg.get("token"):
                return self._rejected(symbol, lot_size, now, "Option leg cannot be paper priced")
            fill = raw_price * (1 - slip if side == "SELL" else 1 + slip)
            entry_fees += fill * lot_size * self.fee_bps / 10000
            credit_points += fill if side == "SELL" else -fill
            stored_legs.append({**leg, "entry_price": round(fill, 4)})
        entry_fees = round(entry_fees, 2)
        widths = []
        option_types = {str(leg.get("derivative_type")) for leg in stored_legs}
        for option_type in option_types:
            shorts = [
                float(leg["strike"])
                for leg in stored_legs
                if leg["side"] == "SELL" and leg.get("derivative_type") == option_type
            ]
            longs = [
                float(leg["strike"])
                for leg in stored_legs
                if leg["side"] == "BUY" and leg.get("derivative_type") == option_type
            ]
            widths.extend(abs(short - long) for short in shorts for long in longs)
        protective_width = max((width for width in widths if width > 0), default=0.0)
        max_profit = round(credit_points * lot_size - entry_fees, 2)
        max_loss = round((protective_width - credit_points) * lot_size + entry_fees, 2)
        if max_loss <= 0 or max_loss > self.risk_limit:
            return self._rejected(symbol, lot_size, now, "Maximum loss exceeds option paper risk limit")

        order_id = f"OPTION-PAPER-{self.id_factory()}"
        timestamp = now.isoformat()
        position = {
            "position_id": order_id,
            "symbol": f"{symbol} {structure.get('name', 'DEFINED_RISK')}",
            "underlying": symbol,
            "structure": structure.get("name"),
            "side": "CREDIT",
            "quantity": lot_size,
            "lot_size": lot_size,
            "entry_price": round(credit_points, 4),
            "entry_fees": entry_fees,
            "stop_loss": max_loss,
            "target": max_profit,
            "max_loss": max_loss,
            "max_profit": max_profit,
            "expiry": structure.get("expiry"),
            "opened_at": timestamp,
            "decision_id": decision_id,
            "legs": stored_legs,
            "unrealized_pnl": -entry_fees,
        }
        order = {
            "order_id": order_id,
            "kind": "ENTRY",
            "status": "FILLED",
            "symbol": position["symbol"],
            "side": "CREDIT",
            "quantity": lot_size,
            "fill_price": position["entry_price"],
            "fees": entry_fees,
            "timestamp": timestamp,
            "decision_id": decision_id,
        }
        state["open_positions"].append(position)
        state["orders"].append(order)
        self._save_state(state)
        return OrderResult(
            True,
            order_id,
            "Defined-risk option paper structure filled",
            symbol=position["symbol"],
            side="CREDIT",
            quantity=lot_size,
            fill_price=position["entry_price"],
            fees=entry_fees,
            timestamp=timestamp,
        )

    def open_contracts(self) -> list[dict]:
        state = self._load_state()
        if not state["open_positions"]:
            return []
        return [
            {
                key: leg.get(key)
                for key in ("exchange", "token", "symbol", "expiry", "strike", "lot_size", "derivative_type")
            }
            for leg in state["open_positions"][0]["legs"]
        ]

    def reconcile(self, quotes: list[dict]) -> OrderResult | None:
        state = self._load_state()
        if not state["open_positions"]:
            return None
        position = state["open_positions"][0]
        quote_map = {(str(item.get("exchange")), str(item.get("token"))): item for item in quotes}
        exit_fees = 0.0
        gross = 0.0
        slip = self.slippage_bps / 10000
        marked_legs = []
        for leg in position["legs"]:
            quote = quote_map.get((str(leg.get("exchange")), str(leg.get("token"))))
            if quote is None:
                return None
            original_side = str(leg["side"])
            raw_exit = float(quote.get("best_ask" if original_side == "SELL" else "best_bid", 0))
            if raw_exit <= 0:
                return None
            exit_price = raw_exit * (1 + slip if original_side == "SELL" else 1 - slip)
            lot_size = int(position["lot_size"])
            exit_fees += exit_price * lot_size * self.fee_bps / 10000
            entry_price = float(leg["entry_price"])
            gross += (
                (entry_price - exit_price) * lot_size
                if original_side == "SELL"
                else (exit_price - entry_price) * lot_size
            )
            marked_legs.append({**leg, "exit_price": round(exit_price, 4)})
        exit_fees = round(exit_fees, 2)
        net_pnl = round(gross - float(position["entry_fees"]) - exit_fees, 2)
        net_pnl = min(float(position["max_profit"]), max(-float(position["max_loss"]), net_pnl))
        now = self._now()
        local = now.astimezone(ZoneInfo("Asia/Kolkata"))
        opened_date = _local_date(position.get("opened_at"))
        should_close = opened_date is not None and (
            local.date() > opened_date or local.time().replace(tzinfo=None) >= self.force_exit
        )
        position["unrealized_pnl"] = round(net_pnl, 2)
        position["last_marked_at"] = now.isoformat()
        if not should_close:
            self._save_state(state)
            return None

        order_id = f"OPTION-PAPER-{self.id_factory()}"
        reason = "overnight_safety_exit" if local.date() > opened_date else "force_exit"
        order = {
            "order_id": order_id,
            "kind": "EXIT",
            "status": "FILLED",
            "symbol": position["symbol"],
            "side": "CLOSE",
            "quantity": int(position["lot_size"]),
            "fill_price": round(sum(float(item["exit_price"]) for item in marked_legs), 4),
            "fees": exit_fees,
            "timestamp": now.isoformat(),
            "position_id": position["position_id"],
            "outcome": reason,
        }
        closed = {
            **position,
            "legs": marked_legs,
            "exit_order_id": order_id,
            "exit_fees": exit_fees,
            "closed_at": now.isoformat(),
            "outcome": reason,
            "net_pnl": round(net_pnl, 2),
        }
        state["open_positions"] = []
        state["closed_positions"].append(closed)
        state["orders"].append(order)
        state["realized_pnl"] = round(float(state["realized_pnl"]) + net_pnl, 2)
        self._save_state(state)
        return OrderResult(
            True,
            order_id,
            f"Option paper structure closed: {reason}",
            symbol=position["symbol"],
            side="CLOSE",
            quantity=int(position["lot_size"]),
            fill_price=order["fill_price"],
            fees=exit_fees,
            timestamp=now.isoformat(),
        )

    def snapshot(self) -> dict[str, object]:
        state = self._load_state()
        realized = round(float(state.get("realized_pnl", 0)), 2)
        unrealized = round(
            sum(float(item.get("unrealized_pnl", 0)) for item in state["open_positions"]), 2
        )
        return {
            "starting_balance": self.initial_balance,
            "paper_balance": round(self.initial_balance + realized, 2),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "open_position_count": len(state["open_positions"]),
            "closed_trade_count": len(state["closed_positions"]),
            "open_positions": list(state["open_positions"]),
            "recent_orders": list(reversed(state["orders"][-20:])),
        }

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("Option paper portfolio state could not be read") from None
        if not isinstance(value, dict) or value.get("version") != 1:
            raise RuntimeError("Option paper portfolio state was invalid")
        for key in ("open_positions", "closed_positions", "orders"):
            if not isinstance(value.get(key), list):
                raise RuntimeError("Option paper portfolio state was invalid")
        return value

    def _save_state(self, state: dict) -> None:
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

    def _now(self) -> datetime:
        value = self.clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _rejected(symbol: str, quantity: int, now: datetime, reason: str) -> OrderResult:
        return OrderResult(
            False,
            "",
            reason,
            status="REJECTED",
            symbol=symbol,
            side="CREDIT",
            quantity=max(0, quantity),
            timestamp=now.isoformat(),
        )


def _clock_time(value: str) -> wall_time:
    return datetime.strptime(value, "%H:%M").time()


def _local_date(value: object):
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Asia/Kolkata")).date()
