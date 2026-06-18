from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories.database import Base, TradingOrderPlanRecord, TradingSignalRecord
from app.trading.models import OrderPlan, SwingSignal


class TradingAuditRepository:
    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(database_url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def save_signal(self, signal: SwingSignal) -> int:
        async with self._session_factory() as session:
            record = TradingSignalRecord(
                symbol=signal.symbol,
                action=signal.action.value,
                close=signal.close,
                sma_fast=signal.sma_fast,
                sma_slow=signal.sma_slow,
                atr=signal.atr,
                stop_price=signal.stop_price,
                reasons_json=signal.reasons,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return int(record.id)

    async def save_order_plan(self, plan: OrderPlan, signal_id: int) -> int:
        async with self._session_factory() as session:
            record = TradingOrderPlanRecord(
                signal_id=signal_id,
                symbol=plan.symbol,
                action=plan.action.value,
                quantity=plan.quantity,
                limit_price=plan.limit_price,
                order_type=plan.order_type,
                status=plan.status.value,
                reasons_json=plan.reasons,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return int(record.id)

    async def list_recent_plans(self, limit: int = 20) -> list[dict]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TradingOrderPlanRecord)
                .order_by(TradingOrderPlanRecord.created_at.desc())
                .limit(limit)
            )
            records = result.scalars().all()
            return [
                {
                    "id": record.id,
                    "signal_id": record.signal_id,
                    "symbol": record.symbol,
                    "action": record.action,
                    "quantity": record.quantity,
                    "limit_price": record.limit_price,
                    "order_type": record.order_type,
                    "status": record.status,
                    "reasons": record.reasons_json or [],
                    "created_at": record.created_at.isoformat() if record.created_at else "",
                }
                for record in records
            ]
