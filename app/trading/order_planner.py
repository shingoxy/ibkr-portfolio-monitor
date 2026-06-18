from __future__ import annotations

from app.trading.models import OrderPlan, OrderPlanStatus, SignalAction, SizedIntent


class OrderPlanner:
    def __init__(self, limit_buffer_pct: float = 0.002) -> None:
        self.limit_buffer_pct = limit_buffer_pct

    def build(self, intent: SizedIntent) -> OrderPlan:
        if intent.quantity <= 0:
            return OrderPlan(
                symbol=intent.symbol,
                action=intent.action,
                quantity=0,
                limit_price=0.0,
                status=OrderPlanStatus.BLOCKED,
                reasons=[*intent.reasons, "zero_quantity"],
            )

        if intent.action == SignalAction.BUY:
            limit_price = round(intent.limit_reference_price * (1 + self.limit_buffer_pct), 2)
            reason = "buy_limit_buffer_0.20pct"
        elif intent.action == SignalAction.SELL:
            limit_price = round(intent.limit_reference_price * (1 - self.limit_buffer_pct), 2)
            reason = "sell_limit_buffer_0.20pct"
        else:
            return OrderPlan(
                symbol=intent.symbol,
                action=intent.action,
                quantity=0,
                limit_price=0.0,
                status=OrderPlanStatus.BLOCKED,
                reasons=[*intent.reasons, "unsupported_action"],
            )

        return OrderPlan(
            symbol=intent.symbol,
            action=intent.action,
            quantity=intent.quantity,
            limit_price=limit_price,
            reasons=[*intent.reasons, reason],
        )
