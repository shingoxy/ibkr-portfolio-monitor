from datetime import date, timedelta

from app.trading.indicators import atr, sma
from app.trading.models import Bar


def _bars(closes: list[float]) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(
            symbol="SPY",
            date=start + timedelta(days=i),
            open=close - 0.5,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000,
        )
        for i, close in enumerate(closes)
    ]


def test_sma_returns_zero_until_enough_values():
    assert sma([10, 11], 3) == 0.0


def test_sma_returns_average_of_last_window():
    assert sma([10, 11, 12, 13], 3) == 12.0


def test_atr_uses_true_range_average():
    bars = _bars([10, 12, 11, 13])
    assert round(atr(bars, 3), 2) == 2.67


def test_atr_returns_zero_until_enough_bars():
    assert atr(_bars([10]), 14) == 0.0
