"""SQLAlchemy 数据库模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Text,
    create_engine, Index, JSON,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class AccountSnapshotRow(Base):
    __tablename__ = "account_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(32), nullable=False, index=True)
    tag = Column(String(64), nullable=False)
    value = Column(String(256), nullable=False)
    currency = Column(String(16), default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (Index("ix_acct_snap_acct_tag", "account", "tag"),)


class PositionSnapshotRow(Base):
    __tablename__ = "position_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(32), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    local_symbol = Column(String(32), default="")
    sec_type = Column(String(16), nullable=False)
    con_id = Column(Integer, default=0)
    currency = Column(String(16), default="USD")
    exchange = Column(String(32), default="")
    primary_exchange = Column(String(32), default="")
    position = Column(Float, nullable=False)
    avg_cost = Column(Float, default=0.0)
    market_price = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    daily_pnl = Column(Float, default=0.0)
    strike = Column(Float, default=0.0)
    right = Column(String(4), default="")
    expiry = Column(String(16), default="")
    multiplier = Column(Float, default=100.0)
    trading_class = Column(String(32), default="")
    delta = Column(Float, default=0.0)
    gamma = Column(Float, default=0.0)
    vega = Column(Float, default=0.0)
    theta = Column(Float, default=0.0)
    implied_vol = Column(Float, default=0.0)
    und_price = Column(Float, default=0.0)
    data_status = Column(String(16), default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class OrderSnapshotRow(Base):
    __tablename__ = "order_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(32), nullable=False, index=True)
    order_id = Column(Integer, nullable=False)
    perm_id = Column(Integer, default=0)
    symbol = Column(String(32), nullable=False)
    sec_type = Column(String(16), default="")
    action = Column(String(8), default="")
    order_type = Column(String(32), default="")
    total_qty = Column(Float, default=0.0)
    lmt_price = Column(Float, default=0.0)
    aux_price = Column(Float, default=0.0)
    stop_price = Column(Float, default=0.0)
    parent_id = Column(Integer, default=0)
    oca_group = Column(String(64), default="")
    status = Column(String(32), default="")
    filled = Column(Float, default=0.0)
    remaining = Column(Float, default=0.0)
    avg_fill_price = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class MarketDataSnapshotRow(Base):
    __tablename__ = "market_data_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    con_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    sec_type = Column(String(16), default="")
    bid = Column(Float, default=0.0)
    ask = Column(Float, default=0.0)
    last = Column(Float, default=0.0)
    close = Column(Float, default=0.0)
    volume = Column(Integer, default=0)
    implied_vol = Column(Float, default=0.0)
    delta = Column(Float, default=0.0)
    gamma = Column(Float, default=0.0)
    vega = Column(Float, default=0.0)
    theta = Column(Float, default=0.0)
    und_price = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class TradingSignalRecord(Base):
    """Trading signal audit record."""
    __tablename__ = "trading_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    symbol = Column(String(32), index=True, nullable=False)
    action = Column(String(16), nullable=False)
    close = Column(Float, default=0.0)
    sma_fast = Column(Float, default=0.0)
    sma_slow = Column(Float, default=0.0)
    atr = Column(Float, default=0.0)
    stop_price = Column(Float, default=0.0)
    reasons_json = Column(JSON, default=list)


class TradingOrderPlanRecord(Base):
    """Simulated order plan audit record."""
    __tablename__ = "trading_order_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, default=0, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    symbol = Column(String(32), index=True, nullable=False)
    action = Column(String(16), nullable=False)
    quantity = Column(Float, default=0.0)
    limit_price = Column(Float, default=0.0)
    order_type = Column(String(16), default="LMT")
    status = Column(String(16), default="SIMULATED")
    reasons_json = Column(JSON, default=list)


class ComboIdentificationRow(Base):
    __tablename__ = "combo_identifications"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(32), nullable=False, index=True)
    underlying = Column(String(32), nullable=False, index=True)
    strategy_name = Column(String(64), nullable=False)
    strategy_name_cn = Column(String(64), default="")
    legs_json = Column(Text, default="[]")
    max_profit = Column(Float, default=0.0)
    max_loss = Column(Float, default=0.0)
    breakevens_json = Column(Text, default="[]")
    net_debit_credit = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ExportLogRow(Base):
    __tablename__ = "export_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    export_type = Column(String(16), nullable=False)
    account_masked = Column(String(32), default="")
    record_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


async def init_db(database_url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
