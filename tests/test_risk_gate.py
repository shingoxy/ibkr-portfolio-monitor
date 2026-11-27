from app.config import Settings
from app.trading.models import AccountRiskState, SignalAction, SwingSignal
from app.trading.risk_gate import RiskGate


def test_default_limits_match_trading_config_defaults():
    settings = Settings()
    gate = RiskGate()

    assert gate.risk_per_trade_pct == settings.trading_risk_per_trade_pct
    assert gate.max_total_exposure_pct == settings.trading_max_total_exposure_pct
    assert gate.max_symbol_exposure_pct == settings.trading_max_symbol_exposure_pct
    assert gate.min_cash_reserve_pct == settings.trading_min_cash_reserve_pct
    assert gate.max_positions == settings.trading_max_positions


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
    account = AccountRiskState(net_liquidation=11000, available_funds=2200, gross_position_value=1000)
    intent = RiskGate(min_cash_reserve_pct=0.20).size(signal, account)
    assert intent.blocked is True
    assert "cash_reserve_limit" in intent.reasons
