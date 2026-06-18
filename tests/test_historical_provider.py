from datetime import date

import pytest

from app.trading.historical_provider import InMemoryHistoricalBarProvider
from app.trading.models import Bar


@pytest.mark.asyncio
async def test_in_memory_provider_returns_symbol_bars_sorted():
    provider = InMemoryHistoricalBarProvider(
        {
            "SPY": [
                Bar(symbol="SPY", date=date(2026, 1, 2), open=101, high=102, low=100, close=101, volume=1),
                Bar(symbol="SPY", date=date(2026, 1, 1), open=100, high=101, low=99, close=100, volume=1),
            ]
        }
    )
    bars = await provider.get_daily_bars("SPY", lookback_days=200)
    assert [bar.date.isoformat() for bar in bars] == ["2026-01-01", "2026-01-02"]


@pytest.mark.asyncio
async def test_in_memory_provider_returns_empty_for_missing_symbol():
    provider = InMemoryHistoricalBarProvider({})
    assert await provider.get_daily_bars("QQQ", lookback_days=200) == []
