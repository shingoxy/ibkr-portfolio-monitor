from __future__ import annotations

from math import floor

from app.trading.models import AccountRiskState, SignalAction, SizedIntent, SwingSignal


class RiskGate:
    def __init__(
        self,
        risk_per_trade_pct: float = 0.003,
        max_total_exposure_pct: float = 0.60,
        max_symbol_exposure_pct: float = 0.20,
        min_cash_reserve_pct: float = 0.25,
        max_positions: int = 4,
    ) -> None:
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_symbol_exposure_pct = max_symbol_exposure_pct
        self.min_cash_reserve_pct = min_cash_reserve_pct
        self.max_positions = max_positions

    @classmethod
    def from_settings(cls, settings) -> "RiskGate":
        return cls(
            risk_per_trade_pct=settings.trading_risk_per_trade_pct,
            max_total_exposure_pct=settings.trading_max_total_exposure_pct,
            max_symbol_exposure_pct=settings.trading_max_symbol_exposure_pct,
            min_cash_reserve_pct=settings.trading_min_cash_reserve_pct,
            max_positions=settings.trading_max_positions,
        )

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
