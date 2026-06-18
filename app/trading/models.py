from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    BLOCK = "BLOCK"


class OrderPlanStatus(str, Enum):
    SIMULATED = "SIMULATED"
    BLOCKED = "BLOCKED"
    REVIEWED = "REVIEWED"


class Bar(BaseModel):
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @field_validator("open", "high", "low", "close")
    @classmethod
    def positive_price(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("price must be positive")
        return value


class SwingSignal(BaseModel):
    symbol: str
    action: SignalAction = SignalAction.HOLD
    close: float = 0.0
    sma_fast: float = 0.0
    sma_slow: float = 0.0
    atr: float = 0.0
    stop_price: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class AccountRiskState(BaseModel):
    net_liquidation: float
    available_funds: float
    gross_position_value: float = 0.0
    open_positions: int = 0

    @field_validator("net_liquidation", "available_funds", "gross_position_value")
    @classmethod
    def non_negative_money(cls, value: float) -> float:
        if value < 0:
            raise ValueError("money value must be non-negative")
        return value


class SizedIntent(BaseModel):
    symbol: str
    action: SignalAction
    quantity: int = 0
    limit_reference_price: float = 0.0
    stop_price: float = 0.0
    risk_dollars: float = 0.0
    notional_dollars: float = 0.0
    blocked: bool = False
    reasons: list[str] = Field(default_factory=list)


class OrderPlan(BaseModel):
    symbol: str
    action: SignalAction
    quantity: int
    limit_price: float
    order_type: str = "LMT"
    status: OrderPlanStatus = OrderPlanStatus.SIMULATED
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
