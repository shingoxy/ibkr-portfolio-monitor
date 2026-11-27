from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date, timedelta
from math import floor, sqrt
from pathlib import Path
from statistics import stdev

import pytest

from app.trading.models import Bar
from app.trading.pead_backtest import (
    PEADBacktestConfig,
    PEADBacktestResult,
    PEADKillCriteriaResult,
    PEADPeriodMetrics,
    evaluate_pead_kill_criteria,
    run_pead_backtest,
    run_pead_slippage_matrix,
)
from app.trading.pead_strategy import (
    EarningsEvent,
    EarningsSession,
    PEADConfig,
    evaluate_pead_candidate,
    resolve_entry_bar_index,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _make_bars(
    symbol: str,
    dates: list[date],
    opens: list[float],
    closes: list[float],
    lows: list[float] | None = None,
    highs: list[float] | None = None,
    volume: int = 1_000_000,
) -> list[Bar]:
    result: list[Bar] = []
    for idx, bar_date in enumerate(dates):
        o = opens[idx]
        c = closes[idx]
        h = highs[idx] if highs is not None else max(o, c) * 1.01
        lo = lows[idx] if lows is not None else min(o, c) * 0.99
        result.append(
            Bar(
                symbol=symbol,
                date=bar_date,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=volume,
            )
        )
    return result


def _base_strategy_config(**overrides) -> PEADConfig:
    params = {
        "min_eps_surprise_pct": 0.05,
        "min_revenue_surprise_pct": 0.0,
        "min_gap_pct": 0.02,
        "max_gap_pct": 0.25,
        "min_avg_dollar_volume": 10_000_000.0,
        "min_price": 5.0,
        "spy_regime_window": 0,
        "holding_days": 3,
        "stop_loss_pct": 0.08,
        "dollar_volume_window": 5,
    }
    params.update(overrides)
    return PEADConfig(**params)


def _base_backtest_config(**overrides) -> PEADBacktestConfig:
    params = {
        "initial_cash": 11_000.0,
        "commission_per_order": 1.0,
        "sec_fee_rate": 0.0000278,
        "finra_taf_per_share": 0.000166,
        "slippage_bps": 20.0,
        "max_positions": 4,
        "max_symbol_exposure_pct": 0.25,
        "max_total_exposure_pct": 0.80,
        "min_cash_reserve_pct": 0.10,
        "enforce_t_plus_1_settled_cash": True,
    }
    params.update(overrides)
    return PEADBacktestConfig(**params)


def test_earnings_event_surprise_math_handles_negative_and_zero_estimates() -> None:
    beat_positive = EarningsEvent(
        symbol="aapl",
        announcement_date=date(2026, 1, 10),
        session="bmo",
        eps_actual=1.10,
        eps_estimate=1.00,
    )
    assert beat_positive.symbol == "AAPL"
    assert beat_positive.session == EarningsSession.BMO
    assert beat_positive.eps_surprise_pct == pytest.approx(0.10)
    assert beat_positive.revenue_surprise_pct is None

    beat_negative_est = EarningsEvent(
        symbol="XYZ",
        announcement_date=date(2026, 1, 10),
        session=EarningsSession.AMC,
        eps_actual=-0.30,
        eps_estimate=-0.50,
    )
    assert beat_negative_est.eps_surprise_pct == pytest.approx(0.40)

    miss_negative_est = EarningsEvent(
        symbol="XYZ",
        announcement_date=date(2026, 1, 10),
        session=EarningsSession.AMC,
        eps_actual=-0.70,
        eps_estimate=-0.50,
    )
    assert miss_negative_est.eps_surprise_pct == pytest.approx(-0.40)

    zero_est = EarningsEvent(
        symbol="ZERO",
        announcement_date=date(2026, 1, 10),
        session=EarningsSession.BMO,
        eps_actual=0.05,
        eps_estimate=0.0,
        revenue_actual=100.0,
        revenue_estimate=0.0,
    )
    assert zero_est.eps_surprise_pct is None
    assert zero_est.revenue_surprise_pct is None


def test_bmo_vs_amc_alignment_and_zero_lookahead() -> None:
    dates = _weekdays(date(2026, 1, 5), 10)  # Mon..Fri + next Mon..Fri
    opens = [100.0] * 5 + [105.0, 106.0, 107.0, 108.0, 109.0]
    closes = [100.0] * 5 + [106.0, 107.0, 108.0, 109.0, 110.0]
    bars = _make_bars("AAA", dates, opens, closes, volume=500_000)

    # Announcement on Monday 2026-01-12 (index 5): gap open = 105 / 100 - 1 = 5%
    bmo_event = EarningsEvent(
        symbol="AAA",
        announcement_date=dates[5],
        session=EarningsSession.BMO,
        eps_actual=1.20,
        eps_estimate=1.00,
    )
    assert resolve_entry_bar_index(bars, bmo_event) == 5

    # Announcement AMC on Friday 2026-01-09 (index 4): first tradable bar is Monday index 5
    amc_friday_event = EarningsEvent(
        symbol="AAA",
        announcement_date=dates[4],
        session=EarningsSession.AMC,
        eps_actual=1.20,
        eps_estimate=1.00,
    )
    assert resolve_entry_bar_index(bars, amc_friday_event) == 5

    # Announcement AMC on Monday 2026-01-12 (index 5): MUST NOT enter on index 5; enters index 6
    amc_monday_event = EarningsEvent(
        symbol="AAA",
        announcement_date=dates[5],
        session=EarningsSession.AMC,
        eps_actual=1.20,
        eps_estimate=1.00,
    )
    assert resolve_entry_bar_index(bars, amc_monday_event) == 6

    # After last bar -> None
    after_end = EarningsEvent(
        symbol="AAA",
        announcement_date=dates[-1],
        session=EarningsSession.AMC,
        eps_actual=1.20,
        eps_estimate=1.00,
    )
    assert resolve_entry_bar_index(bars, after_end) is None

    # Zero lookahead: mutating entry_bar close/high/low/volume and future bars cannot alter evaluation
    spy_dates = dates
    # SPY strictly > SMA(3) before index 5, then crashes on index 5 close!
    spy_closes = [100.0, 101.0, 102.0, 103.0, 104.0, 50.0, 50.0, 50.0, 50.0, 50.0]
    spy_bars = _make_bars("SPY", spy_dates, spy_closes, spy_closes, volume=2_000_000)

    cfg = _base_strategy_config(spy_regime_window=3)
    eval_clean = evaluate_pead_candidate(bmo_event, bars, spy_bars=spy_bars, config=cfg)
    assert eval_clean.accepted is True
    assert eval_clean.candidate is not None
    assert eval_clean.candidate.entry_date == dates[5]
    assert eval_clean.candidate.gap_pct == pytest.approx(0.05)

    # Now mutate entry_bar close/high/low/volume and subsequent bars wildly
    mutated_bars = list(bars)
    mutated_bars[5] = Bar(
        symbol="AAA",
        date=dates[5],
        open=105.0,
        high=300.0,
        low=10.0,
        close=250.0,
        volume=1,
    )
    mutated_bars[6] = Bar(
        symbol="AAA",
        date=dates[6],
        open=200.0,
        high=210.0,
        low=190.0,
        close=205.0,
        volume=1,
    )
    eval_mutated = evaluate_pead_candidate(bmo_event, mutated_bars, spy_bars=spy_bars, config=cfg)
    assert eval_mutated.accepted is True
    assert eval_mutated.candidate == eval_clean.candidate

    # SPY below SMA on prior bar -> rejected even if entry_date SPY close rallies above SMA
    spy_bear_prior = _make_bars(
        "SPY",
        spy_dates,
        [100.0] * 10,
        [105.0, 104.0, 103.0, 102.0, 90.0, 150.0, 150.0, 150.0, 150.0, 150.0],
    )
    eval_spy_bear = evaluate_pead_candidate(bmo_event, bars, spy_bars=spy_bear_prior, config=cfg)
    assert eval_spy_bear.accepted is False
    assert eval_spy_bear.rejection_reason == "spy_below_regime_sma"

    # Illiquid prior bars cannot be rescued by huge volume ON entry_date
    illiquid_bars = _make_bars("AAA", dates, opens, closes, volume=10_000)
    illiquid_bars[5] = Bar(
        symbol="AAA",
        date=dates[5],
        open=105.0,
        high=106.0,
        low=104.0,
        close=105.0,
        volume=50_000_000,
    )
    eval_illiq = evaluate_pead_candidate(bmo_event, illiquid_bars, config=_base_strategy_config())
    assert eval_illiq.accepted is False
    assert eval_illiq.rejection_reason == "illiquid_avg_dollar_volume"


def test_t_plus_1_settled_cash_blocks_same_day_reuse_and_releases_on_next_trading_day() -> None:
    # 12 weekdays starting Mon 2026-01-05
    dates = _weekdays(date(2026, 1, 5), 12)
    # AAA enters on idx 2 (Wed Jan 7), held 2 bars -> exits on idx 4 open (Fri Jan 9).
    # Proceeds settle on next trading day on market calendar: idx 5 (Mon Jan 12)!
    aaa_opens = [100.0, 100.0, 105.0, 106.0, 110.0] + [110.0] * 7
    aaa_closes = [100.0, 100.0, 106.0, 108.0, 110.0] + [110.0] * 7

    # BBB enters on idx 4 (Fri Jan 9) — SAME DAY AAA sells!
    bbb_opens = [100.0, 100.0, 100.0, 100.0, 105.0] + [106.0] * 7
    bbb_closes = [100.0, 100.0, 100.0, 100.0, 106.0] + [107.0] * 7

    # CCC enters on idx 5 (Mon Jan 12) — T+1 after AAA sold on Fri Jan 9!
    ccc_opens = [100.0] * 5 + [105.0, 107.0, 108.0] + [108.0] * 4
    ccc_closes = [100.0] * 5 + [106.0, 108.0, 110.0] + [110.0] * 4

    bars_by_symbol = {
        "AAA": _make_bars("AAA", dates, aaa_opens, aaa_closes),
        "BBB": _make_bars("BBB", dates, bbb_opens, bbb_closes),
        "CCC": _make_bars("CCC", dates, ccc_opens, ccc_closes),
    }

    events = [
        EarningsEvent(
            symbol="AAA",
            announcement_date=dates[2],
            session=EarningsSession.BMO,
            eps_actual=1.20,
            eps_estimate=1.00,
        ),
        EarningsEvent(
            symbol="BBB",
            announcement_date=dates[4],
            session=EarningsSession.BMO,
            eps_actual=1.20,
            eps_estimate=1.00,
        ),
        EarningsEvent(
            symbol="CCC",
            announcement_date=dates[5],
            session=EarningsSession.BMO,
            eps_actual=1.20,
            eps_estimate=1.00,
        ),
    ]

    strat = _base_strategy_config(holding_days=2, dollar_volume_window=2)
    # Allow 90% single position + 10% reserve so AAA consumes ALL spendable settled_cash!
    t1_cfg = _base_backtest_config(
        slippage_bps=0.0,
        commission_per_order=0.0,
        sec_fee_rate=0.0,
        finra_taf_per_share=0.0,
        max_symbol_exposure_pct=0.90,
        max_total_exposure_pct=0.90,
        min_cash_reserve_pct=0.10,
        enforce_t_plus_1_settled_cash=True,
    )
    res_t1 = run_pead_backtest(events, bars_by_symbol, strategy_config=strat, backtest_config=t1_cfg)

    # BBB on Fri Jan 9 MUST be blocked by unsettled cash from AAA's Fri Jan 9 sale!
    # CCC on Mon Jan 12 MUST succeed because AAA's proceeds settled on Mon Jan 12 (T+1)!
    assert [trade.symbol for trade in res_t1.trades] == ["AAA", "CCC"]
    assert res_t1.blocked_by_unsettled_cash_count == 1
    assert res_t1.trades[1].entry_date == dates[5]

    # Verify Friday Jan 9 ledger point shows unsettled_cash > 0 and settled_cash <= reserve
    fri_point = next(pt for pt in res_t1.equity_curve if pt["date"] == dates[4])
    assert fri_point["unsettled_cash"] > 9_000.0
    assert fri_point["settled_cash"] <= 1_200.0

    # With T+1 disabled, BBB executes immediately on Fri Jan 9
    no_t1_cfg = _base_backtest_config(
        slippage_bps=0.0,
        commission_per_order=0.0,
        sec_fee_rate=0.0,
        finra_taf_per_share=0.0,
        max_symbol_exposure_pct=0.90,
        max_total_exposure_pct=0.90,
        min_cash_reserve_pct=0.10,
        enforce_t_plus_1_settled_cash=False,
    )
    res_no_t1 = run_pead_backtest(
        events, bars_by_symbol, strategy_config=strat, backtest_config=no_t1_cfg
    )
    assert [trade.symbol for trade in res_no_t1.trades] == ["AAA", "BBB"]
    assert res_no_t1.blocked_by_unsettled_cash_count == 0


def test_slippage_gradient_and_sec_finra_fees_and_taf_cap() -> None:
    dates = _weekdays(date(2026, 1, 5), 10)
    opens = [100.0, 100.0, 105.0, 107.0, 109.0, 112.0, 112.0, 112.0, 112.0, 112.0]
    closes = [100.0, 100.0, 106.0, 108.0, 110.0, 112.0, 112.0, 112.0, 112.0, 112.0]
    bars_by_symbol = {"AAA": _make_bars("AAA", dates, opens, closes)}
    events = [
        EarningsEvent(
            symbol="AAA",
            announcement_date=dates[2],
            session=EarningsSession.BMO,
            eps_actual=1.20,
            eps_estimate=1.00,
        )
    ]

    strat = _base_strategy_config(holding_days=3, dollar_volume_window=2)
    matrix = run_pead_slippage_matrix(
        events,
        bars_by_symbol,
        strategy_config=strat,
        backtest_config=_base_backtest_config(),
        slippage_grid=(10.0, 20.0, 30.0),
    )

    r10 = matrix.results_by_bps[10.0]
    r20 = matrix.results_by_bps[20.0]
    r30 = matrix.results_by_bps[30.0]
    assert r10.final_equity > r20.final_equity > r30.final_equity
    assert r10.realized_pnl > r20.realized_pnl > r30.realized_pnl

    # Verify exact SEC and FINRA TAF fee math on 20 bps run
    trade = r20.trades[0]
    gross_sell = trade.quantity * trade.exit_price
    assert trade.entry_commission == pytest.approx(1.0)
    assert trade.exit_commission == pytest.approx(1.0)
    assert trade.sec_fee == pytest.approx(gross_sell * 0.0000278, rel=1e-4)
    assert trade.finra_taf_fee == pytest.approx(trade.quantity * 0.000166, rel=1e-4)

    # Verify FINRA TAF $8.30 cap on huge share quantity (100,000 shares * $0.000166 = $16.60 -> $8.30)
    penny_opens = [6.0, 6.0, 6.30, 6.40, 6.50, 6.60, 6.60, 6.60, 6.60, 6.60]
    penny_closes = [6.0, 6.0, 6.35, 6.45, 6.55, 6.60, 6.60, 6.60, 6.60, 6.60]
    large_res = run_pead_backtest(
        events,
        {"AAA": _make_bars("AAA", dates, penny_opens, penny_closes, volume=5_000_000)},
        strategy_config=strat,
        backtest_config=_base_backtest_config(
            initial_cash=1_000_000.0,
            max_symbol_exposure_pct=0.80,
            slippage_bps=0.0,
        ),
    )
    large_trade = large_res.trades[0]
    assert large_trade.quantity > 100_000
    assert large_trade.finra_taf_fee == pytest.approx(8.30)


def test_stop_loss_gap_through_open_and_intraday_low() -> None:
    dates = _weekdays(date(2026, 1, 5), 8)
    # Gap-down open below 8% stop on idx 3 (entry 105 -> stop 96.60; idx 3 opens at 80!)
    opens = [100.0, 100.0, 105.0, 80.0, 80.0, 80.0, 80.0, 80.0]
    closes = [100.0, 100.0, 105.0, 80.0, 80.0, 80.0, 80.0, 80.0]
    lows = [99.0, 99.0, 104.0, 79.0, 79.0, 79.0, 79.0, 79.0]
    bars = _make_bars("AAA", dates, opens, closes, lows=lows)
    events = [
        EarningsEvent(
            symbol="AAA",
            announcement_date=dates[2],
            session=EarningsSession.BMO,
            eps_actual=1.20,
            eps_estimate=1.00,
        )
    ]
    res = run_pead_backtest(
        events,
        {"AAA": bars},
        strategy_config=_base_strategy_config(holding_days=5, stop_loss_pct=0.08, dollar_volume_window=2),
        backtest_config=_base_backtest_config(slippage_bps=0.0),
    )
    assert res.closed_trades == 1
    assert res.trades[0].exit_reason == "stop_loss_gap_open"
    assert res.trades[0].exit_price == pytest.approx(80.0)

    # Intraday low hits stop_price while open stays above stop_price
    intraday_opens = [100.0, 100.0, 105.0, 102.0, 102.0, 102.0, 102.0, 102.0]
    intraday_closes = [100.0, 100.0, 105.0, 95.0, 102.0, 102.0, 102.0, 102.0]
    intraday_lows = [99.0, 99.0, 104.0, 92.0, 101.0, 101.0, 101.0, 101.0]
    intraday_bars = _make_bars("AAA", dates, intraday_opens, intraday_closes, lows=intraday_lows)
    res_intraday = run_pead_backtest(
        events,
        {"AAA": intraday_bars},
        strategy_config=_base_strategy_config(holding_days=5, stop_loss_pct=0.08, dollar_volume_window=2),
        backtest_config=_base_backtest_config(slippage_bps=0.0),
    )
    assert res_intraday.closed_trades == 1
    assert res_intraday.trades[0].exit_reason == "stop_loss_intraday"
    assert res_intraday.trades[0].exit_price == pytest.approx(105.0 * 0.92)


def test_trades_vs_adjusted_last_execution_and_discrepancy_audit() -> None:
    dates = _weekdays(date(2026, 1, 5), 8)
    # ADJUSTED_LAST has a 5:1 split distortion on prior bar (20.0 -> 105.0 fake +425% gap -> rejected!)
    adj_opens = [20.0, 20.0, 105.0, 107.0, 110.0, 112.0, 112.0, 112.0]
    adj_closes = [20.0, 20.0, 106.0, 108.0, 110.0, 112.0, 112.0, 112.0]
    # Unadjusted TRADES shows true pre-event close 100.0 -> entry open 105.0 (+5% real gap -> accepted!)
    trades_opens = [100.0, 100.0, 105.0, 107.0, 110.0, 112.0, 112.0, 112.0]
    trades_closes = [100.0, 100.0, 106.0, 108.0, 110.0, 112.0, 112.0, 112.0]

    events = [
        EarningsEvent(
            symbol="AAA",
            announcement_date=dates[2],
            session=EarningsSession.BMO,
            eps_actual=1.20,
            eps_estimate=1.00,
        )
    ]
    strat = _base_strategy_config(holding_days=3, dollar_volume_window=2)
    cfg = _base_backtest_config(slippage_bps=0.0)

    res = run_pead_backtest(
        events,
        bars_by_symbol={"AAA": _make_bars("AAA", dates, adj_opens, adj_closes)},
        trades_bars_by_symbol={"AAA": _make_bars("AAA", dates, trades_opens, trades_closes)},
        strategy_config=strat,
        backtest_config=cfg,
    )
    assert res.closed_trades == 1
    trade = res.trades[0]
    assert trade.used_trades_prices is True
    assert trade.raw_entry_price == pytest.approx(105.0)
    assert trade.quantity == floor((11_000.0 * 0.25) / 105.0)

    audit = res.price_discrepancy_audit
    assert audit["trades_bars_provided"] is True
    assert audit["used_trades_for_execution"] is True
    assert audit["bars_with_price_diff_gt_5pct"] == 2
    assert audit["decision_changed_count"] == 1
    assert len(audit["event_discrepancies"]) == 1
    assert audit["event_discrepancies"][0]["trades_accepted"] is True
    assert audit["event_discrepancies"][0]["adj_accepted"] is False


def test_dev_holdout_split_by_entry_date() -> None:
    dates = _weekdays(date(2026, 1, 5), 14)
    # Dev event enters idx 2 (Jan 7) and holds 5 days -> exits idx 7 (Jan 14), AFTER holdout_start (idx 6)
    aaa_opens = [100.0, 100.0, 105.0, 106.0, 107.0, 108.0, 109.0, 112.0] + [112.0] * 6
    aaa_closes = [100.0, 100.0, 106.0, 107.0, 108.0, 109.0, 110.0, 112.0] + [112.0] * 6
    # Holdout event enters idx 8 (Jan 15), exits idx 13 (Jan 22)
    bbb_opens = [100.0] * 8 + [105.0, 106.0, 107.0, 108.0, 109.0, 115.0]
    bbb_closes = [100.0] * 8 + [106.0, 107.0, 108.0, 109.0, 110.0, 115.0]

    events = [
        EarningsEvent(
            symbol="AAA",
            announcement_date=dates[2],
            session=EarningsSession.BMO,
            eps_actual=1.20,
            eps_estimate=1.00,
        ),
        EarningsEvent(
            symbol="BBB",
            announcement_date=dates[8],
            session=EarningsSession.BMO,
            eps_actual=1.30,
            eps_estimate=1.00,
        ),
    ]
    res = run_pead_backtest(
        events,
        {
            "AAA": _make_bars("AAA", dates, aaa_opens, aaa_closes),
            "BBB": _make_bars("BBB", dates, bbb_opens, bbb_closes),
        },
        strategy_config=_base_strategy_config(holding_days=5, dollar_volume_window=2),
        backtest_config=_base_backtest_config(),
        holdout_start_date=dates[6],
    )

    assert res.dev_metrics is not None
    assert res.holdout_metrics is not None
    assert res.dev_metrics.closed_trades == 1
    assert res.holdout_metrics.closed_trades == 1
    assert res.trades[0].symbol == "AAA"
    assert res.trades[0].exit_date >= dates[6]  # closed inside holdout, attributed to dev by entry_date!
    assert res.dev_metrics.realized_pnl == pytest.approx(res.trades[0].pnl, abs=0.02)
    assert res.holdout_metrics.realized_pnl == pytest.approx(res.trades[1].pnl, abs=0.02)
    assert res.dev_metrics.realized_pnl + res.holdout_metrics.realized_pnl == pytest.approx(
        res.realized_pnl, abs=0.02
    )


def test_top3_removal_t_stat_survivorship_and_kill_criteria_triggers() -> None:
    # Lottery-ticket test: 3 huge winners + 5 small losers -> net profit > 0, but excluding Top 3 <= 0!
    dates = _weekdays(date(2026, 1, 5), 30)
    bars_by_symbol: dict[str, list[Bar]] = {}
    events: list[EarningsEvent] = []
    outcomes = [1.30, 1.25, 1.20, 0.96, 0.96, 0.96, 0.96, 0.96]
    for idx, mult in enumerate(outcomes):
        sym = f"SYM{idx}"
        entry_i = 2 + idx * 3
        opens = [100.0] * len(dates)
        closes = [100.0] * len(dates)
        opens[entry_i] = 105.0
        closes[entry_i] = 105.0
        opens[entry_i + 1] = 105.0 * mult
        closes[entry_i + 1] = 105.0 * mult
        opens[entry_i + 2] = 105.0 * mult
        closes[entry_i + 2] = 105.0 * mult
        bars_by_symbol[sym] = _make_bars(sym, dates, opens, closes)
        events.append(
            EarningsEvent(
                symbol=sym,
                announcement_date=dates[entry_i],
                session=EarningsSession.BMO,
                eps_actual=1.20,
                eps_estimate=1.00,
            )
        )

    res = run_pead_backtest(
        events,
        bars_by_symbol,
        strategy_config=_base_strategy_config(
            holding_days=2, stop_loss_pct=0.20, dollar_volume_window=2
        ),
        backtest_config=_base_backtest_config(
            slippage_bps=20.0,
            universe_note="Test synthetic universe with survivorship disclosure.",
        ),
    )
    assert res.closed_trades == 8
    assert res.realized_pnl > 0
    assert res.pnl_excluding_top3 < 0
    assert res.return_pct_excluding_top3 < 0
    assert "Test synthetic universe with survivorship disclosure." in res.survivorship_bias_disclosure

    # Independent t-stat check
    rets = [t.return_pct for t in res.trades]
    expected_t = (sum(rets) / len(rets)) / (stdev(rets) / sqrt(len(rets)))
    assert res.trade_t_statistic == pytest.approx(expected_t, abs=1e-3)

    assert res.kill_criteria.killed is True
    assert "pnl_excluding_top3_non_positive" in res.kill_criteria.triggered_rules

    # Test Kill Criteria rules individually with controlled PEADBacktestResult
    def _dummy_result(**overrides) -> PEADBacktestResult:
        defaults = dict(
            start_date=date(2026, 1, 5),
            end_date=date(2026, 6, 30),
            initial_cash=11_000.0,
            final_equity=15_000.0,
            total_return_pct=36.36,
            cagr_pct=80.0,
            max_drawdown_pct=-5.0,
            annualized_volatility_pct=10.0,
            sharpe_ratio=1.5,
            has_benchmark=True,
            benchmark_total_return_pct=20.0,
            benchmark_cagr_pct=40.0,
            benchmark_max_drawdown_pct=-15.0,
            benchmark_sharpe_ratio=1.0,
            excess_return_vs_spy_pct=16.36,
            closed_trades=10,
            win_rate_pct=70.0,
            realized_pnl=4_000.0,
            avg_trade_return_pct=5.0,
            pnl_excluding_top3=1_500.0,
            return_pct_excluding_top3=13.64,
            trade_t_statistic=3.2,
            slippage_bps=20.0,
            total_commissions=20.0,
            total_sec_fees=1.0,
            total_finra_taf_fees=0.5,
            total_slippage_cost=50.0,
            total_costs=71.5,
            total_events_evaluated=10,
            accepted_candidates_count=10,
            rejected_candidates_count=0,
            rejection_reason_counts={},
            blocked_by_unsettled_cash_count=0,
            blocked_by_max_positions_count=0,
            blocked_by_exposure_or_reserve_count=0,
            skipped_existing_position_count=0,
            holdout_start_date=None,
            dev_metrics=None,
            holdout_metrics=None,
            price_discrepancy_audit={},
            survivorship_bias_disclosure="disclosure",
            universe_note="note",
            kill_criteria=PEADKillCriteriaResult(False, "PASS", "full_sample", 20.0, [], []),
            equity_curve=[],
            trades=[],
            open_positions=[],
            evaluations=[],
        )
        defaults.update(overrides)
        return PEADBacktestResult(**defaults)

    # All passing
    assert evaluate_pead_kill_criteria(_dummy_result(), 20.0).killed is False

    # Sharpe < 1.0 triggers kill
    kc_sharpe = evaluate_pead_kill_criteria(_dummy_result(sharpe_ratio=0.95), 20.0)
    assert kc_sharpe.killed is True
    assert "sharpe_below_1.0" in kc_sharpe.triggered_rules

    # Underperforms SPY WITHOUT reducing drawdown triggers kill
    kc_spy_bad_dd = evaluate_pead_kill_criteria(
        _dummy_result(
            total_return_pct=12.0,
            benchmark_total_return_pct=20.0,
            max_drawdown_pct=-18.0,
            benchmark_max_drawdown_pct=-15.0,
        ),
        20.0,
    )
    assert kc_spy_bad_dd.killed is True
    assert "underperforms_spy_without_drawdown_reduction" in kc_spy_bad_dd.triggered_rules

    # Underperforms SPY WITH meaningful drawdown reduction does NOT trigger SPY rule
    kc_spy_good_dd = evaluate_pead_kill_criteria(
        _dummy_result(
            total_return_pct=12.0,
            benchmark_total_return_pct=20.0,
            max_drawdown_pct=-5.0,
            benchmark_max_drawdown_pct=-15.0,
        ),
        20.0,
    )
    assert kc_spy_good_dd.killed is False

    # Holdout scope takes precedence when holdout_metrics is present
    holdout_bad = PEADPeriodMetrics(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 6, 30),
        initial_equity=13_000.0,
        final_equity=12_800.0,
        total_return_pct=-1.54,
        cagr_pct=-6.0,
        max_drawdown_pct=-10.0,
        annualized_volatility_pct=12.0,
        sharpe_ratio=-0.4,
        has_benchmark=True,
        benchmark_total_return_pct=8.0,
        benchmark_cagr_pct=35.0,
        benchmark_max_drawdown_pct=-5.0,
        benchmark_sharpe_ratio=1.5,
        excess_return_vs_spy_pct=-9.54,
        closed_trades=4,
        win_rate_pct=25.0,
        realized_pnl=-200.0,
        avg_trade_return_pct=-1.0,
        pnl_excluding_top3=-250.0,
        return_pct_excluding_top3=-1.92,
        trade_t_statistic=-1.2,
        total_commissions=8.0,
        total_sec_fees=0.2,
        total_finra_taf_fees=0.1,
        total_slippage_cost=10.0,
        total_costs=18.3,
    )
    kc_holdout = evaluate_pead_kill_criteria(_dummy_result(holdout_metrics=holdout_bad), 20.0)
    assert kc_holdout.killed is True
    assert kc_holdout.evaluated_scope == "holdout"
    assert set(kc_holdout.triggered_rules) == {
        "sharpe_below_1.0",
        "underperforms_spy_without_drawdown_reduction",
        "pnl_excluding_top3_non_positive",
    }


def test_cli_run_pead_study_missing_csv_and_end_to_end(tmp_path: Path) -> None:
    script_path = PROJECT_ROOT / "scripts" / "run_pead_study.py"

    # 1. Missing --events-csv prints helpful schema error
    missing_proc = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_proc.returncode != 0
    assert (
        "symbol,announcement_date,session,eps_actual,eps_estimate,revenue_actual,revenue_estimate,source"
        in missing_proc.stderr
    )

    # 2. End-to-end execution with temp history, trades, and events CSV
    history_dir = tmp_path / "history"
    trades_dir = tmp_path / "trades"
    output_dir = tmp_path / "backtests"
    history_dir.mkdir()
    trades_dir.mkdir()

    dates = _weekdays(date(2026, 1, 5), 15)
    for sym, opens, closes in [
        (
            "SPY",
            [400.0 + i for i in range(len(dates))],
            [401.0 + i for i in range(len(dates))],
        ),
        (
            "AAPL",
            [100.0, 100.0, 100.0, 105.0, 107.0, 108.0, 109.0, 110.0]
            + [111.0] * (len(dates) - 8),
            [100.0, 100.0, 100.0, 106.0, 108.0, 109.0, 110.0, 111.0]
            + [112.0] * (len(dates) - 8),
        ),
    ]:
        for target_dir in (history_dir, trades_dir):
            with (target_dir / f"{sym}.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["date", "open", "high", "low", "close", "volume"])
                for idx, d in enumerate(dates):
                    o = opens[idx]
                    c = closes[idx]
                    writer.writerow([d.isoformat(), o, max(o, c) + 1.0, min(o, c) - 1.0, c, 1_000_000])

    events_csv = tmp_path / "events.csv"
    with events_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "symbol",
                "announcement_date",
                "session",
                "eps_actual",
                "eps_estimate",
                "revenue_actual",
                "revenue_estimate",
                "source",
            ]
        )
        writer.writerow(["AAPL", dates[2].isoformat(), "AMC", "1.60", "1.40", "105000000", "100000000", "SEC_8K"])

    ok_proc = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--events-csv",
            str(events_csv),
            "--history-dir",
            str(history_dir),
            "--trades-dir",
            str(trades_dir),
            "--holdout-start",
            dates[7].isoformat(),
            "--spy-regime-window",
            "2",
            "--holding-days",
            "3",
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_proc.returncode == 0, ok_proc.stderr
    latest_json = output_dir / "pead-study-latest.json"
    latest_md = output_dir / "pead-study-latest.md"
    assert latest_json.exists()
    assert latest_md.exists()

    payload = json.loads(latest_json.read_text(encoding="utf-8"))
    assert payload["slippage_matrix"]["slippage_grid"] == [10.0, 20.0, 30.0]
    assert len(payload["slippage_matrix"]["runs"]) == 3
    run20 = payload["slippage_matrix"]["runs"][1]["result"]
    assert run20["closed_trades"] == 1
    assert run20["price_discrepancy_audit"]["trades_bars_provided"] is True
    assert "SURVIVORSHIP BIAS DISCLOSURE" in run20["survivorship_bias_disclosure"]
