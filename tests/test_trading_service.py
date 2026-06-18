from datetime import date, timedelta

import pytest

from app.trading.historical_provider import InMemoryHistoricalBarProvider
from app.trading.models import AccountRiskState, Bar
from app.trading.service import TradingService


def _bars(symbol: str, closes: list[float]) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(
            symbol=symbol,
            date=start + timedelta(days=i),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1_000_000,
        )
        for i, close in enumerate(closes)
    ]


@pytest.mark.asyncio
async def test_evaluate_symbol_returns_signal_and_plan_for_buy():
    provider = InMemoryHistoricalBarProvider({"SPY": _bars("SPY", [100.0] * 45 + [101, 102, 103, 104, 115])})
    service = TradingService(provider=provider)
    result = await service.evaluate_symbol(
        symbol="SPY",
        account=AccountRiskState(net_liquidation=11000, available_funds=9000, gross_position_value=0),
        current_position_qty=0,
    )
    assert result["signal"].action.value == "BUY"
    assert result["order_plan"].symbol == "SPY"
    assert result["order_plan"].quantity > 0


@pytest.mark.asyncio
async def test_evaluate_symbol_blocks_when_no_history():
    provider = InMemoryHistoricalBarProvider({})
    service = TradingService(provider=provider)
    result = await service.evaluate_symbol(
        symbol="QQQ",
        account=AccountRiskState(net_liquidation=11000, available_funds=9000),
        current_position_qty=0,
    )
    assert result["signal"].action.value == "BLOCK"
    assert result["order_plan"].status.value == "BLOCKED"
