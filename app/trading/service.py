from __future__ import annotations

from app.trading.historical_provider import HistoricalBarProvider
from app.trading.models import AccountRiskState, OrderPlan, OrderPlanStatus, SignalAction, SizedIntent
from app.trading.order_planner import OrderPlanner
from app.trading.risk_gate import RiskGate
from app.trading.swing_strategy import SwingTrendStrategy


class TradingService:
    def __init__(
        self,
        provider: HistoricalBarProvider,
        strategy: SwingTrendStrategy | None = None,
        risk_gate: RiskGate | None = None,
        order_planner: OrderPlanner | None = None,
    ) -> None:
        self._provider = provider
        self._strategy = strategy or SwingTrendStrategy()
        self._risk_gate = risk_gate or RiskGate()
        self._order_planner = order_planner or OrderPlanner()

    async def evaluate_symbol(
        self,
        symbol: str,
        account: AccountRiskState,
        current_position_qty: int,
        highest_close_since_entry: float = 0.0,
    ) -> dict:
        bars = await self._provider.get_daily_bars(symbol, lookback_days=220)
        signal = self._strategy.evaluate(
            symbol=symbol,
            bars=bars,
            current_position_qty=current_position_qty,
            highest_close_since_entry=highest_close_since_entry,
        )
        if signal.action == SignalAction.BUY:
            intent = self._risk_gate.size(signal, account)
            plan = self._order_planner.build(intent)
        elif signal.action == SignalAction.SELL and current_position_qty > 0:
            intent = SizedIntent(
                symbol=symbol,
                action=SignalAction.SELL,
                quantity=current_position_qty,
                limit_reference_price=signal.close,
                reasons=signal.reasons,
            )
            plan = self._order_planner.build(intent)
        else:
            plan = OrderPlan(
                symbol=symbol,
                action=signal.action,
                quantity=0,
                limit_price=0.0,
                status=OrderPlanStatus.BLOCKED,
                reasons=[*signal.reasons, "no_order_plan_for_signal"],
            )
        return {"signal": signal, "order_plan": plan}
