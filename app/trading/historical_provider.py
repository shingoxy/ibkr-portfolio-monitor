from __future__ import annotations

from typing import Protocol

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
