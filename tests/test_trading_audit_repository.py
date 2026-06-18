import pytest

from app.repositories.database import Base
from app.trading.audit_repository import TradingAuditRepository
from app.trading.models import OrderPlan, SignalAction, SwingSignal


@pytest.mark.asyncio
async def test_saves_signal_and_order_plan(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'trading.db'}"
    repo = TradingAuditRepository(db_url)
    await repo.init()

    signal_id = await repo.save_signal(
        SwingSignal(symbol="SPY", action=SignalAction.BUY, close=100.0, reasons=["fast_above_slow"])
    )
    plan_id = await repo.save_order_plan(
        OrderPlan(symbol="SPY", action=SignalAction.BUY, quantity=10, limit_price=100.2),
        signal_id=signal_id,
    )
    rows = await repo.list_recent_plans(limit=5)
    await repo.close()

    assert signal_id > 0
    assert plan_id > 0
    assert rows[0]["symbol"] == "SPY"
    assert rows[0]["action"] == "BUY"
    assert rows[0]["quantity"] == 10
    assert Base.metadata.tables["trading_signals"].name == "trading_signals"
