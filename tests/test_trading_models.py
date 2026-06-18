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
