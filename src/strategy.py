from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .bulk_deals import BulkDealSignal
from .indicators import add_indicators
from .news import NewsSignal


@dataclass(frozen=True)
class TradeSignal:
    decision: str
    confidence: int
    market_bias: str
    entry_price: float
    stop_loss: float
    target: float
    reward_risk: float
    reason: str
    strategy: dict[str, object] = field(default_factory=dict)


class TrendStrategy:
    NAME = "EMA + VWAP + RSI + MACD + News + Bulk Deals"
    VERSION = "3.0"

    def __init__(self, config: dict) -> None:
        self.config = config

    def evaluate(
        self,
        candles: pd.DataFrame,
        news: NewsSignal,
        bulk_deals: BulkDealSignal | None = None,
    ) -> TradeSignal:
        df = add_indicators(
            candles,
            ema_fast=int(self.config.get("ema_fast", 9)),
            ema_slow=int(self.config.get("ema_slow", 21)),
            ema_trend=int(self.config.get("ema_trend", 50)),
            rsi_period=int(self.config.get("rsi_period", 14)),
            atr_period=int(self.config.get("atr_period", 14)),
        ).dropna()

        if df.empty:
            return TradeSignal(
                "AVOID",
                0,
                "Neutral",
                0,
                0,
                0,
                0,
                "Not enough candle data",
                self._explanation([], {}, 0, 0),
            )

        latest = df.iloc[-1]
        previous = df.iloc[-2] if len(df) > 1 else latest
        close = float(latest["close"])
        atr = float(latest["atr"])

        bullish_points = 0
        bearish_points = 0
        reasons: list[str] = []
        factors: list[dict[str, object]] = []

        if latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"]:
            bullish_points += 25
            reasons.append("EMA stack is bullish")
            _add_factor(factors, "EMA stack", "Bullish", 25, "Fast EMA > slow EMA > trend EMA")
        elif latest["ema_fast"] < latest["ema_slow"] < latest["ema_trend"]:
            bearish_points += 25
            reasons.append("EMA stack is bearish")
            _add_factor(factors, "EMA stack", "Bearish", -25, "Fast EMA < slow EMA < trend EMA")
        else:
            _add_factor(factors, "EMA stack", "Neutral", 0, "EMA values are not fully aligned")

        if close > latest["vwap"]:
            bullish_points += 15
            reasons.append("Price is above VWAP")
            _add_factor(factors, "VWAP", "Bullish", 15, "Close is above VWAP")
        else:
            bearish_points += 15
            reasons.append("Price is below VWAP")
            _add_factor(factors, "VWAP", "Bearish", -15, "Close is below VWAP")

        if 52 <= latest["rsi"] <= 68:
            bullish_points += 15
            reasons.append("RSI supports upward momentum")
            _add_factor(factors, "RSI", "Bullish", 15, "RSI is between 52 and 68")
        elif 32 <= latest["rsi"] <= 48:
            bearish_points += 15
            reasons.append("RSI supports downward momentum")
            _add_factor(factors, "RSI", "Bearish", -15, "RSI is between 32 and 48")
        elif latest["rsi"] > 75 or latest["rsi"] < 25:
            reasons.append("RSI is stretched")
            _add_factor(factors, "RSI", "Stretched", 0, "RSI is outside the 25 to 75 range")
        else:
            _add_factor(factors, "RSI", "Neutral", 0, "RSI is outside configured momentum bands")

        if latest["macd"] > latest["macd_signal"] and previous["macd"] <= previous["macd_signal"]:
            bullish_points += 15
            reasons.append("MACD bullish crossover")
            _add_factor(factors, "MACD", "Bullish", 15, "MACD crossed above its signal line")
        elif latest["macd"] < latest["macd_signal"] and previous["macd"] >= previous["macd_signal"]:
            bearish_points += 15
            reasons.append("MACD bearish crossover")
            _add_factor(factors, "MACD", "Bearish", -15, "MACD crossed below its signal line")
        else:
            _add_factor(factors, "MACD", "Neutral", 0, "No new MACD crossover")

        news_points = 0
        if news.score > 10:
            news_points = min(20, news.score // 2)
            bullish_points += news_points
            reasons.append("News sentiment is supportive")
        elif news.score < -10:
            news_points = -min(20, abs(news.score) // 2)
            bearish_points += abs(news_points)
            reasons.append("News sentiment is negative")
        _add_factor(
            factors,
            "News sentiment",
            _direction(news_points),
            news_points,
            f"Transparent keyword score: {news.score}",
        )

        bulk_points = max(-10, min(10, int(bulk_deals.score))) if bulk_deals else 0
        if bulk_points > 0:
            bullish_points += bulk_points
            reasons.append("Published bulk deals show accumulation")
        elif bulk_points < 0:
            bearish_points += abs(bulk_points)
            reasons.append("Published bulk deals show distribution")
        _add_factor(
            factors,
            "NSE bulk deals",
            bulk_deals.direction if bulk_deals else "Neutral",
            bulk_points,
            bulk_deals.explanation if bulk_deals else "Bulk-deal data not supplied",
        )

        if bullish_points > bearish_points:
            stop_loss = close - (1.5 * atr)
            target = close + (2.5 * atr)
            reward_risk = (target - close) / max(close - stop_loss, 0.01)
            confidence = min(100, bullish_points - max(0, bearish_points // 2))
            decision = "BUY"
            bias = "Bullish"
        elif bearish_points > bullish_points:
            stop_loss = close + (1.5 * atr)
            target = close - (2.5 * atr)
            reward_risk = (close - target) / max(stop_loss - close, 0.01)
            confidence = min(100, bearish_points - max(0, bullish_points // 2))
            decision = "SELL"
            bias = "Bearish"
        else:
            stop_loss = close
            target = close
            reward_risk = 0
            confidence = 0
            decision = "AVOID"
            bias = "Neutral"

        min_reward_risk = float(self.config.get("min_reward_risk", 1.5))
        if reward_risk < min_reward_risk:
            decision = "AVOID"
            reasons.append(f"Reward/risk below {min_reward_risk}")

        indicators = {
            "close": _round(latest["close"]),
            "ema_fast": _round(latest["ema_fast"]),
            "ema_slow": _round(latest["ema_slow"]),
            "ema_trend": _round(latest["ema_trend"]),
            "vwap": _round(latest["vwap"]),
            "rsi": _round(latest["rsi"]),
            "macd": _round(latest["macd"]),
            "macd_signal": _round(latest["macd_signal"]),
            "atr": _round(latest["atr"]),
        }

        return TradeSignal(
            decision=decision,
            confidence=int(confidence),
            market_bias=bias,
            entry_price=round(close, 2),
            stop_loss=round(stop_loss, 2),
            target=round(target, 2),
            reward_risk=round(reward_risk, 2),
            reason="; ".join(reasons) or "No strong edge",
            strategy=self._explanation(factors, indicators, bullish_points, bearish_points),
        )

    def _explanation(
        self,
        factors: list[dict[str, object]],
        indicators: dict[str, float],
        bullish_points: int,
        bearish_points: int,
    ) -> dict[str, object]:
        return {
            "name": self.NAME,
            "version": self.VERSION,
            "type": "Transparent rule-based strategy",
            "parameters": {
                "ema_fast": int(self.config.get("ema_fast", 9)),
                "ema_slow": int(self.config.get("ema_slow", 21)),
                "ema_trend": int(self.config.get("ema_trend", 50)),
                "rsi_period": int(self.config.get("rsi_period", 14)),
                "atr_period": int(self.config.get("atr_period", 14)),
                "min_reward_risk": float(self.config.get("min_reward_risk", 1.5)),
            },
            "indicators": indicators,
            "factors": factors,
            "bullish_points": bullish_points,
            "bearish_points": bearish_points,
            "raw_edge": bullish_points - bearish_points,
        }


def _add_factor(
    factors: list[dict[str, object]],
    name: str,
    direction: str,
    points: int,
    explanation: str,
) -> None:
    factors.append(
        {
            "name": name,
            "direction": direction,
            "points": points,
            "explanation": explanation,
        }
    )


def _direction(points: int) -> str:
    if points > 0:
        return "Bullish"
    if points < 0:
        return "Bearish"
    return "Neutral"


def _round(value: object) -> float:
    return round(float(value), 4)
