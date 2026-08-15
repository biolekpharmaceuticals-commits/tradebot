from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.agent import TradingAgent
from src.config import load_config
from src.market_data import AngelOneMarketDataProvider
from src.option_selling import DefinedRiskOptionSellingEngine
from src.safety import SafetyConfigError
from src.spot_trend import evaluate_daily_spot_trend
from src.strategy import TradeSignal
from tests.test_release53 import ChainDiscovery, QuoteMarket, liquid_chain


def daily_candles(start: date, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [datetime.combine(start + timedelta(days=index), datetime.min.time()) for index in range(len(closes))],
            "close": closes,
        }
    )


def frozen_config() -> dict:
    return {
        "enabled": True,
        "underlyings": ["NIFTY 50"],
        "timeframe": "ONE_DAY",
        "lookback_days": 30,
        "ema_fast": 5,
        "ema_slow": 20,
        "momentum_days": 5,
        "momentum_threshold_pct": 0.15,
        "volatility_lookback": 10,
        "minimum_volatility_pct": 0,
        "maximum_volatility_pct": 100,
        "allow_range_condor": True,
    }


def test_daily_spot_trend_uses_only_completed_prior_sessions():
    start = date(2026, 7, 15)
    closes = [100 + index for index in range(21)] + [1]
    candles = daily_candles(start, closes)

    result = evaluate_daily_spot_trend(
        candles,
        frozen_config(),
        today=start + timedelta(days=21),
    )

    assert result["eligible"] is True
    assert result["regime"] == "bullish"
    assert result["spot_close"] == 120
    assert result["signal_session"] == (start + timedelta(days=20)).isoformat()


@pytest.mark.parametrize(
    ("regime", "expected"),
    [
        ("bullish", "bull_put_credit_spread"),
        ("bearish", "bear_call_credit_spread"),
        ("range", "iron_condor"),
    ],
)
def test_option_engine_emits_only_the_structure_aligned_to_spot_regime(regime, expected):
    quotes = liquid_chain()
    for item in quotes:
        if regime == "bullish":
            item["open_interest"] *= 5 if item["derivative_type"] == "put" else 1
        elif regime == "bearish":
            item["open_interest"] *= 5 if item["derivative_type"] == "call" else 1
        else:
            item["open_interest"] = 2000
    config = {
        "structures": ["credit_spread", "iron_condor"],
        "wing_width_strikes": 1,
        "minimum_expiry_days": 2,
        "maximum_expiry_days": 10,
        "min_open_interest": 100,
        "min_volume": 100,
        "max_bid_ask_spread_pct": 20,
        "min_credit_to_risk": 0.1,
        "max_risk_per_trade_pct": 1.5,
        "bullish_pcr": 1.1,
        "bearish_pcr": 0.9,
    }
    engine = DefinedRiskOptionSellingEngine(
        config,
        ChainDiscovery(quotes),
        QuoteMarket(quotes),
        {"capital": 300000},
        clock=lambda: datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    result = engine.propose("NIFTY 50", 100, regime)

    assert result["spot_regime"] == regime
    assert {item["name"] for item in result["structures"]} == {expected}


def test_frozen_trend_config_rejects_iron_fly(tmp_path):
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("option_selling:\n  enabled: false", "option_selling:\n  enabled: true")
    text = text.replace("derivatives:\n  enabled: false", "derivatives:\n  enabled: true")
    text = text.replace("market_data:\n  provider: demo", "market_data:\n  provider: angel_one")
    text = text.replace("  spot_trend:\n    enabled: false", "  spot_trend:\n    enabled: true")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(SafetyConfigError, match="credit_spread and iron_condor"):
        load_config(path)


def test_symbol_specific_candle_lookback_is_bounded():
    provider = AngelOneMarketDataProvider(
        lookback_days=5,
        client_factory=lambda key: None,
        clock=lambda: datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    captured = {}
    provider.get_candles_between = lambda symbol, start, end: captured.update(
        {"days": (end - start).days}
    ) or pd.DataFrame()

    provider.get_candles(
        {"exchange": "NSE", "token": "99926000", "timeframe": "ONE_DAY", "lookback_days": 30}
    )

    assert captured["days"] == 30


def test_agent_logs_frozen_trend_shadow_without_any_order(tmp_path, monkeypatch):
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("scanner:\n  enabled: false", "scanner:\n  enabled: true")
    text = text.replace("derivatives:\n  enabled: false", "derivatives:\n  enabled: true")
    text = text.replace("option_selling:\n  enabled: false", "option_selling:\n  enabled: true")
    text = text.replace("market_data:\n  provider: demo", "market_data:\n  provider: angel_one")
    text = text.replace("    - iron_fly\n", "")
    text = text.replace("  max_risk_per_trade_pct: 0.5", "  max_risk_per_trade_pct: 1.5")
    text = text.replace("  max_daily_loss_pct: 1.0", "  max_daily_loss_pct: 1.5")
    text = text.replace("  spot_trend:\n    enabled: false", "  spot_trend:\n    enabled: true")
    text = text.replace("paper_execution:\n  enabled: false", "paper_execution:\n  enabled: true")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "true")

    intraday = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-08-14 09:15", periods=100, freq="5min"),
            "open": [100.0] * 100,
            "high": [101.0] * 100,
            "low": [99.0] * 100,
            "close": [100.0] * 100,
            "volume": [200000] * 100,
        }
    )
    sessions = pd.bdate_range(end="2026-08-14", periods=25)
    daily = pd.DataFrame(
        {
            "timestamp": sessions,
            "close": [100 + 0.5 * index + (1 if index % 2 else -1) for index in range(25)],
        }
    )

    class Market:
        def get_candles(self, symbol_config):
            return daily if symbol_config.get("timeframe") == "ONE_DAY" else intraday

    class Strategy:
        def evaluate(self, candles, news, bulk_deals=None, instrument_type="equity"):
            return TradeSignal("BUY", 90, "Bullish", 100, 98, 103, 1.5, "test")

    class ShadowEngine:
        enabled = True

        def __init__(self):
            self.regime = None

        def propose(self, underlying, spot_price, regime=None):
            self.regime = regime
            return {
                "status": "candidate",
                "selected": {"name": "bull_put_credit_spread", "risk_eligible": True, "score": 10},
                "paper_execution_allowed": False,
                "structures": [],
            }

    class NoOrderBroker:
        def place_order(self, *args, **kwargs):
            raise AssertionError("Frozen option-selling shadow must never place an order")

    agent = TradingAgent(load_config(path))
    agent.market_data = Market()
    agent.strategy = Strategy()
    agent.option_selling = ShadowEngine()
    agent.broker = NoOrderBroker()

    agent.run_once()

    assert agent.option_selling.regime == "bullish"
    payload = (tmp_path / "logs" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"mode": "oi_defined_risk_option_selling_shadow"' in payload
    assert '"executed": false' in payload
    assert '"order": null' in payload
