from __future__ import annotations

from dataclasses import dataclass

from .news import NewsSignal
from .strategy import TradeSignal


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quantity: int
    reason: str


class RiskManager:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.trades_today = 0
        self.realized_pnl = 0.0
        self.consecutive_losses = 0

    def evaluate(self, signal: TradeSignal, requested_quantity: int, news: NewsSignal) -> RiskDecision:
        capital = float(self.config.get("capital", 100000))
        max_daily_loss = capital * float(self.config.get("max_daily_loss_pct", 2.0)) / 100
        max_trades = int(self.config.get("max_trades_per_day", 5))
        stop_after_losses = int(self.config.get("stop_after_consecutive_losses", 3))

        if signal.decision == "AVOID":
            return RiskDecision(False, 0, "Strategy says avoid")
        if self.trades_today >= max_trades:
            return RiskDecision(False, 0, "Max trades per day reached")
        if abs(min(self.realized_pnl, 0)) >= max_daily_loss:
            return RiskDecision(False, 0, "Max daily loss reached")
        if self.consecutive_losses >= stop_after_losses:
            return RiskDecision(False, 0, "Consecutive loss limit reached")
        if self.config.get("avoid_high_impact_news", True) and news.risk_level == "High":
            return RiskDecision(False, 0, "High-impact news risk")

        risk_per_trade = capital * float(self.config.get("risk_per_trade_pct", 0.5)) / 100
        risk_per_share = abs(signal.entry_price - signal.stop_loss)
        if risk_per_share <= 0:
            return RiskDecision(False, 0, "Invalid stop-loss distance")

        risk_quantity = int(risk_per_trade // risk_per_share)
        max_position_value = capital * float(self.config.get("max_position_value_pct", 10)) / 100
        value_quantity = int(max_position_value // signal.entry_price)
        quantity = max(0, min(requested_quantity, risk_quantity, value_quantity))

        if quantity <= 0:
            return RiskDecision(False, 0, "Quantity reduced to zero by risk limits")

        return RiskDecision(True, quantity, "Risk checks passed")
