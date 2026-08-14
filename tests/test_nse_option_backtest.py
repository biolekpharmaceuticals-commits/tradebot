from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

import pytest
import requests

from src.nse_option_backtest import (
    NSEBacktestError,
    NSEFODailyArchive,
    OptionDailyBar,
    load_nse_window,
    normalize_udiff_option,
    parse_udiff_archive,
    run_nse_option_backtest,
)


def bar(
    day: date,
    strike: float,
    option_type: str,
    *,
    symbol: str = "NIFTY",
    expiry: date = date(2026, 8, 20),
    open_price: float = 2,
    close_price: float = 1,
    oi: int = 2000,
    volume: int = 500,
    lot: int = 10,
) -> OptionDailyBar:
    return OptionDailyBar(
        trade_date=day,
        symbol=symbol,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        open=open_price,
        high=max(open_price, close_price),
        low=min(open_price, close_price),
        close=close_price,
        settlement=close_price,
        underlying=100,
        open_interest=oi,
        change_in_open_interest=0,
        volume=volume,
        lot_size=lot,
    )


def chain(day: date, *, lot: int = 10) -> list[OptionDailyBar]:
    rows = []
    for strike in (80, 90, 100, 110, 120):
        put_oi = 10000 if strike == 90 else 2000
        call_oi = 10000 if strike == 110 else 2000
        put_open = 4 if strike == 90 else 1 if strike == 80 else 2
        call_open = 4 if strike == 110 else 1 if strike == 120 else 2
        rows.append(bar(day, strike, "put", open_price=put_open, oi=put_oi, lot=lot))
        rows.append(bar(day, strike, "call", open_price=call_open, oi=call_oi, lot=lot))
    return rows


def config() -> dict:
    return {
        "structures": ["iron_condor"],
        "wing_width_strikes": 1,
        "minimum_expiry_days": 2,
        "maximum_expiry_days": 10,
        "min_open_interest": 1000,
        "min_volume": 100,
        "min_credit_to_risk": 0.2,
        "max_risk_per_trade_pct": 0.5,
        "max_daily_loss_pct": 1.0,
        "bullish_pcr": 1.1,
        "bearish_pcr": 0.9,
    }


def test_udiff_row_normalizes_historical_lot_oi_and_prices():
    result = normalize_udiff_option(
        {
            "TradDt": "2026-08-14",
            "TckrSymb": "NIFTY",
            "XpryDt": "2026-08-20",
            "FininstrmActlXpryDt": "",
            "StrkPric": "24300",
            "OptnTp": "PE",
            "OpnPric": "100.50",
            "HghPric": "110",
            "LwPric": "90",
            "ClsPric": "95",
            "SttlmPric": "96",
            "UndrlygPric": "24366",
            "OpnIntrst": "123,400",
            "ChngInOpnIntrst": "500",
            "TtlTradgVol": "8,000",
            "NewBrdLotQty": "65",
        }
    )

    assert result is not None
    assert result.option_type == "put"
    assert result.open_interest == 123400
    assert result.lot_size == 65
    assert result.open == 100.5


def test_archive_parser_filters_to_supported_index_options():
    rows = [
        {
            "TradDt": "2026-08-14",
            "TckrSymb": "NIFTY",
            "XpryDt": "2026-08-20",
            "StrkPric": "24300",
            "OptnTp": "CE",
            "OpnPric": "10",
            "HghPric": "12",
            "LwPric": "9",
            "ClsPric": "11",
            "SttlmPric": "11",
            "UndrlygPric": "24366",
            "OpnIntrst": "2000",
            "ChngInOpnIntrst": "100",
            "TtlTradgVol": "500",
            "NewBrdLotQty": "65",
        },
        {
            "TradDt": "2026-08-14",
            "TckrSymb": "SBIN",
            "XpryDt": "2026-08-20",
            "StrkPric": "800",
            "OptnTp": "CE",
            "NewBrdLotQty": "750",
        },
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("BhavCopy.csv", output.getvalue())

    parsed = parse_udiff_archive(payload.getvalue())

    assert len(parsed) == 1
    assert parsed[0].symbol == "NIFTY"


def test_backtest_uses_prior_day_oi_and_next_day_open_without_orders():
    signal_day = date(2026, 8, 14)
    trade_day = date(2026, 8, 17)
    daily = {signal_day: chain(signal_day), trade_day: chain(trade_day)}

    report = run_nse_option_backtest(daily, config(), initial_balance=300000, slippage_bps=0, fee_bps=0)

    assert report["trades"] == 1
    assert report["wins"] == 1
    assert report["ending_balance"] > 300000
    trade = report["recent_trades"][0]
    assert trade["structure"] == "iron_condor"
    assert trade["signal_date"] == "2026-08-14"
    assert trade["trade_date"] == "2026-08-17"
    assert {leg["prior_day_open_interest"] for leg in trade["legs"]} >= {10000}


def test_one_lot_structure_above_risk_budget_is_rejected():
    signal_day = date(2026, 8, 14)
    trade_day = date(2026, 8, 17)
    daily = {signal_day: chain(signal_day, lot=1000), trade_day: chain(trade_day, lot=1000)}

    report = run_nse_option_backtest(daily, config(), initial_balance=300000, slippage_bps=0, fee_bps=0)

    assert report["trades"] == 0
    assert report["rejected_for_risk"] >= 1
    assert report["ending_balance"] == 300000


def test_window_fails_closed_when_archive_coverage_is_insufficient(tmp_path):
    archive = NSEFODailyArchive(tmp_path, fetcher=lambda url: None)

    with pytest.raises(NSEBacktestError, match="Only 0 NSE sessions"):
        load_nse_window(
            archive,
            end_date=date(2026, 8, 14),
            calendar_days=10,
            minimum_sessions=2,
        )


def test_archive_retries_transient_connection_failure(tmp_path, monkeypatch):
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "TradDt",
            "TckrSymb",
            "XpryDt",
            "StrkPric",
            "OptnTp",
            "OpnPric",
            "HghPric",
            "LwPric",
            "ClsPric",
            "SttlmPric",
            "UndrlygPric",
            "OpnIntrst",
            "ChngInOpnIntrst",
            "TtlTradgVol",
            "NewBrdLotQty",
        ],
    )
    writer.writeheader()
    writer.writerow(
        {
            "TradDt": "2026-08-14",
            "TckrSymb": "NIFTY",
            "XpryDt": "2026-08-20",
            "StrkPric": "24300",
            "OptnTp": "CE",
            "OpnPric": "10",
            "HghPric": "12",
            "LwPric": "9",
            "ClsPric": "11",
            "SttlmPric": "11",
            "UndrlygPric": "24366",
            "OpnIntrst": "2000",
            "ChngInOpnIntrst": "100",
            "TtlTradgVol": "500",
            "NewBrdLotQty": "65",
        }
    )
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive_file:
        archive_file.writestr("BhavCopy.csv", output.getvalue())

    class Response:
        status_code = 200
        content = zipped.getvalue()
        headers = {}

    class FlakySession:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers, timeout):
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError("temporary reset")
            return Response()

    session = FlakySession()
    monkeypatch.setattr("src.nse_option_backtest.time.sleep", lambda seconds: None)
    archive = NSEFODailyArchive(
        tmp_path,
        request_interval_seconds=0,
        max_retries=2,
        session=session,
    )

    rows = archive.load(date(2026, 8, 14))

    assert session.calls == 2
    assert rows is not None and len(rows) == 1
