from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

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


class TrendStrategy:
    def __init__(self, config: dict) -> None:
        self.config = config

    def evaluate(self, candles: pd.DataFrame, news: NewsSignal) -> TradeSignal:
        df = add_indicators(
            candles,
            ema_fast=int(self.config.get("ema_fast", 9)),
            ema_slow=int(self.config.get("ema_slow", 21)),
            ema_trend=int(self.config.get("ema_trend", 50)),
            rsi_period=int(self.config.get("rsi_period", 14)),
            atr_period=int(self.config.get("atr_period", 14)),
        ).dropna()

        if df.empty:
            return TradeSignal("AVOID", 0, "Neutral", 0, 0, 0, 0, "Not enough candle data")

        latest = df.iloc[-1]
        previous = df.iloc[-2] if len(df) > 1 else latest
        close = float(latest["close"])
        atr = float(latest["atr"])

        bullish_points = 0
        bearish_points = 0
        reasons = []

        if latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"]:
            bullish_points += 25
            reasons.append("EMA stack is bullish")
        elif latest["ema_fast"] < latest["ema_slow"] < latest["ema_trend"]:
            bearish_points += 25
            reasons.append("EMA stack is bearish")

        if close > latest["vwap"]:
            bullish_points += 15
            reasons.append("Price is above VWAP")
        else:
            bearish_points += 15
            reasons.append("Price is below VWAP")

        if 52 <= latest["rsi"] <= 68:
            bullish_points += 15
            reasons.append("RSI supports upward momentum")
        elif 32 <= latest["rsi"] <= 48:
            bearish_points += 15
            reasons.append("RSI supports downward momentum")
        elif latest["rsi"] > 75 or latest["rsi"] < 25:
            reasons.append("RSI is stretched")

        if latest["macd"] > latest["macd_signal"] and previous["macd"] <= previous["macd_signal"]:
            bullish_points += 15
            reasons.append("MACD bullish crossover")
        elif latest["macd"] < latest["macd_signal"] and previous["macd"] >= previous["macd_signal"]:
            bearish_points += 15
            reasons.append("MACD bearish crossover")

        if news.score > 10:
            bullish_points += min(20, news.score // 2)
            reasons.append("News sentiment is supportive")
        elif news.score < -10:
            bearish_points += min(20, abs(news.score) // 2)
            reasons.append("News sentiment is negative")

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

        return TradeSignal(
            decision=decision,
            confidence=int(confidence),
            market_bias=bias,
            entry_price=round(close, 2),
            stop_loss=round(stop_loss, 2),
            target=round(target, 2),
            reward_risk=round(reward_risk, 2),
            reason="; ".join(reasons) or "No strong edge",
        )
