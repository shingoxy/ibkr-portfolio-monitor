from app.trading.models import SignalAction, SizedIntent
from app.trading.order_planner import OrderPlanner


def test_builds_buy_limit_plan_with_small_price_buffer():
    intent = SizedIntent(
        symbol="SPY",
        action=SignalAction.BUY,
        quantity=10,
        limit_reference_price=100.0,
        reasons=["risk_sized"],
    )
    plan = OrderPlanner().build(intent)
    assert plan.symbol == "SPY"
    assert plan.action == SignalAction.BUY
    assert plan.quantity == 10
    assert plan.limit_price == 100.2
    assert "buy_limit_buffer_0.20pct" in plan.reasons


def test_builds_sell_limit_plan_with_small_price_buffer():
    intent = SizedIntent(
        symbol="SPY",
        action=SignalAction.SELL,
        quantity=10,
        limit_reference_price=100.0,
        reasons=["exit_signal"],
    )
    plan = OrderPlanner().build(intent)
    assert plan.limit_price == 99.8
    assert "sell_limit_buffer_0.20pct" in plan.reasons


def test_blocks_zero_quantity():
    intent = SizedIntent(symbol="SPY", action=SignalAction.BUY, quantity=0, limit_reference_price=100.0)
    plan = OrderPlanner().build(intent)
    assert plan.quantity == 0
    assert plan.status.value == "BLOCKED"
    assert "zero_quantity" in plan.reasons


def test_blocks_when_intent_is_blocked_even_with_quantity():
    intent = SizedIntent(
        symbol="SPY",
        action=SignalAction.BUY,
        quantity=10,
        limit_reference_price=100.0,
        blocked=True,
        reasons=["cash_reserve_limit"],
    )
    plan = OrderPlanner().build(intent)
    assert plan.quantity == 0
    assert plan.status.value == "BLOCKED"
    assert "cash_reserve_limit" in plan.reasons
    assert "intent_blocked" in plan.reasons
