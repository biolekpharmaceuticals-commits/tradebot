from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .strategy import TradeSignal


@dataclass(frozen=True)
class ScanCandidate:
    symbol_config: dict
    candles: pd.DataFrame
    signal: TradeSignal
    average_volume: int
    average_turnover: float
    eligible: bool
    reason: str
    contract_value: float | None = None
    lot_risk: float | None = None

    def public_summary(self, rank: int) -> dict[str, object]:
        return {
            "rank": rank,
            "symbol": self.symbol_config.get("symbol"),
            "instrument_type": self.symbol_config.get("instrument_type", "equity"),
            "derivative_type": self.symbol_config.get("derivative_type"),
            "underlying": self.symbol_config.get("underlying"),
            "expiry": self.symbol_config.get("expiry"),
            "strike": self.symbol_config.get("strike"),
            "lot_size": self.symbol_config.get("lot_size"),
            "decision": self.signal.decision,
            "confidence": self.signal.confidence,
            "average_volume": self.average_volume,
            "average_turnover": round(self.average_turnover, 2),
            "contract_value": round(self.contract_value, 2) if self.contract_value is not None else None,
            "lot_risk": round(self.lot_risk, 2) if self.lot_risk is not None else None,
            "eligible": self.eligible,
            "reason": self.reason,
        }


def build_candidate(
    symbol_config: dict,
    candles: pd.DataFrame,
    signal: TradeSignal,
    *,
    min_average_volume: int,
    min_derivative_average_volume: int | None = None,
    risk_config: dict | None = None,
) -> ScanCandidate:
    if candles is None or candles.empty or "volume" not in candles or "close" not in candles:
        average_volume = 0
        average_turnover = 0.0
    else:
        recent = candles.tail(min(20, len(candles)))
        average_volume = int(recent["volume"].mean())
        average_turnover = float((recent["close"] * recent["volume"]).mean())

    instrument_type = str(symbol_config.get("instrument_type", "equity")).lower()
    is_index = instrument_type == "index"
    is_derivative = instrument_type == "derivative"
    reasons: list[str] = []
    if signal.decision not in {"BUY", "SELL"}:
        reasons.append("No directional signal")
    required_decision = symbol_config.get("required_decision")
    if required_decision and signal.decision != required_decision:
        reasons.append(f"Contract requires {required_decision} confirmation")
    required_volume = (
        min_derivative_average_volume
        if is_derivative and min_derivative_average_volume is not None
        else min_average_volume
    )
    if not is_index and average_volume < required_volume:
        label = "F&O average volume" if is_derivative else "Average volume"
        reasons.append(f"{label} below {required_volume}")

    contract_value = None
    lot_risk = None
    if is_derivative:
        lot_size = _positive_int(symbol_config.get("lot_size") or symbol_config.get("quantity"))
        contract_value = max(0.0, float(signal.entry_price)) * lot_size
        lot_risk = abs(float(signal.entry_price) - float(signal.stop_loss)) * lot_size
        if risk_config is not None and lot_size > 0:
            capital = float(risk_config.get("capital", 100000))
            max_position_value = capital * float(risk_config.get("max_position_value_pct", 10)) / 100
            risk_budget = capital * float(risk_config.get("risk_per_trade_pct", 0.5)) / 100
            if contract_value > max_position_value:
                reasons.append("Full F&O lot value exceeds position limit")
            if lot_risk > risk_budget:
                reasons.append("Full F&O lot risk exceeds per-trade limit")

    eligible = not reasons
    return ScanCandidate(
        symbol_config=symbol_config,
        candles=candles,
        signal=signal,
        average_volume=average_volume,
        average_turnover=average_turnover,
        eligible=eligible,
        reason=("Eligible index; volume filter not applicable" if eligible and is_index else "Eligible")
        if eligible
        else "; ".join(reasons),
        contract_value=contract_value,
        lot_risk=lot_risk,
    )


def rank_candidates(candidates: list[ScanCandidate]) -> list[ScanCandidate]:
    return sorted(
        candidates,
        key=lambda item: (
            item.eligible,
            _long_option_priority(item),
            item.signal.confidence,
            item.average_turnover,
            str(item.symbol_config.get("symbol", "")),
        ),
        reverse=True,
    )


def _long_option_priority(candidate: ScanCandidate) -> int:
    derivative_type = str(candidate.symbol_config.get("derivative_type", "")).lower()
    return 1 if derivative_type in {"call", "put"} else 0


def _positive_int(value: object) -> int:
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return 0
