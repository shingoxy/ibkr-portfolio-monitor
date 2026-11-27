from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from math import floor, sqrt
from statistics import pstdev

from app.trading.models import AccountRiskState, Bar, SignalAction, SwingSignal
from app.trading.risk_gate import RiskGate
from app.trading.swing_strategy import SwingTrendStrategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 11_000.0
    commission_per_order: float = 1.0
    slippage_bps: float = 5.0
    risk_per_trade_pct: float = 0.003
    max_total_exposure_pct: float = 0.60
    max_symbol_exposure_pct: float = 0.20
    min_cash_reserve_pct: float = 0.25
    max_positions: int = 4


@dataclass
class BacktestTrade:
    symbol: str
    entry_date: date
    exit_date: date
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    entry_reason: list[str] = field(default_factory=list)
    exit_reason: list[str] = field(default_factory=list)


@dataclass
class _Position:
    symbol: str
    quantity: int
    entry_date: date
    entry_price: float
    entry_commission: float
    highest_close: float
    entry_reason: list[str]


@dataclass
class BacktestResult:
    start_date: date
    end_date: date
    initial_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    benchmark_total_return_pct: float
    benchmark_cagr_pct: float
    benchmark_max_drawdown_pct: float
    closed_trades: int
    win_rate_pct: float
    realized_pnl: float
    equity_curve: list[dict]
    trades: list[BacktestTrade]
    open_positions: list[dict]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["start_date"] = self.start_date.isoformat()
        payload["end_date"] = self.end_date.isoformat()
        for point in payload["equity_curve"]:
            point["date"] = point["date"].isoformat()
        for trade in payload["trades"]:
            trade["entry_date"] = trade["entry_date"].isoformat()
            trade["exit_date"] = trade["exit_date"].isoformat()
        for position in payload["open_positions"]:
            position["entry_date"] = position["entry_date"].isoformat()
        return payload


def run_backtest(
    bars_by_symbol: dict[str, list[Bar]],
    config: BacktestConfig | None = None,
    strategy: SwingTrendStrategy | None = None,
    start_date: date | None = None,
) -> BacktestResult:
    config = config or BacktestConfig()
    strategy = strategy or SwingTrendStrategy()
    normalized = {
        symbol: sorted(bars, key=lambda bar: bar.date)
        for symbol, bars in bars_by_symbol.items()
        if bars
    }
    if not normalized:
        raise ValueError("bars_by_symbol must contain at least one bar")

    bars_by_date: dict[date, dict[str, Bar]] = {}
    for symbol, bars in normalized.items():
        for bar in bars:
            bars_by_date.setdefault(bar.date, {})[symbol] = bar

    risk_gate = RiskGate(
        risk_per_trade_pct=config.risk_per_trade_pct,
        max_total_exposure_pct=config.max_total_exposure_pct,
        max_symbol_exposure_pct=config.max_symbol_exposure_pct,
        min_cash_reserve_pct=config.min_cash_reserve_pct,
        max_positions=config.max_positions,
    )
    cash = config.initial_cash
    histories: dict[str, list[Bar]] = {symbol: [] for symbol in normalized}
    positions: dict[str, _Position] = {}
    pending: dict[str, SwingSignal] = {}
    last_prices: dict[str, float] = {}
    trades: list[BacktestTrade] = []
    equity_curve: list[dict] = []

    for current_date in sorted(bars_by_date):
        day_bars = bars_by_date[current_date]
        if start_date is not None and current_date < start_date:
            for symbol, bar in day_bars.items():
                histories[symbol].append(bar)
                last_prices[symbol] = bar.close
            continue
        open_prices = {symbol: bar.open for symbol, bar in day_bars.items()}

        for symbol, signal in list(pending.items()):
            if signal.action != SignalAction.SELL or symbol not in positions or symbol not in day_bars:
                continue
            position = positions.pop(symbol)
            exit_price = _fill_price(day_bars[symbol].open, SignalAction.SELL, config.slippage_bps)
            proceeds = position.quantity * exit_price - config.commission_per_order
            cash += proceeds
            cost = position.quantity * position.entry_price + position.entry_commission
            pnl = proceeds - cost
            trades.append(
                BacktestTrade(
                    symbol=symbol,
                    entry_date=position.entry_date,
                    exit_date=current_date,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    pnl=round(pnl, 2),
                    return_pct=round((pnl / cost) * 100, 4) if cost else 0.0,
                    entry_reason=position.entry_reason,
                    exit_reason=signal.reasons,
                )
            )
            pending.pop(symbol, None)

        buy_candidates = [
            signal
            for symbol, signal in pending.items()
            if signal.action == SignalAction.BUY and symbol not in positions and symbol in day_bars
        ]
        buy_candidates.sort(key=_trend_strength, reverse=True)
        for signal in buy_candidates:
            symbol = signal.symbol
            equity, gross_exposure = _portfolio_value(cash, positions, open_prices, last_prices)
            account = AccountRiskState(
                net_liquidation=max(0.0, equity),
                available_funds=max(0.0, cash),
                gross_position_value=max(0.0, gross_exposure),
                open_positions=len(positions),
            )
            intent = risk_gate.size(signal, account)
            if intent.blocked:
                pending.pop(symbol, None)
                continue

            entry_price = _fill_price(day_bars[symbol].open, SignalAction.BUY, config.slippage_bps)
            symbol_cap = floor((equity * config.max_symbol_exposure_pct) / entry_price)
            total_room = max(0.0, equity * config.max_total_exposure_pct - gross_exposure)
            total_cap = floor(total_room / entry_price)
            reserve = equity * config.min_cash_reserve_pct
            cash_cap = floor(max(0.0, cash - reserve - config.commission_per_order) / entry_price)
            quantity = min(intent.quantity, symbol_cap, total_cap, cash_cap)
            if quantity > 0 and len(positions) < config.max_positions:
                cash -= quantity * entry_price + config.commission_per_order
                positions[symbol] = _Position(
                    symbol=symbol,
                    quantity=quantity,
                    entry_date=current_date,
                    entry_price=entry_price,
                    entry_commission=config.commission_per_order,
                    highest_close=day_bars[symbol].close,
                    entry_reason=signal.reasons,
                )
            pending.pop(symbol, None)

        for symbol, bar in day_bars.items():
            histories[symbol].append(bar)
            last_prices[symbol] = bar.close
            position = positions.get(symbol)
            if position is not None:
                position.highest_close = max(position.highest_close, bar.close)
            signal = strategy.evaluate(
                symbol,
                histories[symbol],
                current_position_qty=position.quantity if position else 0,
                highest_close_since_entry=position.highest_close if position else 0.0,
            )
            if signal.action in (SignalAction.BUY, SignalAction.SELL):
                pending[symbol] = signal

        close_prices = {symbol: bar.close for symbol, bar in day_bars.items()}
        equity, exposure = _portfolio_value(cash, positions, close_prices, last_prices)
        equity_curve.append(
            {
                "date": current_date,
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "gross_exposure": round(exposure, 2),
            }
        )

    if not equity_curve:
        raise ValueError("start_date is after the available history")
    return _build_result(config, normalized, equity_curve, trades, positions)


def _fill_price(price: float, action: SignalAction, slippage_bps: float) -> float:
    direction = 1 if action == SignalAction.BUY else -1
    return round(price * (1 + direction * slippage_bps / 10_000), 4)


def _trend_strength(signal: SwingSignal) -> float:
    if signal.sma_slow <= 0:
        return 0.0
    return signal.sma_fast / signal.sma_slow - 1


def _portfolio_value(
    cash: float,
    positions: dict[str, _Position],
    current_prices: dict[str, float],
    last_prices: dict[str, float],
) -> tuple[float, float]:
    exposure = sum(
        position.quantity * current_prices.get(symbol, last_prices.get(symbol, position.entry_price))
        for symbol, position in positions.items()
    )
    return cash + exposure, exposure


def _build_result(
    config: BacktestConfig,
    bars_by_symbol: dict[str, list[Bar]],
    equity_curve: list[dict],
    trades: list[BacktestTrade],
    positions: dict[str, _Position],
) -> BacktestResult:
    start_date = equity_curve[0]["date"]
    end_date = equity_curve[-1]["date"]
    final_equity = equity_curve[-1]["equity"]
    total_return = final_equity / config.initial_cash - 1
    elapsed_days = max(1, (end_date - start_date).days)
    cagr = (final_equity / config.initial_cash) ** (365.25 / elapsed_days) - 1

    peak = 0.0
    max_drawdown = 0.0
    returns: list[float] = []
    previous_equity = 0.0
    for point in equity_curve:
        equity = point["equity"]
        peak = max(peak, equity)
        if peak:
            max_drawdown = min(max_drawdown, equity / peak - 1)
        if previous_equity:
            returns.append(equity / previous_equity - 1)
        previous_equity = equity
    volatility = pstdev(returns) * sqrt(252) if len(returns) > 1 else 0.0
    mean_return = sum(returns) / len(returns) if returns else 0.0
    sharpe = mean_return / pstdev(returns) * sqrt(252) if len(returns) > 1 and pstdev(returns) else 0.0

    winners = sum(1 for trade in trades if trade.pnl > 0)
    spy_bars = [bar for bar in bars_by_symbol.get("SPY", []) if start_date <= bar.date <= end_date]
    benchmark_return = spy_bars[-1].close / spy_bars[0].close - 1 if len(spy_bars) > 1 else 0.0
    benchmark_cagr = (1 + benchmark_return) ** (365.25 / elapsed_days) - 1 if len(spy_bars) > 1 else 0.0
    benchmark_peak = 0.0
    benchmark_drawdown = 0.0
    for bar in spy_bars:
        benchmark_peak = max(benchmark_peak, bar.close)
        if benchmark_peak:
            benchmark_drawdown = min(benchmark_drawdown, bar.close / benchmark_peak - 1)
    open_positions = [
        {
            "symbol": position.symbol,
            "quantity": position.quantity,
            "entry_date": position.entry_date,
            "entry_price": position.entry_price,
            "highest_close": position.highest_close,
        }
        for position in sorted(positions.values(), key=lambda item: item.symbol)
    ]

    return BacktestResult(
        start_date=start_date,
        end_date=end_date,
        initial_cash=config.initial_cash,
        final_equity=round(final_equity, 2),
        total_return_pct=round(total_return * 100, 2),
        cagr_pct=round(cagr * 100, 2),
        max_drawdown_pct=round(max_drawdown * 100, 2),
        annualized_volatility_pct=round(volatility * 100, 2),
        sharpe_ratio=round(sharpe, 3),
        benchmark_total_return_pct=round(benchmark_return * 100, 2),
        benchmark_cagr_pct=round(benchmark_cagr * 100, 2),
        benchmark_max_drawdown_pct=round(benchmark_drawdown * 100, 2),
        closed_trades=len(trades),
        win_rate_pct=round(winners / len(trades) * 100, 2) if trades else 0.0,
        realized_pnl=round(sum(trade.pnl for trade in trades), 2),
        equity_curve=equity_curve,
        trades=trades,
        open_positions=open_positions,
    )
