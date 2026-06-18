from __future__ import annotations

from app.trading.models import Bar


def sma(values: list[float], window: int) -> float:
    if window <= 0:
        raise ValueError("window must be positive")
    if len(values) < window:
        return 0.0
    return sum(values[-window:]) / window


def atr(bars: list[Bar], window: int = 14) -> float:
    if window <= 0:
        raise ValueError("window must be positive")
    if len(bars) < window + 1:
        return 0.0
    true_ranges: list[float] = []
    for idx in range(1, len(bars)):
        current = bars[idx]
        previous = bars[idx - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges[-window:]) / window
