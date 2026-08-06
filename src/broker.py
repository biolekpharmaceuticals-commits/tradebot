from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from .demo_data import demo_candles
from .safety import LiveTradingDisabledError
from .strategy import TradeSignal


@dataclass(frozen=True)
class OrderResult:
    accepted: bool
    order_id: str
    message: str


class Broker(Protocol):
    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        ...

    def place_order(self, symbol_config: dict, signal: TradeSignal, quantity: int) -> OrderResult:
        ...


class PaperBroker:
    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        return demo_candles()

    def place_order(self, symbol_config: dict, signal: TradeSignal, quantity: int) -> OrderResult:
        symbol = symbol_config.get("symbol", "UNKNOWN")
        order_id = f"PAPER-{symbol}-{signal.decision}-{quantity}"
        return OrderResult(True, order_id, "Paper order recorded")


class AngelOneBroker:
    def __init__(self, config: dict) -> None:
        self.config = config
        self._smart = None

    def _client(self):
        raise LiveTradingDisabledError("Angel One live API access is disabled in Release 1")

    def get_candles(self, symbol_config: dict) -> pd.DataFrame:
        raise NotImplementedError("Wire historical candle fetch for your exact exchange/timeframe before live use")

    def place_order(self, symbol_config: dict, signal: TradeSignal, quantity: int) -> OrderResult:
        raise LiveTradingDisabledError("Live order submission is disabled in Release 1")
