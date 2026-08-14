from __future__ import annotations

from datetime import datetime, time as wall_time
from math import log10
from typing import Callable
from zoneinfo import ZoneInfo

from .market_data import MarketDataError


ALLOWED_STRUCTURES = {"credit_spread", "iron_condor", "iron_fly"}


class OptionSellingError(RuntimeError):
    """Raised when a read-only OI structure cannot be evaluated safely."""


class DisabledOptionSellingEngine:
    enabled = False

    def propose(self, *args, **kwargs) -> dict[str, object]:
        return {"enabled": False, "status": "disabled", "structures": []}


class DefinedRiskOptionSellingEngine:
    enabled = True

    def __init__(
        self,
        config: dict,
        discovery,
        market_data,
        risk_config: dict,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.discovery = discovery
        self.market_data = market_data
        self.capital = float(risk_config.get("capital", 300000))
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")))

    def propose(self, underlying: str, spot_price: float) -> dict[str, object]:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        local_time = now.astimezone(ZoneInfo("Asia/Kolkata")).time().replace(tzinfo=None)
        entry_start = _time_value(self.config.get("entry_start", "09:30"))
        entry_end = _time_value(self.config.get("entry_end", "14:30"))
        if local_time < entry_start or local_time > entry_end:
            return {
                "enabled": True,
                "status": "outside_entry_window",
                "mode": "shadow_defined_risk_only",
                "underlying": underlying,
                "spot_price": round(float(spot_price), 2),
                "selected": None,
                "structures": [],
                "paper_execution_allowed": False,
                "reason": "New option-selling structures are blocked outside the configured entry window",
            }
        try:
            contracts = self.discovery.option_chain_for(
                underlying,
                spot_price,
                strikes_each_side=int(self.config.get("chain_strikes_each_side", 6)),
                minimum_expiry_days=int(self.config.get("minimum_expiry_days", 2)),
                maximum_expiry_days=int(self.config.get("maximum_expiry_days", 10)),
            )
            if not contracts:
                raise OptionSellingError("No supported option chain contracts were discovered")
            quotes = self.market_data.get_full_quotes(contracts)
        except (MarketDataError, OptionSellingError):
            raise OptionSellingError("Option-chain OI data is temporarily unavailable") from None

        valid, rejected = self._liquid_quotes(quotes)
        by_key = {
            (float(item["strike"]), str(item["derivative_type"])): item
            for item in valid
            if item.get("strike") is not None
        }
        strikes = sorted({key[0] for key in by_key})
        if len(strikes) < 5:
            raise OptionSellingError("Insufficient liquid strikes for a defined-risk structure")

        calls = [item for item in valid if item.get("derivative_type") == "call"]
        puts = [item for item in valid if item.get("derivative_type") == "put"]
        call_oi = sum(float(item["open_interest"]) for item in calls)
        put_oi = sum(float(item["open_interest"]) for item in puts)
        pcr = put_oi / call_oi if call_oi else 0.0
        width_steps = int(self.config.get("wing_width_strikes", 2))
        allowed = set(self.config.get("structures", sorted(ALLOWED_STRUCTURES)))
        structures: list[dict[str, object]] = []

        short_put = _highest_oi(
            item
            for item in puts
            if float(item["strike"]) < spot_price
            and _wing(strikes, float(item["strike"]), -width_steps) is not None
        )
        short_call = _highest_oi(
            item
            for item in calls
            if float(item["strike"]) > spot_price
            and _wing(strikes, float(item["strike"]), width_steps) is not None
        )

        if "iron_condor" in allowed and short_put and short_call:
            long_put = by_key.get((_wing(strikes, float(short_put["strike"]), -width_steps), "put"))
            long_call = by_key.get((_wing(strikes, float(short_call["strike"]), width_steps), "call"))
            if long_put and long_call:
                structures.append(
                    self._structure(
                        "iron_condor",
                        underlying,
                        spot_price,
                        [("BUY", long_put), ("SELL", short_put), ("SELL", short_call), ("BUY", long_call)],
                    )
                )

        atm = min(strikes, key=lambda strike: abs(strike - spot_price))
        if "iron_fly" in allowed:
            atm_put = by_key.get((atm, "put"))
            atm_call = by_key.get((atm, "call"))
            put_wing = _wing(strikes, atm, -width_steps)
            call_wing = _wing(strikes, atm, width_steps)
            long_put = by_key.get((put_wing, "put")) if put_wing is not None else None
            long_call = by_key.get((call_wing, "call")) if call_wing is not None else None
            if atm_put and atm_call and long_put and long_call:
                structures.append(
                    self._structure(
                        "iron_fly",
                        underlying,
                        spot_price,
                        [("BUY", long_put), ("SELL", atm_put), ("SELL", atm_call), ("BUY", long_call)],
                    )
                )

        if "credit_spread" in allowed and pcr >= float(self.config.get("bullish_pcr", 1.1)) and short_put:
            wing = _wing(strikes, float(short_put["strike"]), -width_steps)
            long_put = by_key.get((wing, "put")) if wing is not None else None
            if long_put:
                structures.append(
                    self._structure(
                        "bull_put_credit_spread",
                        underlying,
                        spot_price,
                        [("BUY", long_put), ("SELL", short_put)],
                    )
                )
        elif "credit_spread" in allowed and pcr <= float(self.config.get("bearish_pcr", 0.9)) and short_call:
            wing = _wing(strikes, float(short_call["strike"]), width_steps)
            long_call = by_key.get((wing, "call")) if wing is not None else None
            if long_call:
                structures.append(
                    self._structure(
                        "bear_call_credit_spread",
                        underlying,
                        spot_price,
                        [("SELL", short_call), ("BUY", long_call)],
                    )
                )

        ranked = sorted(
            structures,
            key=lambda item: (bool(item["risk_eligible"]), float(item["score"])),
            reverse=True,
        )
        selected = ranked[0] if ranked else None
        return {
            "enabled": True,
            "status": "candidate" if selected else "no_structure",
            "mode": "shadow_defined_risk_only",
            "underlying": underlying,
            "spot_price": round(float(spot_price), 2),
            "put_call_oi_ratio": round(pcr, 4),
            "liquid_quotes": len(valid),
            "rejected_quotes": rejected,
            "selected": selected,
            "structures": ranked,
            "paper_execution_allowed": False,
            "reason": "Release 5.3 shadow mode blocks all option-selling orders",
            "risk_controls": {
                "entry_start": self.config.get("entry_start", "09:30"),
                "entry_end": self.config.get("entry_end", "14:30"),
                "force_exit": self.config.get("force_exit", "15:10"),
                "max_trades_per_day": self.config.get("max_trades_per_day", 2),
                "max_open_structures": self.config.get("max_open_structures", 1),
                "max_daily_loss_pct": self.config.get("max_daily_loss_pct", 1.0),
            },
        }

    def _liquid_quotes(self, quotes: list[dict]) -> tuple[list[dict], int]:
        minimum_oi = float(self.config.get("min_open_interest", 1000))
        minimum_volume = float(self.config.get("min_volume", 100))
        maximum_spread = float(self.config.get("max_bid_ask_spread_pct", 10))
        valid = []
        for item in quotes:
            bid = float(item.get("best_bid", 0))
            ask = float(item.get("best_ask", 0))
            midpoint = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
            spread_pct = ((ask - bid) / midpoint) * 100 if midpoint > 0 and ask >= bid else 999
            if (
                bid <= 0
                or ask <= 0
                or float(item.get("open_interest", 0)) < minimum_oi
                or float(item.get("volume", 0)) < minimum_volume
                or spread_pct > maximum_spread
            ):
                continue
            valid.append({**item, "spread_pct": round(spread_pct, 2)})
        return valid, len(quotes) - len(valid)

    def _structure(
        self,
        name: str,
        underlying: str,
        spot_price: float,
        legs: list[tuple[str, dict]],
    ) -> dict[str, object]:
        lot_sizes = {int(item.get("lot_size", 0)) for _, item in legs}
        expiries = {str(item.get("expiry", "")) for _, item in legs}
        blockers: list[str] = []
        if len(lot_sizes) != 1 or min(lot_sizes or {0}) <= 0:
            blockers.append("Leg lot sizes are inconsistent")
        if len(expiries) != 1 or not next(iter(expiries), ""):
            blockers.append("Leg expiries are inconsistent")
        lot_size = next(iter(lot_sizes), 0)
        credit_points = sum(
            float(item["best_bid"]) if side == "SELL" else -float(item["best_ask"])
            for side, item in legs
        )
        widths = []
        for option_type in {str(item.get("derivative_type")) for _, item in legs}:
            shorts = [
                float(item["strike"])
                for side, item in legs
                if side == "SELL" and item.get("derivative_type") == option_type
            ]
            longs = [
                float(item["strike"])
                for side, item in legs
                if side == "BUY" and item.get("derivative_type") == option_type
            ]
            widths.extend(abs(short - long) for short in shorts for long in longs)
        protective_width = max((value for value in widths if value > 0), default=0.0)
        max_loss_points = protective_width - credit_points
        if credit_points <= 0:
            blockers.append("Conservative bid/ask pricing produces no credit")
        if protective_width <= 0 or max_loss_points <= 0:
            blockers.append("Maximum loss could not be bounded")

        max_profit = max(0.0, credit_points * lot_size)
        max_loss = max(0.0, max_loss_points * lot_size)
        credit_to_risk = max_profit / max_loss if max_loss else 0.0
        risk_budget = self.capital * float(self.config.get("max_risk_per_trade_pct", 0.5)) / 100
        if max_loss > risk_budget:
            blockers.append("One-lot maximum loss exceeds per-trade risk budget")
        if credit_to_risk < float(self.config.get("min_credit_to_risk", 0.2)):
            blockers.append("Credit-to-risk ratio is below minimum")

        short_oi = sum(float(item.get("open_interest", 0)) for side, item in legs if side == "SELL")
        score = credit_to_risk * 100 + log10(max(short_oi, 1))
        return {
            "name": name,
            "underlying": underlying,
            "expiry": next(iter(expiries), None),
            "spot_price": round(spot_price, 2),
            "lot_size": lot_size,
            "lots": 1 if not blockers else 0,
            "net_credit_points": round(credit_points, 2),
            "max_profit": round(max_profit, 2),
            "max_loss": round(max_loss, 2),
            "risk_budget": round(risk_budget, 2),
            "credit_to_risk": round(credit_to_risk, 4),
            "short_leg_open_interest": round(short_oi, 2),
            "risk_eligible": not blockers,
            "blockers": blockers,
            "score": round(score, 4),
            "legs": [
                {
                    "side": side,
                    "symbol": item.get("symbol"),
                    "option_type": item.get("derivative_type"),
                    "strike": item.get("strike"),
                    "price": item.get("best_bid") if side == "SELL" else item.get("best_ask"),
                    "open_interest": item.get("open_interest"),
                    "volume": item.get("volume"),
                }
                for side, item in legs
            ],
        }


def build_option_selling_engine(config: object, discovery, market_data, risk_config: dict):
    if not isinstance(config, dict) or not config.get("enabled", False):
        return DisabledOptionSellingEngine()
    return DefinedRiskOptionSellingEngine(config, discovery, market_data, risk_config)


def validate_option_selling_config(
    config: object,
    derivatives_config: object | None = None,
    market_data_config: object | None = None,
) -> None:
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("option_selling must be a mapping")
    if not isinstance(config.get("enabled", False), bool):
        raise ValueError("option_selling.enabled must be a boolean")
    if not config.get("enabled", False):
        return
    if (
        not isinstance(derivatives_config, dict)
        or derivatives_config.get("enabled") is not True
        or "options" not in derivatives_config.get("instruments", [])
    ):
        raise ValueError("option_selling requires enabled derivatives options discovery")
    if not isinstance(market_data_config, dict) or market_data_config.get("provider") != "angel_one":
        raise ValueError("option_selling requires market_data.provider angel_one")
    for key in ("paper_only", "shadow_mode", "defined_risk_only"):
        if config.get(key) is not True:
            raise ValueError(f"option_selling.{key} must remain true")
    if config.get("naked_short_options", False) is not False:
        raise ValueError("option_selling.naked_short_options must remain false")
    structures = config.get("structures", [])
    if not isinstance(structures, list) or not structures or not set(structures) <= ALLOWED_STRUCTURES:
        raise ValueError("option_selling.structures contains an unsupported structure")
    _bounded_int(config, "chain_strikes_each_side", 4, 12, 6)
    _bounded_int(config, "wing_width_strikes", 1, 4, 2)
    _bounded_int(config, "minimum_expiry_days", 2, 5, 2)
    _bounded_int(config, "maximum_expiry_days", 5, 14, 10)
    if int(config.get("minimum_expiry_days", 2)) >= int(config.get("maximum_expiry_days", 10)):
        raise ValueError("option_selling expiry-day bounds are invalid")
    _bounded_int(config, "min_open_interest", 100, 1000000000, 1000)
    _bounded_int(config, "min_volume", 1, 1000000000, 100)
    _bounded_number(config, "max_bid_ask_spread_pct", 1, 20, 10)
    _bounded_number(config, "min_credit_to_risk", 0.1, 0.5, 0.2)
    _bounded_number(config, "max_risk_per_trade_pct", 0.1, 0.5, 0.5)
    _bounded_number(config, "max_daily_loss_pct", 0.5, 1.0, 1.0)
    _bounded_int(config, "max_trades_per_day", 1, 3, 2)
    _bounded_int(config, "max_open_structures", 1, 1, 1)
    bullish = float(config.get("bullish_pcr", 1.1))
    bearish = float(config.get("bearish_pcr", 0.9))
    if not 1.0 <= bullish <= 2.0 or not 0.5 <= bearish <= 1.0 or bearish >= bullish:
        raise ValueError("option_selling PCR thresholds are invalid")
    entry_start = _time_value(config.get("entry_start", "09:30"))
    entry_end = _time_value(config.get("entry_end", "14:30"))
    force_exit = _time_value(config.get("force_exit", "15:10"))
    if not wall_time(9, 20) <= entry_start < entry_end <= wall_time(14, 45):
        raise ValueError("option_selling entry window is invalid")
    if not entry_end < force_exit <= wall_time(15, 20):
        raise ValueError("option_selling force_exit is invalid")


def _highest_oi(items) -> dict | None:
    values = list(items)
    return max(values, key=lambda item: float(item.get("open_interest", 0)), default=None)


def _wing(strikes: list[float], strike: float, offset: int) -> float | None:
    try:
        target = strikes.index(strike) + offset
    except ValueError:
        return None
    return strikes[target] if 0 <= target < len(strikes) else None


def _time_value(value: object) -> wall_time:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        raise ValueError("option_selling times must use HH:MM format") from None


def _bounded_int(config: dict, key: str, minimum: int, maximum: int, default: int) -> None:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"option_selling.{key} must be between {minimum} and {maximum}")


def _bounded_number(config: dict, key: str, minimum: float, maximum: float, default: float) -> None:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"option_selling.{key} must be between {minimum} and {maximum}")
