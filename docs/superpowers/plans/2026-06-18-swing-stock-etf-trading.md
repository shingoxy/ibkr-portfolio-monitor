# Swing Stock ETF Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a conservative swing-trading signal system for stocks and ETFs that can monitor IBKR data, backtest fixed rules, generate simulated order plans, and keep the current project read-only for real orders.

**Architecture:** Add a new `app/trading/` package with pure strategy, risk, sizing, and order-plan modules that do not import `ibapi.client.EClient` and never call order placement methods. Read-only market/account data flows from existing cache and a new historical-bar adapter into a `TradingService`, which stores signals and simulated order plans for review. Real order execution is explicitly out of scope for this phase.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy/aiosqlite, pytest, existing IBKR TWS API read-only client.

---

## Scope

This plan implements Phase 1 only:

- Stocks and ETFs only.
- Low-frequency swing rules with holding periods of days to weeks.
- Signal generation, risk sizing, backtest-ready pure functions, audit persistence, API, and UI.
- Simulated limit-order plans only.
- No `placeOrder`, `cancelOrder`, `reqGlobalCancel`, or `exerciseOptions` calls.

The next plan can add Paper order execution after this phase has stable audit output and passing tests.

## File Structure

- Create `app/trading/__init__.py`: package marker.
- Create `app/trading/models.py`: Pydantic models and enums for bars, signals, account risk state, and order plans.
- Create `app/trading/indicators.py`: SMA and ATR calculations.
- Create `app/trading/swing_strategy.py`: fixed swing strategy rules.
- Create `app/trading/risk_gate.py`: sizing and exposure limits.
- Create `app/trading/order_planner.py`: simulated limit-order plan generation.
- Create `app/trading/audit_repository.py`: persistence helpers for signals and plans.
- Create `app/trading/historical_provider.py`: read-only historical-bar protocol and IBKR adapter boundary.
- Create `app/trading/service.py`: orchestration for evaluate-one-symbol and evaluate-watchlist.
- Create `app/api/routes_trading.py`: manual evaluation and signal/order-plan API.
- Create `app/templates/trading.html`: simple review page for signal and plan output.
- Modify `app/config.py`: add trading config with disabled-by-default mode.
- Modify `app/repositories/database.py`: add `TradingSignalRecord` and `TradingOrderPlanRecord`.
- Modify `app/main.py`: include trading routes.
- Test with focused files under `tests/`.

---

### Task 1: Trading Models and Config

**Files:**
- Create: `app/trading/__init__.py`
- Create: `app/trading/models.py`
- Modify: `app/config.py`
- Test: `tests/test_trading_models.py`

- [ ] **Step 1: Write the failing model tests**

Create `tests/test_trading_models.py`:

```python
from datetime import date

import pytest

from app.config import Settings
from app.trading.models import (
    AccountRiskState,
    Bar,
    OrderPlan,
    OrderPlanStatus,
    SignalAction,
    SwingSignal,
)


def test_bar_requires_positive_prices():
    with pytest.raises(ValueError):
        Bar(symbol="SPY", date=date(2026, 6, 17), open=1, high=1, low=1, close=0, volume=100)


def test_swing_signal_is_hold_by_default():
    signal = SwingSignal(symbol="SPY")
    assert signal.action == SignalAction.HOLD
    assert signal.reasons == []


def test_order_plan_defaults_to_simulated_review():
    plan = OrderPlan(symbol="SPY", action=SignalAction.BUY, quantity=3, limit_price=510.25)
    assert plan.status == OrderPlanStatus.SIMULATED
    assert plan.order_type == "LMT"


def test_account_risk_state_rejects_negative_equity():
    with pytest.raises(ValueError):
        AccountRiskState(net_liquidation=-1, available_funds=100)


def test_trading_config_defaults_to_signal_only_disabled_execution():
    settings = Settings()
    assert settings.trading_enabled is False
    assert settings.trading_mode == "signal_only"
    assert settings.trading_max_total_exposure_pct == 0.70
    assert settings.trading_max_symbol_exposure_pct == 0.25
    assert settings.trading_risk_per_trade_pct == 0.005
```

- [ ] **Step 2: Run the model tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_models.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading'`.

- [ ] **Step 3: Add trading models**

Create `app/trading/__init__.py`:

```python
"""Trading signal package.

Phase 1 is signal-only and simulated-order-plan only.
"""
```

Create `app/trading/models.py`:

```python
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
```

- [ ] **Step 4: Add disabled-by-default trading config**

Modify `app/config.py` by adding these fields inside `Settings` after `market_data_type`:

```python
    # Trading signal system, Phase 1 only creates simulated order plans.
    trading_enabled: bool = False
    trading_mode: str = "signal_only"  # signal_only, paper
    trading_watchlist: str = "SPY,QQQ,VTI,IWM,DIA"
    trading_max_positions: int = 4
    trading_max_total_exposure_pct: float = 0.70
    trading_max_symbol_exposure_pct: float = 0.25
    trading_risk_per_trade_pct: float = 0.005
    trading_min_cash_reserve_pct: float = 0.20
```

Add this validator after `validate_read_only`:

```python
    @field_validator("trading_mode")
    @classmethod
    def validate_trading_mode(cls, v: str) -> str:
        allowed = {"signal_only", "paper"}
        if v not in allowed:
            raise ValueError(f"trading_mode must be one of {sorted(allowed)}")
        return v
```

- [ ] **Step 5: Run the model tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_models.py -q
```

Expected: `5 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/trading/__init__.py app/trading/models.py tests/test_trading_models.py
git commit -m "feat: add trading signal models"
```

---

### Task 2: Indicators

**Files:**
- Create: `app/trading/indicators.py`
- Test: `tests/test_trading_indicators.py`

- [ ] **Step 1: Write failing indicator tests**

Create `tests/test_trading_indicators.py`:

```python
from datetime import date, timedelta

from app.trading.indicators import atr, sma
from app.trading.models import Bar


def _bars(closes: list[float]) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(
            symbol="SPY",
            date=start + timedelta(days=i),
            open=close - 0.5,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000,
        )
        for i, close in enumerate(closes)
    ]


def test_sma_returns_zero_until_enough_values():
    assert sma([10, 11], 3) == 0.0


def test_sma_returns_average_of_last_window():
    assert sma([10, 11, 12, 13], 3) == 12.0


def test_atr_uses_true_range_average():
    bars = _bars([10, 12, 11, 13])
    assert round(atr(bars, 3), 2) == 3.00


def test_atr_returns_zero_until_enough_bars():
    assert atr(_bars([10]), 14) == 0.0
```

- [ ] **Step 2: Run indicator tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_indicators.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading.indicators'`.

- [ ] **Step 3: Implement indicators**

Create `app/trading/indicators.py`:

```python
from __future__ import annotations

from app.trading.models import Bar


def sma(values: list[float], window: int) -> float:
    if window <= 0:
        raise ValueError("window must be positive")
    if len(values) < window:
        return 0.0
    return sum(values[-window:]) / window


def atr(bars: list[Bar], window: int = 14) -> float:
    if window <= 0:
        raise ValueError("window must be positive")
    if len(bars) < window + 1:
        return 0.0
    true_ranges: list[float] = []
    for idx in range(1, len(bars)):
        current = bars[idx]
        previous = bars[idx - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges[-window:]) / window
```

- [ ] **Step 4: Run indicator tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_indicators.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/trading/indicators.py tests/test_trading_indicators.py
git commit -m "feat: add swing trading indicators"
```

---

### Task 3: Fixed Swing Strategy

**Files:**
- Create: `app/trading/swing_strategy.py`
- Test: `tests/test_swing_strategy.py`

- [ ] **Step 1: Write failing strategy tests**

Create `tests/test_swing_strategy.py`:

```python
from datetime import date, timedelta

from app.trading.models import Bar, SignalAction
from app.trading.swing_strategy import SwingTrendStrategy


def _bars(closes: list[float]) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(
            symbol="SPY",
            date=start + timedelta(days=i),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1_000_000,
        )
        for i, close in enumerate(closes)
    ]


def test_blocks_when_history_is_too_short():
    signal = SwingTrendStrategy().evaluate("SPY", _bars([100, 101, 102]), current_position_qty=0)
    assert signal.action == SignalAction.BLOCK
    assert "insufficient_history" in signal.reasons


def test_buy_when_fast_sma_crosses_above_slow_sma_and_no_position():
    closes = [100.0] * 45 + [101.0, 102.0, 103.0, 104.0, 115.0]
    signal = SwingTrendStrategy(fast_window=5, slow_window=20).evaluate(
        "SPY", _bars(closes), current_position_qty=0
    )
    assert signal.action == SignalAction.BUY
    assert "fast_above_slow" in signal.reasons
    assert signal.stop_price > 0


def test_sell_when_close_falls_below_slow_sma_and_position_exists():
    closes = [120.0] * 45 + [118.0, 116.0, 114.0, 112.0, 90.0]
    signal = SwingTrendStrategy(fast_window=5, slow_window=20).evaluate(
        "SPY", _bars(closes), current_position_qty=10, highest_close_since_entry=125.0
    )
    assert signal.action == SignalAction.SELL
    assert "close_below_slow" in signal.reasons


def test_sell_when_trailing_stop_breaks():
    closes = [120.0] * 50
    signal = SwingTrendStrategy(fast_window=5, slow_window=20).evaluate(
        "SPY", _bars(closes), current_position_qty=10, highest_close_since_entry=140.0
    )
    assert signal.action == SignalAction.SELL
    assert "trailing_stop" in signal.reasons
```

- [ ] **Step 2: Run strategy tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_swing_strategy.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading.swing_strategy'`.

- [ ] **Step 3: Implement strategy**

Create `app/trading/swing_strategy.py`:

```python
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
```

- [ ] **Step 4: Run strategy tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_swing_strategy.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/trading/swing_strategy.py tests/test_swing_strategy.py
git commit -m "feat: add fixed swing strategy"
```

---

### Task 4: Risk Gate and Position Sizing

**Files:**
- Create: `app/trading/risk_gate.py`
- Test: `tests/test_risk_gate.py`

- [ ] **Step 1: Write failing risk tests**

Create `tests/test_risk_gate.py`:

```python
from app.trading.models import AccountRiskState, SignalAction, SwingSignal
from app.trading.risk_gate import RiskGate


def test_blocks_non_buy_signal():
    signal = SwingSignal(symbol="SPY", action=SignalAction.HOLD, close=500)
    intent = RiskGate().size(signal, AccountRiskState(net_liquidation=11000, available_funds=9000))
    assert intent.blocked is True
    assert intent.quantity == 0
    assert "not_a_buy_signal" in intent.reasons


def test_sizes_buy_by_risk_and_symbol_cap():
    signal = SwingSignal(symbol="SPY", action=SignalAction.BUY, close=100, stop_price=95)
    account = AccountRiskState(net_liquidation=11000, available_funds=9000, gross_position_value=2000)
    intent = RiskGate(risk_per_trade_pct=0.005, max_symbol_exposure_pct=0.25).size(signal, account)
    assert intent.blocked is False
    assert intent.quantity == 11
    assert intent.risk_dollars == 55.0
    assert intent.notional_dollars == 1100.0


def test_blocks_when_total_exposure_is_full():
    signal = SwingSignal(symbol="SPY", action=SignalAction.BUY, close=100, stop_price=95)
    account = AccountRiskState(net_liquidation=11000, available_funds=2000, gross_position_value=8000)
    intent = RiskGate(max_total_exposure_pct=0.70).size(signal, account)
    assert intent.blocked is True
    assert "total_exposure_limit" in intent.reasons


def test_blocks_when_cash_reserve_would_be_breached():
    signal = SwingSignal(symbol="SPY", action=SignalAction.BUY, close=100, stop_price=99)
    account = AccountRiskState(net_liquidation=11000, available_funds=2500, gross_position_value=1000)
    intent = RiskGate(min_cash_reserve_pct=0.20).size(signal, account)
    assert intent.blocked is True
    assert "cash_reserve_limit" in intent.reasons
```

- [ ] **Step 2: Run risk tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_risk_gate.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading.risk_gate'`.

- [ ] **Step 3: Implement risk gate**

Create `app/trading/risk_gate.py`:

```python
from __future__ import annotations

from math import floor

from app.trading.models import AccountRiskState, SignalAction, SizedIntent, SwingSignal


class RiskGate:
    def __init__(
        self,
        risk_per_trade_pct: float = 0.005,
        max_total_exposure_pct: float = 0.70,
        max_symbol_exposure_pct: float = 0.25,
        min_cash_reserve_pct: float = 0.20,
        max_positions: int = 4,
    ) -> None:
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_symbol_exposure_pct = max_symbol_exposure_pct
        self.min_cash_reserve_pct = min_cash_reserve_pct
        self.max_positions = max_positions

    def size(self, signal: SwingSignal, account: AccountRiskState) -> SizedIntent:
        if signal.action != SignalAction.BUY:
            return SizedIntent(
                symbol=signal.symbol,
                action=signal.action,
                blocked=True,
                reasons=["not_a_buy_signal"],
            )

        if account.open_positions >= self.max_positions:
            return SizedIntent(symbol=signal.symbol, action=signal.action, blocked=True, reasons=["max_positions"])

        price = signal.close
        stop_distance = price - signal.stop_price
        if price <= 0 or stop_distance <= 0:
            return SizedIntent(symbol=signal.symbol, action=signal.action, blocked=True, reasons=["invalid_price_or_stop"])

        max_total_exposure = account.net_liquidation * self.max_total_exposure_pct
        remaining_total_exposure = max_total_exposure - account.gross_position_value
        if remaining_total_exposure <= 0:
            return SizedIntent(
                symbol=signal.symbol,
                action=signal.action,
                blocked=True,
                reasons=["total_exposure_limit"],
            )

        minimum_cash_reserve = account.net_liquidation * self.min_cash_reserve_pct
        spendable_cash = account.available_funds - minimum_cash_reserve
        if spendable_cash <= 0:
            return SizedIntent(
                symbol=signal.symbol,
                action=signal.action,
                blocked=True,
                reasons=["cash_reserve_limit"],
            )

        risk_dollars = round(account.net_liquidation * self.risk_per_trade_pct, 2)
        shares_by_risk = floor(risk_dollars / stop_distance)
        shares_by_symbol_cap = floor((account.net_liquidation * self.max_symbol_exposure_pct) / price)
        shares_by_total_cap = floor(remaining_total_exposure / price)
        shares_by_cash = floor(spendable_cash / price)
        quantity = min(shares_by_risk, shares_by_symbol_cap, shares_by_total_cap, shares_by_cash)

        if quantity <= 0:
            return SizedIntent(
                symbol=signal.symbol,
                action=signal.action,
                blocked=True,
                reasons=["quantity_too_small"],
            )

        return SizedIntent(
            symbol=signal.symbol,
            action=signal.action,
            quantity=quantity,
            limit_reference_price=price,
            stop_price=signal.stop_price,
            risk_dollars=risk_dollars,
            notional_dollars=round(quantity * price, 2),
            reasons=["risk_sized"],
        )
```

- [ ] **Step 4: Run risk tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_risk_gate.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/trading/risk_gate.py tests/test_risk_gate.py
git commit -m "feat: add conservative trading risk gate"
```

---

### Task 5: Simulated Order Planner

**Files:**
- Create: `app/trading/order_planner.py`
- Test: `tests/test_order_planner.py`

- [ ] **Step 1: Write failing order planner tests**

Create `tests/test_order_planner.py`:

```python
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
```

- [ ] **Step 2: Run order planner tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_order_planner.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading.order_planner'`.

- [ ] **Step 3: Implement order planner**

Create `app/trading/order_planner.py`:

```python
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
```

- [ ] **Step 4: Run order planner tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_order_planner.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/trading/order_planner.py tests/test_order_planner.py
git commit -m "feat: add simulated order planner"
```

---

### Task 6: Audit Persistence

**Files:**
- Modify: `app/repositories/database.py`
- Create: `app/trading/audit_repository.py`
- Test: `tests/test_trading_audit_repository.py`

- [ ] **Step 1: Write failing persistence test**

Create `tests/test_trading_audit_repository.py`:

```python
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
```

- [ ] **Step 2: Run persistence test to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_audit_repository.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading.audit_repository'`.

- [ ] **Step 3: Add database tables**

Modify `app/repositories/database.py` by adding these ORM models after `MarketDataSnapshot`:

```python
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
```

- [ ] **Step 4: Implement audit repository**

Create `app/trading/audit_repository.py`:

```python
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
```

- [ ] **Step 5: Run persistence test to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_audit_repository.py -q
```

Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/repositories/database.py app/trading/audit_repository.py tests/test_trading_audit_repository.py
git commit -m "feat: persist trading signal audit records"
```

---

### Task 7: Historical Bar Provider Boundary

**Files:**
- Create: `app/trading/historical_provider.py`
- Test: `tests/test_historical_provider.py`

- [ ] **Step 1: Write failing provider tests**

Create `tests/test_historical_provider.py`:

```python
from datetime import date

import pytest

from app.trading.historical_provider import InMemoryHistoricalBarProvider
from app.trading.models import Bar


@pytest.mark.asyncio
async def test_in_memory_provider_returns_symbol_bars_sorted():
    provider = InMemoryHistoricalBarProvider(
        {
            "SPY": [
                Bar(symbol="SPY", date=date(2026, 1, 2), open=101, high=102, low=100, close=101, volume=1),
                Bar(symbol="SPY", date=date(2026, 1, 1), open=100, high=101, low=99, close=100, volume=1),
            ]
        }
    )
    bars = await provider.get_daily_bars("SPY", lookback_days=200)
    assert [bar.date.isoformat() for bar in bars] == ["2026-01-01", "2026-01-02"]


@pytest.mark.asyncio
async def test_in_memory_provider_returns_empty_for_missing_symbol():
    provider = InMemoryHistoricalBarProvider({})
    assert await provider.get_daily_bars("QQQ", lookback_days=200) == []
```

- [ ] **Step 2: Run provider tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_historical_provider.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading.historical_provider'`.

- [ ] **Step 3: Implement provider protocol and test provider**

Create `app/trading/historical_provider.py`:

```python
from __future__ import annotations

from typing import Protocol

from app.trading.models import Bar


class HistoricalBarProvider(Protocol):
    async def get_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        """Return ascending daily bars for one symbol."""


class InMemoryHistoricalBarProvider:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars_by_symbol = bars_by_symbol

    async def get_daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        bars = sorted(self._bars_by_symbol.get(symbol, []), key=lambda bar: bar.date)
        return bars[-lookback_days:]
```

- [ ] **Step 4: Run provider tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_historical_provider.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/trading/historical_provider.py tests/test_historical_provider.py
git commit -m "feat: add historical bar provider boundary"
```

---

### Task 8: Trading Service

**Files:**
- Create: `app/trading/service.py`
- Test: `tests/test_trading_service.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/test_trading_service.py`:

```python
from datetime import date, timedelta

import pytest

from app.trading.historical_provider import InMemoryHistoricalBarProvider
from app.trading.models import AccountRiskState, Bar
from app.trading.service import TradingService


def _bars(symbol: str, closes: list[float]) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(
            symbol=symbol,
            date=start + timedelta(days=i),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1_000_000,
        )
        for i, close in enumerate(closes)
    ]


@pytest.mark.asyncio
async def test_evaluate_symbol_returns_signal_and_plan_for_buy():
    provider = InMemoryHistoricalBarProvider({"SPY": _bars("SPY", [100.0] * 45 + [101, 102, 103, 104, 115])})
    service = TradingService(provider=provider)
    result = await service.evaluate_symbol(
        symbol="SPY",
        account=AccountRiskState(net_liquidation=11000, available_funds=9000, gross_position_value=0),
        current_position_qty=0,
    )
    assert result["signal"].action.value == "BUY"
    assert result["order_plan"].symbol == "SPY"
    assert result["order_plan"].quantity > 0


@pytest.mark.asyncio
async def test_evaluate_symbol_blocks_when_no_history():
    provider = InMemoryHistoricalBarProvider({})
    service = TradingService(provider=provider)
    result = await service.evaluate_symbol(
        symbol="QQQ",
        account=AccountRiskState(net_liquidation=11000, available_funds=9000),
        current_position_qty=0,
    )
    assert result["signal"].action.value == "BLOCK"
    assert result["order_plan"].status.value == "BLOCKED"
```

- [ ] **Step 2: Run service tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_service.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.trading.service'`.

- [ ] **Step 3: Implement service**

Create `app/trading/service.py`:

```python
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
```

- [ ] **Step 4: Run service tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_service.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/trading/service.py tests/test_trading_service.py
git commit -m "feat: add trading signal service"
```

---

### Task 9: API and Review Page

**Files:**
- Create: `app/api/routes_trading.py`
- Create: `app/templates/trading.html`
- Modify: `app/main.py`
- Test: `tests/test_trading_routes.py`

- [ ] **Step 1: Write failing route tests**

Create `tests/test_trading_routes.py`:

```python
from app.api.routes_trading import _parse_watchlist


def test_parse_watchlist_trims_and_uppercases_symbols():
    assert _parse_watchlist(" spy, qqq,VTI ") == ["SPY", "QQQ", "VTI"]


def test_parse_watchlist_drops_empty_values():
    assert _parse_watchlist("SPY,,QQQ, ") == ["SPY", "QQQ"]
```

- [ ] **Step 2: Run route tests to verify failure**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_routes.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.routes_trading'`.

- [ ] **Step 3: Implement routes**

Create `app/api/routes_trading.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings

router = APIRouter()


def _parse_watchlist(raw: str) -> list[str]:
    return [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()]


@router.get("/trading", response_class=HTMLResponse)
async def trading_page(request: Request):
    return request.app.state.templates.TemplateResponse(request, "trading.html")


@router.get("/api/trading/config")
async def api_trading_config():
    settings = get_settings()
    return {
        "enabled": settings.trading_enabled,
        "mode": settings.trading_mode,
        "watchlist": _parse_watchlist(settings.trading_watchlist),
        "max_positions": settings.trading_max_positions,
        "max_total_exposure_pct": settings.trading_max_total_exposure_pct,
        "max_symbol_exposure_pct": settings.trading_max_symbol_exposure_pct,
        "risk_per_trade_pct": settings.trading_risk_per_trade_pct,
        "min_cash_reserve_pct": settings.trading_min_cash_reserve_pct,
    }
```

Modify `app/main.py` route import and include block:

```python
    from app.api import routes_dashboard, routes_positions, routes_strategies, routes_orders, routes_export, routes_trading
    app.include_router(routes_dashboard.router)
    app.include_router(routes_positions.router)
    app.include_router(routes_strategies.router)
    app.include_router(routes_orders.router)
    app.include_router(routes_export.router)
    app.include_router(routes_trading.router)
```

- [ ] **Step 4: Add review page**

Create `app/templates/trading.html`:

```html
{% extends "base.html" %}
{% block title %}Trading Signals{% endblock %}
{% block content %}
<div class="container-fluid">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h1 class="h3">Trading Signals</h1>
    <span class="badge bg-secondary" id="trading-mode">signal_only</span>
  </div>
  <div class="alert alert-warning">
    Phase 1 only generates simulated order plans. Real order execution is disabled.
  </div>
  <div class="card">
    <div class="card-body">
      <h2 class="h5">Configuration</h2>
      <pre id="trading-config" class="mb-0"></pre>
    </div>
  </div>
</div>
<script>
async function loadTradingConfig() {
  const res = await fetch('/api/trading/config');
  const data = await res.json();
  document.getElementById('trading-mode').textContent = data.mode;
  document.getElementById('trading-config').textContent = JSON.stringify(data, null, 2);
}
loadTradingConfig();
</script>
{% endblock %}
```

- [ ] **Step 5: Run route tests to verify pass**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_routes.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Run read-only guard tests**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_read_only_guards.py -q
```

Expected: PASS. This confirms no forbidden trading API calls were introduced.

- [ ] **Step 7: Commit**

```bash
git add app/api/routes_trading.py app/main.py app/templates/trading.html tests/test_trading_routes.py
git commit -m "feat: add trading signal review page"
```

---

### Task 10: Full Verification

**Files:**
- No code files unless previous tasks require fixing a failing test.

- [ ] **Step 1: Run focused trading tests**

Run:

```bash
conda run -n mlx312 python -m pytest tests/test_trading_models.py tests/test_trading_indicators.py tests/test_swing_strategy.py tests/test_risk_gate.py tests/test_order_planner.py tests/test_trading_audit_repository.py tests/test_historical_provider.py tests/test_trading_service.py tests/test_trading_routes.py -q
```

Expected: all listed tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
conda run -n mlx312 python -m pytest tests/ -q
```

Expected: existing 46 tests plus new trading tests pass.

- [ ] **Step 3: Start app locally**

Run:

```bash
APP_PORT=8766 conda run -n mlx312 python -m uvicorn app.main:app --host 127.0.0.1 --port 8766
```

Expected:

```text
IBKR 持仓分析工具启动
模式: 只读
```

- [ ] **Step 4: Check trading config API**

Run:

```bash
curl -s --max-time 5 http://127.0.0.1:8766/api/trading/config
```

Expected JSON includes:

```json
{
  "enabled": false,
  "mode": "signal_only",
  "watchlist": ["SPY", "QQQ", "VTI", "IWM", "DIA"]
}
```

- [ ] **Step 5: Confirm no execution methods exist**

Run:

```bash
rg -n "placeOrder|cancelOrder|reqGlobalCancel|exerciseOptions" app tests
```

Expected: matches only comments, guard constants, or tests that explicitly scan for forbidden methods. No production trading module may call or reference those methods.

- [ ] **Step 6: Commit verification fixes if any were needed**

Only run this if a previous verification step required code edits:

```bash
git add app tests
git commit -m "test: verify trading signal phase"
```

---

## Self-Review

- Spec coverage: The plan covers stocks/ETFs, low-frequency swing rules, 11000 USD-style conservative sizing, signal-only defaults, simulated order plans, audit records, API visibility, and read-only verification.
- Placeholder scan: No incomplete instruction or fill-in marker remains.
- Type consistency: `SignalAction`, `SwingSignal`, `AccountRiskState`, `SizedIntent`, and `OrderPlan` names are consistent across tests and implementations.
- Scope check: IBKR real order execution is intentionally excluded. A later Paper execution plan can add a single isolated gateway after this phase proves signal quality and auditability.
