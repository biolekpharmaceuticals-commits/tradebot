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
            "eligible": self.eligible,
            "reason": self.reason,
        }


def build_candidate(
    symbol_config: dict,
    candles: pd.DataFrame,
    signal: TradeSignal,
    *,
    min_average_volume: int,
) -> ScanCandidate:
    if candles is None or candles.empty or "volume" not in candles or "close" not in candles:
        average_volume = 0
        average_turnover = 0.0
    else:
        recent = candles.tail(min(20, len(candles)))
        average_volume = int(recent["volume"].mean())
        average_turnover = float((recent["close"] * recent["volume"]).mean())

    is_index = str(symbol_config.get("instrument_type", "equity")).lower() == "index"
    reasons: list[str] = []
    if signal.decision not in {"BUY", "SELL"}:
        reasons.append("No directional signal")
    required_decision = symbol_config.get("required_decision")
    if required_decision and signal.decision != required_decision:
        reasons.append(f"Contract requires {required_decision} confirmation")
    if not is_index and average_volume < min_average_volume:
        reasons.append(f"Average volume below {min_average_volume}")

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
    )


def rank_candidates(candidates: list[ScanCandidate]) -> list[ScanCandidate]:
    return sorted(
        candidates,
        key=lambda item: (
            item.eligible,
            item.signal.confidence,
            item.average_turnover,
            str(item.symbol_config.get("symbol", "")),
        ),
        reverse=True,
    )
