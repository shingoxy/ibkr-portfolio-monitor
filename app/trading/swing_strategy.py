from __future__ import annotations

from app.trading.indicators import atr, sma
from app.trading.models import Bar, SignalAction, SwingSignal


class SwingTrendStrategy:
    def __init__(
        self,
        fast_window: int = 20,
        slow_window: int = 50,
        atr_window: int = 14,
        trailing_stop_pct: float = 0.08,
        atr_stop_multiple: float = 2.0,
    ) -> None:
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.atr_window = atr_window
        self.trailing_stop_pct = trailing_stop_pct
        self.atr_stop_multiple = atr_stop_multiple

    def evaluate(
        self,
        symbol: str,
        bars: list[Bar],
        current_position_qty: int,
        highest_close_since_entry: float = 0.0,
    ) -> SwingSignal:
        min_required = max(self.slow_window, self.atr_window + 1)
        if len(bars) < min_required:
            return SwingSignal(symbol=symbol, action=SignalAction.BLOCK, reasons=["insufficient_history"])

        closes = [bar.close for bar in bars]
        close = closes[-1]
        fast = sma(closes, self.fast_window)
        slow = sma(closes, self.slow_window)
        current_atr = atr(bars, self.atr_window)
        stop_distance = max(current_atr * self.atr_stop_multiple, close * 0.05)
        stop_price = round(max(0.01, close - stop_distance), 2)

        if current_position_qty > 0:
            trailing_reference = highest_close_since_entry if highest_close_since_entry > 0 else close
            trailing_stop = trailing_reference * (1 - self.trailing_stop_pct)
            if close < slow:
                return SwingSignal(
                    symbol=symbol,
                    action=SignalAction.SELL,
                    close=close,
                    sma_fast=fast,
                    sma_slow=slow,
                    atr=current_atr,
                    stop_price=round(trailing_stop, 2),
                    reasons=["close_below_slow"],
                )
            if close <= trailing_stop:
                return SwingSignal(
                    symbol=symbol,
                    action=SignalAction.SELL,
                    close=close,
                    sma_fast=fast,
                    sma_slow=slow,
                    atr=current_atr,
                    stop_price=round(trailing_stop, 2),
                    reasons=["trailing_stop"],
                )
            return SwingSignal(
                symbol=symbol,
                action=SignalAction.HOLD,
                close=close,
                sma_fast=fast,
                sma_slow=slow,
                atr=current_atr,
                stop_price=round(trailing_stop, 2),
                reasons=["position_held"],
            )

        if close > slow and fast > slow:
            return SwingSignal(
                symbol=symbol,
                action=SignalAction.BUY,
                close=close,
                sma_fast=fast,
                sma_slow=slow,
                atr=current_atr,
                stop_price=stop_price,
                reasons=["close_above_slow", "fast_above_slow"],
            )

        return SwingSignal(
            symbol=symbol,
            action=SignalAction.HOLD,
            close=close,
            sma_fast=fast,
            sma_slow=slow,
            atr=current_atr,
            stop_price=stop_price,
            reasons=["trend_filter_not_met"],
        )
