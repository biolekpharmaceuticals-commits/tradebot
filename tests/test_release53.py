from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import json
import pandas as pd
import pytest

from src.agent import TradingAgent
from src.config import load_config
from src.derivatives import AngelOneDerivativeDiscovery
from src.market_data import _parse_full_quotes
from src.option_selling import DefinedRiskOptionSellingEngine
from src.safety import SafetyConfigError
from src.strategy import TradeSignal


def contract(strike: float, option_type: str, *, lot_size: int = 65) -> dict:
    suffix = "CE" if option_type == "call" else "PE"
    return {
        "exchange": "NFO",
        "symbol": f"NIFTY27AUG26{int(strike)}{suffix}",
        "token": f"{int(strike)}{suffix}",
        "quantity": lot_size,
        "timeframe": "FIVE_MINUTE",
        "instrument_type": "derivative",
        "derivative_type": option_type,
        "underlying": "NIFTY",
        "expiry": "2026-08-27",
        "strike": strike,
        "lot_size": lot_size,
        "required_decision": "SELL",
    }


def quote(strike: float, option_type: str, bid: float, ask: float, oi: int) -> dict:
    return {
        **contract(strike, option_type),
        "ltp": (bid + ask) / 2,
        "open_interest": oi,
        "volume": 500,
        "best_bid": bid,
        "best_ask": ask,
    }


class ChainDiscovery:
    def __init__(self, contracts):
        self.contracts = contracts

    def option_chain_for(
        self,
        underlying,
        spot_price,
        *,
        strikes_each_side,
        minimum_expiry_days,
        maximum_expiry_days,
    ):
        return self.contracts


class QuoteMarket:
    def __init__(self, quotes):
        self.quotes = quotes

    def get_full_quotes(self, contracts):
        return self.quotes


CONFIG = {
    "enabled": True,
    "paper_only": True,
    "shadow_mode": True,
    "defined_risk_only": True,
    "naked_short_options": False,
    "structures": ["credit_spread", "iron_condor", "iron_fly"],
    "chain_strikes_each_side": 4,
    "wing_width_strikes": 1,
    "minimum_expiry_days": 2,
    "maximum_expiry_days": 10,
    "min_open_interest": 100,
    "min_volume": 100,
    "max_bid_ask_spread_pct": 20,
    "min_credit_to_risk": 0.2,
    "max_risk_per_trade_pct": 0.5,
    "max_daily_loss_pct": 1.0,
    "max_trades_per_day": 2,
    "max_open_structures": 1,
    "entry_start": "09:30",
    "entry_end": "14:30",
    "force_exit": "15:10",
    "bullish_pcr": 1.1,
    "bearish_pcr": 0.9,
}


def liquid_chain(lot_size: int = 65) -> list[dict]:
    quotes = []
    for strike in (90, 95, 100, 105, 110):
        put_oi = 6000 if strike == 95 else 2000
        call_oi = 5500 if strike == 105 else 1800
        bid = 2.0 if strike == 100 else 1.5 if strike in {95, 105} else 0.4
        ask = bid + 0.05
        for option_type, oi in (("put", put_oi), ("call", call_oi)):
            item = quote(strike, option_type, bid, ask, oi)
            item["lot_size"] = lot_size
            item["quantity"] = lot_size
            quotes.append(item)
    return quotes


def test_oi_engine_builds_only_hedged_defined_risk_structures():
    quotes = liquid_chain()
    engine = DefinedRiskOptionSellingEngine(
        CONFIG,
        ChainDiscovery(quotes),
        QuoteMarket(quotes),
        {"capital": 300000},
        clock=lambda: datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    result = engine.propose("NIFTY 50", 100)

    assert result["mode"] == "shadow_defined_risk_only"
    assert result["paper_execution_allowed"] is False
    assert result["structures"]
    assert any(item["name"] == "iron_condor" for item in result["structures"])
    iron_fly = next(item for item in result["structures"] if item["name"] == "iron_fly")
    assert len(iron_fly["legs"]) == 4
    assert [leg["side"] for leg in iron_fly["legs"]].count("BUY") == 2
    assert [leg["side"] for leg in iron_fly["legs"]].count("SELL") == 2
    assert iron_fly["max_loss"] > 0
    assert iron_fly["max_loss"] <= iron_fly["risk_budget"]


def test_one_lot_structure_is_blocked_when_maximum_loss_exceeds_budget():
    quotes = liquid_chain(lot_size=1000)
    engine = DefinedRiskOptionSellingEngine(
        CONFIG,
        ChainDiscovery(quotes),
        QuoteMarket(quotes),
        {"capital": 300000},
        clock=lambda: datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    result = engine.propose("NIFTY 50", 100)

    assert result["structures"]
    assert all(not item["risk_eligible"] for item in result["structures"])
    assert any("risk budget" in blocker for blocker in result["structures"][0]["blockers"])


def test_condor_maximum_loss_uses_the_wider_wing():
    engine = DefinedRiskOptionSellingEngine(
        CONFIG,
        ChainDiscovery([]),
        QuoteMarket([]),
        {"capital": 300000},
        clock=lambda: datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    long_put = quote(90, "put", 0.4, 0.5, 2000)
    short_put = quote(95, "put", 1.5, 1.6, 5000)
    short_call = quote(105, "call", 1.5, 1.6, 5000)
    long_call = quote(115, "call", 0.4, 0.5, 2000)

    structure = engine._structure(
        "iron_condor",
        "NIFTY 50",
        100,
        [("BUY", long_put), ("SELL", short_put), ("SELL", short_call), ("BUY", long_call)],
    )

    assert structure["net_credit_points"] == 2.0
    assert structure["max_loss"] == 520.0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("shadow_mode", False, "shadow_mode"),
        ("defined_risk_only", False, "defined_risk_only"),
        ("naked_short_options", True, "naked_short_options"),
        ("max_risk_per_trade_pct", 1.0, "max_risk_per_trade_pct"),
    ],
)
def test_option_selling_safety_configuration_fails_closed(tmp_path, field, value, message):
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("option_selling:\n  enabled: false", "option_selling:\n  enabled: true")
    text = text.replace("derivatives:\n  enabled: false", "derivatives:\n  enabled: true")
    text = text.replace("market_data:\n  provider: demo", "market_data:\n  provider: angel_one")
    old = f"  {field}: {str(CONFIG[field]).lower()}"
    new = f"  {field}: {str(value).lower()}"
    path = tmp_path / "config.yaml"
    path.write_text(text.replace(old, new), encoding="utf-8")

    with pytest.raises(SafetyConfigError, match=message):
        load_config(path)


def test_full_quote_parser_extracts_oi_depth_without_credentials():
    item = contract(100, "call")
    response = {
        "status": True,
        "data": {
            "fetched": [
                {
                    "exchange": "NFO",
                    "symbolToken": item["token"],
                    "ltp": 10.5,
                    "opnInterest": 12345,
                    "tradeVolume": 900,
                    "depth": {"buy": [{"price": 10.4}], "sell": [{"price": 10.6}]},
                }
            ]
        },
    }

    parsed = _parse_full_quotes(response, {("NFO", item["token"]): item})

    assert parsed[0]["open_interest"] == 12345
    assert parsed[0]["best_bid"] == 10.4
    assert parsed[0]["best_ask"] == 10.6


def test_derivative_discovery_exposes_balanced_option_chain_only():
    master = []
    for strike in (90, 95, 100, 105, 110):
        for suffix in ("CE", "PE"):
            master.append(
                {
                    "exch_seg": "NFO",
                    "name": "NIFTY",
                    "expiry": "27AUG2026",
                    "instrumenttype": "OPTIDX",
                    "symbol": f"NIFTY27AUG26{strike}{suffix}",
                    "token": f"{strike}{suffix}",
                    "strike": str(strike * 100),
                    "lotsize": "65",
                }
            )
    discovery = AngelOneDerivativeDiscovery(
        instruments=["options"],
        option_strikes=1,
        max_expiry_days=20,
        max_contracts=6,
        timeframe="FIVE_MINUTE",
        timeout_seconds=10,
        fetcher=lambda: master,
        today=lambda: date(2026, 8, 20),
        minimum_expiry_days=2,
    )

    contracts = discovery.option_chain_for(
        "NIFTY 50",
        100,
        strikes_each_side=4,
        minimum_expiry_days=2,
        maximum_expiry_days=10,
    )

    assert len(contracts) == 10
    assert {item["derivative_type"] for item in contracts} == {"call", "put"}
    assert all(item["required_decision"] == "SELL" for item in contracts)


def test_option_selling_source_has_no_live_order_path():
    source = (Path(__file__).resolve().parents[1] / "src" / "option_selling.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("placeOrder", "place_order", "modifyOrder", "cancelOrder"):
        assert forbidden not in source


def test_agent_logs_oi_structure_in_shadow_mode_without_paper_order(tmp_path, monkeypatch):
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("scanner:\n  enabled: false", "scanner:\n  enabled: true")
    text = text.replace("derivatives:\n  enabled: false", "derivatives:\n  enabled: true")
    text = text.replace("option_selling:\n  enabled: false", "option_selling:\n  enabled: true")
    text = text.replace("market_data:\n  provider: demo", "market_data:\n  provider: angel_one")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", "false")
    monkeypatch.setenv("AUTO_PAPER_TRADING_ENABLED", "true")

    class Market:
        def get_candles(self, symbol_config):
            return pd.DataFrame(
                {
                    "timestamp": pd.date_range("2026-08-14 09:15", periods=100, freq="5min"),
                    "open": [100.0] * 100,
                    "high": [101.0] * 100,
                    "low": [99.0] * 100,
                    "close": [100.0] * 100,
                    "volume": [0] * 100,
                }
            )

    class Strategy:
        def evaluate(self, candles, news, bulk_deals=None, instrument_type="equity"):
            return TradeSignal("BUY", 90, "Bullish", 100, 98, 103, 1.5, "test")

    class ShadowEngine:
        enabled = True

        def propose(self, underlying, spot_price):
            return {
                "status": "candidate",
                "selected": {"name": "iron_condor", "risk_eligible": True, "score": 10},
                "paper_execution_allowed": False,
                "structures": [],
            }

    agent = TradingAgent(load_config(path))
    agent.market_data = Market()
    agent.strategy = Strategy()
    agent.option_selling = ShadowEngine()

    agent.run_once()

    record = json.loads((tmp_path / "logs" / "decisions.jsonl").read_text(encoding="utf-8"))
    assert record["scanner"]["mode"] == "oi_defined_risk_option_selling_shadow"
    assert record["scanner"]["option_selling"]["naked_short_options"] is False
    assert record["scanner"]["option_selling"]["selected"]["paper_execution_allowed"] is False
    assert record["executed"] is False
    assert record["order"] is None
