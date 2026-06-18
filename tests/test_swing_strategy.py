from datetime import date, timedelta

from app.trading.models import Bar, SignalAction
from app.trading.swing_strategy import SwingTrendStrategy


def _bars(closes: list[float]) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(
            symbol="SPY",
            date=start + timedelta(days=i),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1_000_000,
        )
        for i, close in enumerate(closes)
    ]


def test_blocks_when_history_is_too_short():
    signal = SwingTrendStrategy().evaluate("SPY", _bars([100, 101, 102]), current_position_qty=0)
    assert signal.action == SignalAction.BLOCK
    assert "insufficient_history" in signal.reasons


def test_buy_when_fast_sma_crosses_above_slow_sma_and_no_position():
    closes = [100.0] * 45 + [101.0, 102.0, 103.0, 104.0, 115.0]
    signal = SwingTrendStrategy(fast_window=5, slow_window=20).evaluate(
        "SPY", _bars(closes), current_position_qty=0
    )
    assert signal.action == SignalAction.BUY
    assert "fast_above_slow" in signal.reasons
    assert signal.stop_price > 0


def test_sell_when_close_falls_below_slow_sma_and_position_exists():
    closes = [120.0] * 45 + [118.0, 116.0, 114.0, 112.0, 90.0]
    signal = SwingTrendStrategy(fast_window=5, slow_window=20).evaluate(
        "SPY", _bars(closes), current_position_qty=10, highest_close_since_entry=125.0
    )
    assert signal.action == SignalAction.SELL
    assert "close_below_slow" in signal.reasons


def test_sell_when_trailing_stop_breaks():
    closes = [120.0] * 50
    signal = SwingTrendStrategy(fast_window=5, slow_window=20).evaluate(
        "SPY", _bars(closes), current_position_qty=10, highest_close_since_entry=140.0
    )
    assert signal.action == SignalAction.SELL
    assert "trailing_stop" in signal.reasons
