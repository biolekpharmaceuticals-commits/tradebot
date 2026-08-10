from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import requests


NSE_BULK_DEALS_URL = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
NSE_LARGE_DEALS_PAGE = "https://www.nseindia.com/market-data/large-deals"


@dataclass(frozen=True)
class BulkDealSignal:
    status: str
    direction: str
    score: int
    deal_count: int
    buy_quantity: int
    sell_quantity: int
    net_quantity: int
    latest_date: str | None
    stale: bool
    explanation: str
    source: str
    deals: list[dict[str, object]]


class BulkDealProvider(Protocol):
    def get_signal(self, symbol: str) -> BulkDealSignal:
        ...


class DisabledBulkDealProvider:
    def get_signal(self, symbol: str) -> BulkDealSignal:
        return _neutral_signal("disabled", "Bulk-deal analysis is disabled")


class NSEBulkDealProvider:
    """Read-only adapter for NSE's public bulk-deal publication."""

    def __init__(
        self,
        *,
        max_age_days: int = 7,
        max_confidence_points: int = 10,
        min_imbalance_ratio: float = 0.2,
        timeout_seconds: float = 8.0,
        fetcher: Callable[[], object] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        if max_age_days < 0 or max_age_days > 30:
            raise ValueError("bulk_deals.max_age_days must be between 0 and 30")
        if max_confidence_points < 0 or max_confidence_points > 10:
            raise ValueError("bulk_deals.max_confidence_points must be between 0 and 10")
        if min_imbalance_ratio < 0 or min_imbalance_ratio > 1:
            raise ValueError("bulk_deals.min_imbalance_ratio must be between 0 and 1")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("bulk_deals.timeout_seconds must be between 0 and 30")

        self.max_age_days = max_age_days
        self.max_confidence_points = max_confidence_points
        self.min_imbalance_ratio = min_imbalance_ratio
        self.timeout_seconds = timeout_seconds
        self.fetcher = fetcher or self._fetch_nse
        self.today = today or self._today_kolkata

    def get_signal(self, symbol: str) -> BulkDealSignal:
        normalized_symbol = _normalize_symbol(symbol)
        try:
            response = self.fetcher()
            return self._parse_response(response, normalized_symbol)
        except Exception:
            return _neutral_signal("unavailable", "NSE bulk-deal data is currently unavailable")

    def _parse_response(self, response: object, symbol: str) -> BulkDealSignal:
        if not isinstance(response, dict):
            return _neutral_signal("unavailable", "NSE bulk-deal response was malformed")

        rows = response.get("BULK_DEALS_DATA")
        if not isinstance(rows, list):
            return _neutral_signal("unavailable", "NSE bulk-deal response was malformed")

        latest_date = _optional_text(response.get("as_on_date"))
        stale = _is_stale(latest_date, self.today(), self.max_age_days)
        matching: list[dict[str, object]] = []
        buy_quantity = 0
        sell_quantity = 0

        for row in rows:
            if not isinstance(row, dict) or _normalize_symbol(row.get("symbol")) != symbol:
                continue

            side = _optional_text(row.get("buySell")).upper()
            quantity = _safe_int(row.get("qty"))
            if side == "BUY":
                buy_quantity += quantity
            elif side == "SELL":
                sell_quantity += quantity
            else:
                continue

            matching.append(
                {
                    "date": _optional_text(row.get("date")) or latest_date,
                    "client_name": _optional_text(row.get("clientName")),
                    "side": side,
                    "quantity": quantity,
                    "average_price": _safe_float(row.get("watp")),
                }
            )

        if not matching:
            return BulkDealSignal(
                status="no_match",
                direction="Neutral",
                score=0,
                deal_count=0,
                buy_quantity=0,
                sell_quantity=0,
                net_quantity=0,
                latest_date=latest_date,
                stale=stale,
                explanation=f"No published NSE bulk deals matched {symbol}",
                source=NSE_LARGE_DEALS_PAGE,
                deals=[],
            )

        net_quantity = buy_quantity - sell_quantity
        total_quantity = buy_quantity + sell_quantity
        imbalance = net_quantity / total_quantity if total_quantity else 0.0
        direction = "Neutral"
        score = 0

        if not stale and abs(imbalance) >= self.min_imbalance_ratio:
            points = min(self.max_confidence_points, max(1, round(abs(imbalance) * self.max_confidence_points)))
            if imbalance > 0:
                direction = "Accumulation"
                score = points
            else:
                direction = "Distribution"
                score = -points

        if stale:
            status = "stale"
            explanation = "Published NSE bulk deals are stale and contribute zero confidence points"
        else:
            status = "available"
            explanation = (
                f"{len(matching)} published deal(s); buy quantity {buy_quantity}, "
                f"sell quantity {sell_quantity}; bias {direction.lower()}"
            )

        return BulkDealSignal(
            status=status,
            direction=direction,
            score=score,
            deal_count=len(matching),
            buy_quantity=buy_quantity,
            sell_quantity=sell_quantity,
            net_quantity=net_quantity,
            latest_date=latest_date,
            stale=stale,
            explanation=explanation,
            source=NSE_LARGE_DEALS_PAGE,
            deals=matching[:20],
        )

    def _fetch_nse(self) -> object:
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": NSE_LARGE_DEALS_PAGE,
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/126.0 Safari/537.36"
            ),
        }
        with requests.Session() as session:
            session.headers.update(headers)
            session.get("https://www.nseindia.com/", timeout=self.timeout_seconds)
            response = session.get(NSE_BULK_DEALS_URL, timeout=self.timeout_seconds)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _today_kolkata() -> date:
        return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def build_bulk_deal_provider(config: dict) -> BulkDealProvider:
    if not config.get("enabled", False):
        return DisabledBulkDealProvider()

    provider = str(config.get("provider", "nse")).lower()
    if provider != "nse":
        raise ValueError("bulk_deals.provider must be nse")

    return NSEBulkDealProvider(
        max_age_days=int(config.get("max_age_days", 7)),
        max_confidence_points=int(config.get("max_confidence_points", 10)),
        min_imbalance_ratio=float(config.get("min_imbalance_ratio", 0.2)),
        timeout_seconds=float(config.get("timeout_seconds", 8)),
    )


def validate_bulk_deal_config(config: object) -> None:
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("bulk_deals must be a mapping")

    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("bulk_deals.enabled must be a boolean")
    if str(config.get("provider", "nse")).lower() != "nse":
        raise ValueError("bulk_deals.provider must be nse")

    max_age_days = config.get("max_age_days", 7)
    max_points = config.get("max_confidence_points", 10)
    min_ratio = config.get("min_imbalance_ratio", 0.2)
    timeout = config.get("timeout_seconds", 8)
    if not isinstance(max_age_days, int) or isinstance(max_age_days, bool) or not 0 <= max_age_days <= 30:
        raise ValueError("bulk_deals.max_age_days must be an integer between 0 and 30")
    if not isinstance(max_points, int) or isinstance(max_points, bool) or not 0 <= max_points <= 10:
        raise ValueError("bulk_deals.max_confidence_points must be an integer between 0 and 10")
    if not isinstance(min_ratio, (int, float)) or isinstance(min_ratio, bool) or not 0 <= min_ratio <= 1:
        raise ValueError("bulk_deals.min_imbalance_ratio must be between 0 and 1")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30:
        raise ValueError("bulk_deals.timeout_seconds must be between 0 and 30")


def _neutral_signal(status: str, explanation: str) -> BulkDealSignal:
    return BulkDealSignal(
        status=status,
        direction="Neutral",
        score=0,
        deal_count=0,
        buy_quantity=0,
        sell_quantity=0,
        net_quantity=0,
        latest_date=None,
        stale=False,
        explanation=explanation,
        source=NSE_LARGE_DEALS_PAGE,
        deals=[],
    )


def _normalize_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    return symbol[:-3] if symbol.endswith("-EQ") else symbol


def _optional_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _safe_int(value: object) -> int:
    try:
        return max(0, int(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float | None:
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def _is_stale(value: str, today: date, max_age_days: int) -> bool:
    if not value:
        return True
    try:
        published = datetime.strptime(value, "%d-%b-%Y").date()
    except ValueError:
        return True
    age_days = (today - published).days
    return age_days < 0 or age_days > max_age_days
