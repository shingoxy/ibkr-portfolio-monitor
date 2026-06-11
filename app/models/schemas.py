"""Pydantic v2 数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── 账户 ──────────────────────────────────────────────────────────────

class AccountSummaryItem(BaseModel):
    account: str = ""
    tag: str = ""
    value: str = ""
    currency: str = ""


class AccountSnapshot(BaseModel):
    """账户快照，包含完整字段。"""
    account_masked: str = ""
    base_currency: str = "USD"
    account_type: str = ""  # INDIVIDUAL, etc.

    net_liquidation: float = 0.0
    total_cash: float = 0.0
    settled_cash: float = 0.0
    accrued_cash: float = 0.0
    buying_power: float = 0.0
    equity_with_loan: float = 0.0
    gross_position_value: float = 0.0

    available_funds: float = 0.0
    excess_liquidity: float = 0.0
    full_available_funds: float = 0.0
    full_excess_liquidity: float = 0.0
    init_margin_req: float = 0.0
    maint_margin_req: float = 0.0
    full_init_margin_req: float = 0.0
    full_maint_margin_req: float = 0.0

    look_ahead_available_funds: float = 0.0
    look_ahead_excess_liquidity: float = 0.0

    cushion: float = 0.0
    day_trades_remaining: int = -1
    leverage: float = 0.0

    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0

    updated_at: Optional[datetime] = None


# ── 持仓 ──────────────────────────────────────────────────────────────

class PositionItem(BaseModel):
    """单个持仓，扩展字段。"""
    account: str = ""
    symbol: str = ""
    local_symbol: str = ""
    sec_type: str = ""
    con_id: int = 0
    currency: str = "USD"
    exchange: str = ""
    primary_exchange: str = ""
    position: float = 0.0
    avg_cost: float = 0.0  # IBKR 返回的每股/每单位成本
    market_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    # 期权字段
    strike: float = 0.0
    right: str = ""  # C, P
    expiry: str = ""  # YYYYMMDD
    multiplier: float = 100.0
    trading_class: str = ""
    # Greeks
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    implied_vol: float = 0.0
    und_price: float = 0.0
    # 数据状态
    data_status: str = ""  # realtime, delayed, frozen, unknown
    last_update_time: Optional[datetime] = None


class PositionSummary(BaseModel):
    total_market_value: float = 0.0
    total_unrealized_pnl: float = 0.0
    total_realized_pnl: float = 0.0
    total_daily_pnl: float = 0.0
    total_cost_basis: float = 0.0
    stock_count: int = 0
    option_count: int = 0
    positions: list[PositionItem] = []


# ── 订单 ──────────────────────────────────────────────────────────────

class OrderItem(BaseModel):
    account: str = ""
    order_id: int = 0
    perm_id: int = 0
    symbol: str = ""
    sec_type: str = ""
    action: str = ""  # BUY, SELL
    order_type: str = ""
    total_qty: float = 0.0
    lmt_price: float = 0.0
    aux_price: float = 0.0
    stop_price: float = 0.0
    parent_id: int = 0
    oca_group: str = ""
    status: str = ""
    filled: float = 0.0
    remaining: float = 0.0
    avg_fill_price: float = 0.0
    completed_time: str = ""


# ── 期权 ──────────────────────────────────────────────────────────────

class OptionGreeks(BaseModel):
    con_id: int = 0
    symbol: str = ""
    implied_vol: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0
    und_price: float = 0.0


class OptionLeg(BaseModel):
    con_id: int = 0
    symbol: str = ""
    sec_type: str = "OPT"
    strike: float = 0.0
    right: str = ""
    expiry: str = ""
    position: float = 0.0  # 正=多头, 负=空头
    avg_cost: float = 0.0  # IBKR 每股成本
    market_price: float = 0.0
    multiplier: float = 100.0
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0
    implied_vol: float = 0.0
    und_price: float = 0.0


class OptionCombo(BaseModel):
    id: int = 0
    underlying: str = ""
    strategy_name: str = ""
    strategy_name_cn: str = ""
    legs: list[OptionLeg] = []
    max_profit: float = 0.0
    max_loss: float = 0.0
    breakevens: list[float] = []
    net_debit_credit: float = 0.0  # 正=净支出，负=净收入
    total_delta: float = 0.0
    total_gamma: float = 0.0
    total_vega: float = 0.0
    total_theta: float = 0.0
    days_to_expiry: int = 0
    risk_warnings: list[str] = []


class PayoffCurve(BaseModel):
    combo_id: int = 0
    strategy_name: str = ""
    underlying: str = ""
    current_price: float = 0.0
    prices: list[float] = []
    pnl: list[float] = []
    max_profit: float = 0.0
    max_loss: float = 0.0
    breakevens: list[float] = []


class ScenarioResult(BaseModel):
    combo_id: int = 0
    strategy_name: str = ""
    underlying: str = ""
    price_steps: list[float] = []
    date_steps: list[str] = []
    matrix: list[list[float]] = []


# ── 快照 ──────────────────────────────────────────────────────────────

class SnapshotMeta(BaseModel):
    snapshot_id: str = ""
    requested_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    connection_status: str = ""
    account_data_complete: bool = False
    positions_complete: bool = False
    market_data_complete: bool = False
    orders_complete: bool = False
    errors: list[str] = []
    stale_fields: list[str] = []


# ── 导出 ──────────────────────────────────────────────────────────────

class ExportResult(BaseModel):
    format: str = ""
    content: str = ""
    record_count: int = 0
    exported_at: Optional[datetime] = None
