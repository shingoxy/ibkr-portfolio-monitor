"""快照仓储：异步 CRUD 操作。"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select, delete

from app.repositories.database import (
    Base, AccountSnapshotRow, PositionSnapshotRow, OrderSnapshotRow,
    MarketDataSnapshotRow, ComboIdentificationRow, ExportLogRow,
)

logger = logging.getLogger(__name__)


class SnapshotRepository:
    """异步快照存储。"""

    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(database_url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库初始化完成")

    async def close(self) -> None:
        await self._engine.dispose()

    async def save_account_snapshot(self, account: str, items: list[dict]) -> None:
        async with self._session_factory() as session:
            for item in items:
                session.add(AccountSnapshotRow(
                    account=account, tag=item["tag"],
                    value=item["value"], currency=item.get("currency", ""),
                    created_at=datetime.utcnow(),
                ))
            await session.commit()

    async def save_position_snapshots(self, positions: list[dict]) -> None:
        async with self._session_factory() as session:
            for p in positions:
                session.add(PositionSnapshotRow(
                    account=p.get("account", ""),
                    symbol=p.get("symbol", ""),
                    local_symbol=p.get("local_symbol", ""),
                    sec_type=p.get("sec_type", ""),
                    con_id=p.get("con_id", 0),
                    currency=p.get("currency", "USD"),
                    exchange=p.get("exchange", ""),
                    primary_exchange=p.get("primary_exchange", ""),
                    position=p.get("position", 0),
                    avg_cost=p.get("avg_cost", 0),
                    market_price=p.get("market_price", 0),
                    market_value=p.get("market_value", 0),
                    unrealized_pnl=p.get("unrealized_pnl", 0),
                    realized_pnl=p.get("realized_pnl", 0),
                    daily_pnl=p.get("daily_pnl", 0),
                    strike=p.get("strike", 0),
                    right=p.get("right", ""),
                    expiry=p.get("expiry", ""),
                    multiplier=p.get("multiplier", 100),
                    trading_class=p.get("trading_class", ""),
                    delta=p.get("delta", 0),
                    gamma=p.get("gamma", 0),
                    vega=p.get("vega", 0),
                    theta=p.get("theta", 0),
                    implied_vol=p.get("implied_vol", 0),
                    und_price=p.get("und_price", 0),
                    data_status=p.get("data_status", ""),
                    created_at=datetime.utcnow(),
                ))
            await session.commit()

    async def save_order_snapshots(self, orders: list[dict]) -> None:
        async with self._session_factory() as session:
            for o in orders:
                session.add(OrderSnapshotRow(
                    account=o.get("account", ""),
                    order_id=o.get("order_id", 0),
                    perm_id=o.get("perm_id", 0),
                    symbol=o.get("symbol", ""),
                    sec_type=o.get("sec_type", ""),
                    action=o.get("action", ""),
                    order_type=o.get("order_type", ""),
                    total_qty=o.get("total_qty", 0),
                    lmt_price=o.get("lmt_price", 0),
                    aux_price=o.get("aux_price", 0),
                    stop_price=o.get("stop_price", 0),
                    parent_id=o.get("parent_id", 0),
                    oca_group=o.get("oca_group", ""),
                    status=o.get("status", ""),
                    filled=o.get("filled", 0),
                    remaining=o.get("remaining", 0),
                    avg_fill_price=o.get("avg_fill_price", 0),
                    created_at=datetime.utcnow(),
                ))
            await session.commit()

    async def save_market_data_batch(self, items: list[dict]) -> None:
        async with self._session_factory() as session:
            for item in items:
                session.add(MarketDataSnapshotRow(
                    con_id=item.get("con_id", 0),
                    symbol=item.get("symbol", ""),
                    sec_type=item.get("sec_type", ""),
                    bid=item.get("bid", 0),
                    ask=item.get("ask", 0),
                    last=item.get("last", 0),
                    close=item.get("close", 0),
                    volume=item.get("volume", 0),
                    implied_vol=item.get("implied_vol", 0),
                    delta=item.get("delta", 0),
                    gamma=item.get("gamma", 0),
                    vega=item.get("vega", 0),
                    theta=item.get("theta", 0),
                    und_price=item.get("und_price", 0),
                    created_at=datetime.utcnow(),
                ))
            await session.commit()

    async def save_combo(self, combo_data: dict, account: str) -> int:
        async with self._session_factory() as session:
            row = ComboIdentificationRow(
                account=account,
                underlying=combo_data.get("underlying", ""),
                strategy_name=combo_data.get("strategy_name", ""),
                strategy_name_cn=combo_data.get("strategy_name_cn", ""),
                legs_json=json.dumps(combo_data.get("legs", []), ensure_ascii=False),
                max_profit=combo_data.get("max_profit", 0),
                max_loss=combo_data.get("max_loss", 0),
                breakevens_json=json.dumps(combo_data.get("breakevens", [])),
                net_debit_credit=combo_data.get("net_debit_credit", 0),
                created_at=datetime.utcnow(),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    async def log_export(self, export_type: str, account_masked: str, record_count: int) -> None:
        async with self._session_factory() as session:
            session.add(ExportLogRow(
                export_type=export_type, account_masked=account_masked,
                record_count=record_count, created_at=datetime.utcnow(),
            ))
            await session.commit()

    async def cleanup_old_snapshots(self, keep_hours: int = 72) -> None:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=keep_hours)
        async with self._session_factory() as session:
            for model in [AccountSnapshotRow, PositionSnapshotRow, OrderSnapshotRow,
                          MarketDataSnapshotRow, ComboIdentificationRow]:
                await session.execute(delete(model).where(model.created_at < cutoff))
            await session.commit()
        logger.info("已清理 %d 小时前的旧快照", keep_hours)
