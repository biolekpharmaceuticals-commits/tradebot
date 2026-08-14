from __future__ import annotations

import csv
import io
import json
import math
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import requests


NSE_FO_ARCHIVE = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{trade_date}_F_0000.csv.zip"
)
SUPPORTED_UNDERLYINGS = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}


class NSEBacktestError(RuntimeError):
    """Raised when NSE archives cannot support a trustworthy backtest."""


@dataclass(frozen=True)
class OptionDailyBar:
    trade_date: date
    symbol: str
    expiry: date
    strike: float
    option_type: str
    open: float
    high: float
    low: float
    close: float
    settlement: float
    underlying: float
    open_interest: int
    change_in_open_interest: int
    volume: int
    lot_size: int

    @property
    def key(self) -> tuple[str, date, float, str]:
        return self.symbol, self.expiry, self.strike, self.option_type


class NSEFODailyArchive:
    """Cached, read-only loader for NSE's public F&O UDiFF bhavcopy."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        timeout_seconds: float = 20,
        request_interval_seconds: float = 0.35,
        max_retries: int = 2,
        fetcher: Callable[[str], bytes | None] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout_seconds = timeout_seconds
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self.fetcher = fetcher or self._fetch
        self._last_request_at = 0.0

    @staticmethod
    def url_for(trade_date: date) -> str:
        return NSE_FO_ARCHIVE.format(trade_date=trade_date.strftime("%Y%m%d"))

    def load(self, trade_date: date) -> list[OptionDailyBar] | None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.cache_dir / f"nse-fo-{trade_date.isoformat()}.zip"
        if archive_path.exists():
            payload = archive_path.read_bytes()
        else:
            payload = self.fetcher(self.url_for(trade_date))
            if payload is None:
                return None
            _validate_zip(payload)
            archive_path.write_bytes(payload)
        return parse_udiff_archive(payload)

    def _fetch(self, url: str) -> bytes | None:
        headers = {
            "Accept": "application/zip,application/octet-stream,*/*",
            "Referer": "https://www.nseindia.com/all-reports-derivatives",
            "User-Agent": "Mozilla/5.0 (compatible; tradebot-research/5.4)",
        }
        last_error = "NSE archive request failed"
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.request_interval_seconds:
                time.sleep(self.request_interval_seconds - elapsed)
            try:
                response = requests.get(url, headers=headers, timeout=self.timeout_seconds)
                self._last_request_at = time.monotonic()
            except requests.RequestException:
                last_error = "NSE archive request failed"
            else:
                if response.status_code == 404:
                    return None
                if response.status_code == 200:
                    _validate_zip(response.content)
                    return response.content
                last_error = f"NSE archive returned HTTP {response.status_code}"
            if attempt < self.max_retries:
                time.sleep(1.0 * (attempt + 1))
        raise NSEBacktestError(last_error)


def parse_udiff_archive(payload: bytes) -> list[OptionDailyBar]:
    _validate_zip(payload)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise NSEBacktestError("NSE archive must contain exactly one CSV file")
        text = archive.read(members[0]).decode("utf-8-sig", errors="strict")
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        normalized = normalize_udiff_option(raw)
        if normalized is not None:
            rows.append(normalized)
    return rows


def normalize_udiff_option(raw: dict[str, object]) -> OptionDailyBar | None:
    row = {_header(key): str(value or "").strip() for key, value in raw.items() if key is not None}
    option_code = _value(row, "optntp").upper()
    if option_code not in {"CE", "PE"}:
        return None
    symbol = _value(row, "tckrsymb").upper()
    if symbol not in SUPPORTED_UNDERLYINGS:
        return None
    expiry_text = _value(row, "fininstrmactlxprydt") or _value(row, "xprydt")
    try:
        trade_date = date.fromisoformat(_value(row, "traddt"))
        expiry = date.fromisoformat(expiry_text)
    except ValueError:
        raise NSEBacktestError("NSE archive contains an invalid trade or expiry date") from None
    strike = _number(_value(row, "strkpric"))
    lot_size = _integer(_value(row, "newbrdlotqty"))
    if strike <= 0 or lot_size <= 0:
        return None
    return OptionDailyBar(
        trade_date=trade_date,
        symbol=symbol,
        expiry=expiry,
        strike=strike,
        option_type="call" if option_code == "CE" else "put",
        open=_number(_value(row, "opnpric")),
        high=_number(_value(row, "hghpric")),
        low=_number(_value(row, "lwpric")),
        close=_number(_value(row, "clspric")),
        settlement=_number(_value(row, "sttlmpric")),
        underlying=_number(_value(row, "undrlygpric")),
        open_interest=_integer(_value(row, "opnintrst")),
        change_in_open_interest=_integer(_value(row, "chnginopnintrst")),
        volume=_integer(_value(row, "ttltradgvol")),
        lot_size=lot_size,
    )


def load_nse_window(
    archive: NSEFODailyArchive,
    *,
    end_date: date,
    calendar_days: int,
    minimum_sessions: int = 45,
) -> tuple[dict[date, list[OptionDailyBar]], dict[str, object]]:
    if not 10 <= calendar_days <= 366:
        raise ValueError("calendar_days must be between 10 and 366")
    if minimum_sessions < 2:
        raise ValueError("minimum_sessions must be at least 2")
    start_date = end_date - timedelta(days=calendar_days - 1)
    loaded: dict[date, list[OptionDailyBar]] = {}
    requested_weekdays = 0
    missing_weekdays: list[str] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:
            requested_weekdays += 1
            rows = archive.load(cursor)
            if rows is None:
                missing_weekdays.append(cursor.isoformat())
            elif rows:
                loaded[cursor] = rows
        cursor += timedelta(days=1)
    if len(loaded) < minimum_sessions:
        raise NSEBacktestError(
            f"Only {len(loaded)} NSE sessions were loaded; at least {minimum_sessions} are required"
        )
    return loaded, {
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "requested_calendar_days": calendar_days,
        "requested_weekdays": requested_weekdays,
        "sessions_loaded": len(loaded),
        "missing_weekdays": missing_weekdays,
    }


def run_nse_option_backtest(
    daily_bars: dict[date, list[OptionDailyBar]],
    option_config: dict,
    *,
    initial_balance: float = 300000,
    slippage_bps: float = 5,
    fee_bps: float = 10,
) -> dict[str, object]:
    dates = sorted(daily_bars)
    if len(dates) < 2:
        raise NSEBacktestError("At least two NSE sessions are required")
    capital = float(initial_balance)
    risk_budget = capital * float(option_config.get("max_risk_per_trade_pct", 0.5)) / 100
    daily_loss_limit = capital * float(option_config.get("max_daily_loss_pct", 1.0)) / 100
    equity = capital
    equity_peak = capital
    maximum_drawdown = 0.0
    trades: list[dict[str, object]] = []
    signals_evaluated = 0
    rejected_for_risk = 0
    rejected_for_data = 0

    for signal_date, trade_date in zip(dates, dates[1:]):
        signal_rows = daily_bars[signal_date]
        trade_rows = daily_bars[trade_date]
        candidates = []
        for symbol in SUPPORTED_UNDERLYINGS:
            signals_evaluated += 1
            proposals = _proposals_for_symbol(
                signal_rows,
                trade_rows,
                symbol,
                option_config,
                risk_budget=risk_budget,
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
            )
            rejected_for_risk += sum(item["rejection"] == "risk" for item in proposals)
            rejected_for_data += sum(item["rejection"] == "data" for item in proposals)
            candidates.extend(item for item in proposals if item["eligible"])
        if not candidates:
            continue
        selected = max(candidates, key=lambda item: float(item["score"]))
        net_pnl = float(selected["net_pnl"])
        net_pnl = max(net_pnl, -float(selected["max_loss"]) - float(selected["fees"]))
        net_pnl = min(net_pnl, float(selected["max_profit"]) - float(selected["fees"]))
        if net_pnl < -daily_loss_limit:
            net_pnl = -daily_loss_limit
        equity += net_pnl
        equity_peak = max(equity_peak, equity)
        drawdown = (equity_peak - equity) / equity_peak * 100 if equity_peak else 0.0
        maximum_drawdown = max(maximum_drawdown, drawdown)
        trades.append(
            {
                **{key: value for key, value in selected.items() if key not in {"eligible", "rejection"}},
                "signal_date": signal_date.isoformat(),
                "trade_date": trade_date.isoformat(),
                "net_pnl": round(net_pnl, 2),
                "ending_balance": round(equity, 2),
            }
        )

    wins = [trade for trade in trades if float(trade["net_pnl"]) > 0]
    losses = [trade for trade in trades if float(trade["net_pnl"]) < 0]
    gross_profit = sum(float(trade["net_pnl"]) for trade in wins)
    gross_loss = abs(sum(float(trade["net_pnl"]) for trade in losses))
    by_structure = _group_trades(trades, "structure")
    by_underlying = _group_trades(trades, "underlying")
    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        "strategy": "OI defined-risk option selling (NSE EOD adaptation)",
        "mode": "historical_backtest_only",
        "period_start": dates[0].isoformat(),
        "period_end": dates[-1].isoformat(),
        "sessions": len(dates),
        "signals_evaluated": signals_evaluated,
        "initial_balance": round(capital, 2),
        "ending_balance": round(equity, 2),
        "net_pnl": round(equity - capital, 2),
        "net_return_pct": round((equity / capital - 1) * 100, 2) if capital else 0.0,
        "max_drawdown_pct": round(maximum_drawdown, 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "average_trade_pnl": round((equity - capital) / len(trades), 2) if trades else 0.0,
        "risk_budget_per_structure": round(risk_budget, 2),
        "daily_loss_limit": round(daily_loss_limit, 2),
        "rejected_for_risk": rejected_for_risk,
        "rejected_for_data": rejected_for_data,
        "by_structure": by_structure,
        "by_underlying": by_underlying,
        "recent_trades": trades[-20:],
        "methodology": {
            "signal": "Prior trading day's OI and volume; nearest eligible 2-10 DTE expiry",
            "entry": "Next trading day's official option opening price with adverse slippage",
            "exit": "Same trading day's official option closing price with adverse slippage",
            "structures": list(option_config.get("structures", [])),
            "defined_risk_only": True,
            "naked_short_options": False,
            "maximum_open_structures": 1,
            "lookahead_bias": "Prior-day OI is used; trade-day OI is never used for selection",
            "limitations": (
                "Free NSE bhavcopy is end-of-day data. Intraday 09:30-14:30 timing, bid/ask history, "
                "intraday OI changes, stop paths, and partial fills cannot be reconstructed."
            ),
        },
    }


def build_nse_option_backtest_report(
    daily_bars: dict[date, list[OptionDailyBar]],
    coverage: dict[str, object],
    option_config: dict,
    *,
    initial_balance: float,
    slippage_bps: float,
    fee_bps: float,
) -> dict[str, object]:
    result = run_nse_option_backtest(
        daily_bars,
        option_config,
        initial_balance=initial_balance,
        slippage_bps=slippage_bps,
        fee_bps=fee_bps,
    )
    return {
        "source": "NSE F&O UDiFF Common Bhavcopy Final",
        "source_url": "https://www.nseindia.com/all-reports-derivatives",
        "coverage": coverage,
        "cost_assumptions": {"slippage_bps_per_side": slippage_bps, "fee_bps_per_side": fee_bps},
        "result": result,
    }


def _proposals_for_symbol(
    signal_rows: list[OptionDailyBar],
    trade_rows: list[OptionDailyBar],
    symbol: str,
    config: dict,
    *,
    risk_budget: float,
    slippage_bps: float,
    fee_bps: float,
) -> list[dict[str, object]]:
    minimum_oi = int(config.get("min_open_interest", 1000))
    minimum_volume = int(config.get("min_volume", 100))
    minimum_dte = int(config.get("minimum_expiry_days", 2))
    maximum_dte = int(config.get("maximum_expiry_days", 10))
    eligible = [
        item
        for item in signal_rows
        if item.symbol == symbol
        and minimum_dte <= (item.expiry - item.trade_date).days <= maximum_dte
        and item.open_interest >= minimum_oi
        and item.volume >= minimum_volume
    ]
    if not eligible:
        return []
    expiry = min(item.expiry for item in eligible)
    chain = [item for item in eligible if item.expiry == expiry]
    spot_values = [item.underlying for item in chain if item.underlying > 0]
    if not spot_values:
        return []
    spot = float(median(spot_values))
    by_key = {(item.strike, item.option_type): item for item in chain}
    strikes = sorted({item.strike for item in chain})
    if len(strikes) < 5:
        return []
    calls = [item for item in chain if item.option_type == "call"]
    puts = [item for item in chain if item.option_type == "put"]
    call_oi = sum(item.open_interest for item in calls)
    put_oi = sum(item.open_interest for item in puts)
    pcr = put_oi / call_oi if call_oi else 0.0
    width = int(config.get("wing_width_strikes", 2))
    allowed = set(config.get("structures", ["credit_spread", "iron_condor", "iron_fly"]))
    specs: list[tuple[str, list[tuple[str, OptionDailyBar]]]] = []
    short_put = _highest_oi(
        item for item in puts if item.strike < spot and _wing(strikes, item.strike, -width) is not None
    )
    short_call = _highest_oi(
        item for item in calls if item.strike > spot and _wing(strikes, item.strike, width) is not None
    )
    if "iron_condor" in allowed and short_put and short_call:
        long_put = by_key.get((_wing(strikes, short_put.strike, -width), "put"))
        long_call = by_key.get((_wing(strikes, short_call.strike, width), "call"))
        if long_put and long_call:
            specs.append(
                ("iron_condor", [("BUY", long_put), ("SELL", short_put), ("SELL", short_call), ("BUY", long_call)])
            )
    atm = min(strikes, key=lambda strike: abs(strike - spot))
    if "iron_fly" in allowed:
        atm_put = by_key.get((atm, "put"))
        atm_call = by_key.get((atm, "call"))
        long_put = by_key.get((_wing(strikes, atm, -width), "put"))
        long_call = by_key.get((_wing(strikes, atm, width), "call"))
        if atm_put and atm_call and long_put and long_call:
            specs.append(
                ("iron_fly", [("BUY", long_put), ("SELL", atm_put), ("SELL", atm_call), ("BUY", long_call)])
            )
    if "credit_spread" in allowed and pcr >= float(config.get("bullish_pcr", 1.1)) and short_put:
        long_put = by_key.get((_wing(strikes, short_put.strike, -width), "put"))
        if long_put:
            specs.append(("bull_put_credit_spread", [("BUY", long_put), ("SELL", short_put)]))
    elif "credit_spread" in allowed and pcr <= float(config.get("bearish_pcr", 0.9)) and short_call:
        long_call = by_key.get((_wing(strikes, short_call.strike, width), "call"))
        if long_call:
            specs.append(("bear_call_credit_spread", [("SELL", short_call), ("BUY", long_call)]))

    trade_by_key = {item.key: item for item in trade_rows}
    proposals = []
    for name, signal_legs in specs:
        priced_legs = []
        for side, signal_leg in signal_legs:
            trade_leg = trade_by_key.get(signal_leg.key)
            if trade_leg is None:
                priced_legs = []
                break
            priced_legs.append((side, signal_leg, trade_leg))
        proposals.append(
            _price_structure(
                name,
                symbol,
                spot,
                pcr,
                priced_legs,
                risk_budget=risk_budget,
                minimum_credit_to_risk=float(config.get("min_credit_to_risk", 0.2)),
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
            )
        )
    return proposals


def _price_structure(
    name: str,
    symbol: str,
    spot: float,
    pcr: float,
    legs: list[tuple[str, OptionDailyBar, OptionDailyBar]],
    *,
    risk_budget: float,
    minimum_credit_to_risk: float,
    slippage_bps: float,
    fee_bps: float,
) -> dict[str, object]:
    if not legs:
        return {"structure": name, "eligible": False, "rejection": "data", "score": 0.0}
    lot_sizes = {trade.lot_size for _, _, trade in legs}
    if len(lot_sizes) != 1 or next(iter(lot_sizes), 0) <= 0:
        return {"structure": name, "eligible": False, "rejection": "data", "score": 0.0}
    if any(trade.open <= 0 or trade.close <= 0 or trade.volume <= 0 for _, _, trade in legs):
        return {"structure": name, "eligible": False, "rejection": "data", "score": 0.0}
    lot_size = next(iter(lot_sizes))
    slip = slippage_bps / 10000
    entry_prices = [trade.open * (1 - slip if side == "SELL" else 1 + slip) for side, _, trade in legs]
    exit_prices = [trade.close * (1 + slip if side == "SELL" else 1 - slip) for side, _, trade in legs]
    credit_points = sum(price if side == "SELL" else -price for price, (side, _, _) in zip(entry_prices, legs))
    widths = []
    for option_type in {signal.option_type for _, signal, _ in legs}:
        shorts = [signal.strike for side, signal, _ in legs if side == "SELL" and signal.option_type == option_type]
        longs = [signal.strike for side, signal, _ in legs if side == "BUY" and signal.option_type == option_type]
        widths.extend(abs(short - long) for short in shorts for long in longs)
    protective_width = max((value for value in widths if value > 0), default=0.0)
    max_loss_points = protective_width - credit_points
    max_profit = max(0.0, credit_points * lot_size)
    max_loss = max(0.0, max_loss_points * lot_size)
    credit_to_risk = max_profit / max_loss if max_loss else 0.0
    fees = sum((entry + exit) * lot_size * fee_bps / 10000 for entry, exit in zip(entry_prices, exit_prices))
    pnl_points = sum(
        entry - exit if side == "SELL" else exit - entry
        for entry, exit, (side, _, _) in zip(entry_prices, exit_prices, legs)
    )
    net_pnl = pnl_points * lot_size - fees
    eligible = (
        credit_points > 0
        and protective_width > 0
        and max_loss > 0
        and max_loss <= risk_budget
        and credit_to_risk >= minimum_credit_to_risk
    )
    short_oi = sum(signal.open_interest for side, signal, _ in legs if side == "SELL")
    score = credit_to_risk * 100 + math.log10(max(short_oi, 1))
    return {
        "structure": name,
        "underlying": SUPPORTED_UNDERLYINGS[symbol],
        "expiry": legs[0][1].expiry.isoformat(),
        "signal_spot": round(spot, 2),
        "put_call_oi_ratio": round(pcr, 4),
        "lot_size": lot_size,
        "lots": 1 if eligible else 0,
        "net_credit_points": round(credit_points, 4),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "credit_to_risk": round(credit_to_risk, 4),
        "fees": round(fees, 2),
        "net_pnl": round(net_pnl, 2),
        "score": round(score, 4),
        "eligible": eligible,
        "rejection": None if eligible else "risk",
        "legs": [
            {
                "side": side,
                "option_type": signal.option_type,
                "strike": signal.strike,
                "entry_open": round(entry, 4),
                "exit_close": round(exit, 4),
                "prior_day_open_interest": signal.open_interest,
            }
            for entry, exit, (side, signal, _) in zip(entry_prices, exit_prices, legs)
        ],
    }


def _group_trades(trades: list[dict[str, object]], key: str) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for trade in trades:
        grouped.setdefault(str(trade.get(key)), []).append(trade)
    return {
        name: {
            "trades": len(items),
            "wins": sum(float(item["net_pnl"]) > 0 for item in items),
            "net_pnl": round(sum(float(item["net_pnl"]) for item in items), 2),
        }
        for name, items in grouped.items()
    }


def _highest_oi(items: Iterable[OptionDailyBar]) -> OptionDailyBar | None:
    return max(items, key=lambda item: item.open_interest, default=None)


def _wing(strikes: list[float], strike: float, offset: int) -> float | None:
    try:
        target = strikes.index(strike) + offset
    except ValueError:
        return None
    return strikes[target] if 0 <= target < len(strikes) else None


def _validate_zip(payload: bytes) -> None:
    if not payload.startswith(b"PK"):
        raise NSEBacktestError("NSE archive response was not a ZIP file")
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise NSEBacktestError("NSE archive ZIP file was invalid")


def _header(value: object) -> str:
    return "".join(character.lower() for character in str(value).strip() if character.isalnum())


def _value(row: dict[str, str], key: str) -> str:
    return row.get(key, "")


def _number(value: object) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except ValueError:
        return 0.0


def _integer(value: object) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except ValueError:
        return 0


def render_summary(report: dict[str, object], report_file: Path | None = None) -> str:
    result = dict(report["result"])
    return json.dumps(
        {
            "source": report["source"],
            "coverage": report["coverage"],
            "strategy": result.get("strategy"),
            "initial_balance": result.get("initial_balance"),
            "ending_balance": result.get("ending_balance"),
            "net_pnl": result.get("net_pnl"),
            "net_return_pct": result.get("net_return_pct"),
            "max_drawdown_pct": result.get("max_drawdown_pct"),
            "trades": result.get("trades"),
            "wins": result.get("wins"),
            "losses": result.get("losses"),
            "win_rate_pct": result.get("win_rate_pct"),
            "profit_factor": result.get("profit_factor"),
            "rejected_for_risk": result.get("rejected_for_risk"),
            "rejected_for_data": result.get("rejected_for_data"),
            "by_structure": result.get("by_structure"),
            "by_underlying": result.get("by_underlying"),
            "report_file": str(report_file) if report_file else None,
            "limitations": result.get("methodology", {}).get("limitations"),
        },
        indent=2,
        default=str,
    )
