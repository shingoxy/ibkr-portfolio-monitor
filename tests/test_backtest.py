from datetime import date, timedelta

from app.trading.backtest import BacktestConfig, run_backtest
from app.trading.models import Bar
from app.trading.swing_strategy import SwingTrendStrategy


def _bars(symbol: str, closes: list[float], opens: list[float] | None = None) -> list[Bar]:
    start = date(2026, 1, 1)
    opens = opens or closes
    return [
        Bar(
            symbol=symbol,
            date=start + timedelta(days=index),
            open=opens[index],
            high=max(opens[index], close) + 1,
            low=min(opens[index], close) - 1,
            close=close,
            volume=1_000_000,
        )
        for index, close in enumerate(closes)
    ]


def _config(**overrides) -> BacktestConfig:
    values = {
        "initial_cash": 11_000,
        "commission_per_order": 0,
        "slippage_bps": 0,
        "risk_per_trade_pct": 0.10,
        "max_total_exposure_pct": 1.0,
        "max_symbol_exposure_pct": 1.0,
        "min_cash_reserve_pct": 0.0,
        "max_positions": 4,
    }
    values.update(overrides)
    return BacktestConfig(**values)


def test_signal_executes_at_next_bar_open_without_lookahead():
    closes = [100, 100, 110, 90, 80]
    opens = [100, 100, 100, 120, 80]
    result = run_backtest(
        {"SPY": _bars("SPY", closes, opens)},
        config=_config(),
        strategy=SwingTrendStrategy(fast_window=2, slow_window=3, atr_window=2),
    )

    assert result.closed_trades == 1
    assert result.trades[0].entry_date == date(2026, 1, 4)
    assert result.trades[0].entry_price == 120
    assert result.trades[0].exit_date == date(2026, 1, 5)
    assert result.trades[0].exit_price == 80


def test_history_before_start_date_is_used_only_for_indicator_warmup():
    result = run_backtest(
        {"SPY": _bars("SPY", [100, 100, 110, 112])},
        config=_config(),
        strategy=SwingTrendStrategy(fast_window=2, slow_window=3, atr_window=2),
        start_date=date(2026, 1, 3),
    )

    assert result.start_date == date(2026, 1, 3)
    assert result.open_positions[0]["entry_date"] == date(2026, 1, 4)


def test_max_positions_keeps_strongest_trend_candidate():
    result = run_backtest(
        {
            "AAA": _bars("AAA", [100, 100, 110, 112]),
            "BBB": _bars("BBB", [100, 100, 130, 132]),
        },
        config=_config(max_positions=1),
        strategy=SwingTrendStrategy(fast_window=2, slow_window=3, atr_window=2),
    )

    assert [position["symbol"] for position in result.open_positions] == ["BBB"]


def test_commission_and_slippage_reduce_final_equity():
    bars = {"SPY": _bars("SPY", [100, 100, 110, 90, 80], [100, 100, 100, 120, 80])}
    strategy = SwingTrendStrategy(fast_window=2, slow_window=3, atr_window=2)

    without_costs = run_backtest(bars, config=_config(), strategy=strategy)
    with_costs = run_backtest(
        bars,
        config=_config(commission_per_order=1, slippage_bps=10),
        strategy=strategy,
    )

    assert with_costs.final_equity < without_costs.final_equity
