from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Protocol

from app.ibkr.contracts import make_stock_contract
from app.ibkr.request_ids import RequestIdManager
from app.trading.models import Bar


class HistoricalBarProvider(Protocol):
    async def get_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        """Return ascending daily bars for one symbol."""


class InMemoryHistoricalBarProvider:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars_by_symbol = bars_by_symbol

    async def get_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        bars = sorted(self._bars_by_symbol.get(symbol, []), key=lambda bar: bar.date)
        return bars[-lookback_days:]


class IBKRHistoricalBarProvider:
    def __init__(
        self,
        client,
        request_ids: RequestIdManager | None = None,
        timeout_seconds: float = 10.0,
        poll_interval: float = 0.1,
    ) -> None:
        self._client = client
        self._request_ids = request_ids or RequestIdManager()
        self._timeout_seconds = timeout_seconds
        self._poll_interval = poll_interval

    async def get_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        req_id = self._request_ids.allocate("historical")
        try:
            self._client.reqHistoricalData(
                req_id,
                make_stock_contract(symbol),
                "",
                f"{max(1, lookback_days)} D",
                "1 day",
                "TRADES",
                1,
                1,
                False,
                [],
            )
            raw_bars = await self._wait_for_daily_bars(req_id, symbol)
            return sorted(raw_bars, key=lambda bar: bar.date)[-lookback_days:]
        finally:
            self._request_ids.release(req_id)

    async def _wait_for_daily_bars(self, req_id: int, symbol: str) -> list[Bar]:
        deadline = time.monotonic() + self._timeout_seconds
        bars: list[Bar] = []
        complete = False
        while time.monotonic() <= deadline and not complete:
            for item in self._client.drain_historical_data():
                if item.get("req_id") != req_id:
                    continue
                bars.append(_bar_from_ibkr_item(symbol, item))
            for item in self._client.drain_historical_data_end():
                if item.get("req_id") == req_id:
                    complete = True
            if not complete:
                await asyncio.sleep(self._poll_interval)
        return bars


def _bar_from_ibkr_item(symbol: str, item: dict) -> Bar:
    return Bar(
        symbol=symbol,
        date=_parse_ibkr_daily_date(str(item.get("date", ""))),
        open=float(item.get("open", 0.0) or 0.0),
        high=float(item.get("high", 0.0) or 0.0),
        low=float(item.get("low", 0.0) or 0.0),
        close=float(item.get("close", 0.0) or 0.0),
        volume=int(item.get("volume", 0) or 0),
    )


def _parse_ibkr_daily_date(value: str):
    if len(value) == 8 and value.isdigit():
        return datetime.strptime(value, "%Y%m%d").date()
    return datetime.fromisoformat(value).date()
