import json
from datetime import date, timedelta

import pytest

from app.trading.index_backtest import (
    STRESS_WINDOWS,
    IndexBacktestConfig,
    PortfolioRule,
    compute_annual_breakdown,
    compute_rolling_windows,
    compute_stress_periods,
    run_index_backtest,
)
from app.trading.models import Bar


def _bars(
    symbol: str,
    dates: list[date],
    closes: list[float],
    opens: list[float] | None = None,
) -> list[Bar]:
    opens = opens or closes
    return [
        Bar(
            symbol=symbol,
            date=bar_date,
            open=opens[index],
            high=max(opens[index], closes[index]),
            low=min(opens[index], closes[index]),
            close=closes[index],
            volume=1_000_000,
        )
        for index, bar_date in enumerate(dates)
    ]


def _config(**overrides) -> IndexBacktestConfig:
    values = {
        "initial_cash": 1_000.0,
        "commission_per_order": 0.0,
        "slippage_bps": 0.0,
    }
    values.update(overrides)
    return IndexBacktestConfig(**values)


# ── Existing baseline tests (preserved) ─────────────────────────────────────────


def test_trend_filter_uses_only_closes_before_execution_day():
    dates = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    result = run_index_backtest(
        {"SPY": _bars("SPY", dates, [100, 110, 80], [100, 100, 120])},
        PortfolioRule("trend", {"SPY": 1.0}, "monthly", trend_window=2),
        config=_config(),
        start_date=date(2026, 1, 3),
    )

    assert result.ending_holdings == {"SPY": 8}
    assert result.final_equity == 680.0


def test_trend_filter_stays_in_cash_when_previous_close_is_below_average():
    dates = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    result = run_index_backtest(
        {"SPY": _bars("SPY", dates, [100, 90, 200])},
        PortfolioRule("trend", {"SPY": 1.0}, "monthly", trend_window=2),
        config=_config(),
        start_date=date(2026, 1, 3),
    )

    assert result.ending_holdings == {}
    assert result.final_equity == 1_000.0


def test_quarterly_rule_rebalances_on_first_date_of_new_quarter():
    dates = [date(2025, 12, 30), date(2026, 1, 2)]
    result = run_index_backtest(
        {
            "SPY": _bars("SPY", dates, [100, 200]),
            "QQQ": _bars("QQQ", dates, [100, 50]),
        },
        PortfolioRule("mix", {"SPY": 0.5, "QQQ": 0.5}, "quarterly"),
        config=_config(),
    )

    assert result.order_count == 4
    assert result.ending_holdings == {"SPY": 3, "QQQ": 12}


def test_commission_and_slippage_reduce_buy_and_hold_result():
    dates = [date(2026, 1, 1), date(2026, 1, 2)]
    bars = {"SPY": _bars("SPY", dates, [100, 120])}
    rule = PortfolioRule("hold", {"SPY": 1.0}, "buy_and_hold")

    without_costs = run_index_backtest(bars, rule, config=_config())
    with_costs = run_index_backtest(
        bars,
        rule,
        config=_config(commission_per_order=1.0, slippage_bps=10.0),
    )

    assert with_costs.final_equity < without_costs.final_equity
    assert with_costs.total_costs > 0


# ── Config & rule validation tests ──────────────────────────────────────────────


def test_config_and_rule_validations():
    with pytest.raises(ValueError):
        IndexBacktestConfig(initial_cash=0)
    with pytest.raises(ValueError):
        IndexBacktestConfig(periodic_contribution=-10.0)
    with pytest.raises(ValueError):
        IndexBacktestConfig(contribution_frequency="weekly")

    with pytest.raises(ValueError):
        PortfolioRule("bad_buf_high", {"SPY": 1.0}, trend_buffer_pct=0.5)
    with pytest.raises(ValueError):
        PortfolioRule("bad_buf_neg", {"SPY": 1.0}, trend_buffer_pct=-0.01)
    with pytest.raises(ValueError):
        PortfolioRule("drift_missing", {"SPY": 1.0}, rebalance_frequency="drift")
    with pytest.raises(ValueError):
        PortfolioRule("drift_bounds", {"SPY": 1.0}, rebalance_frequency="drift", drift_threshold_pct=1.0)
    with pytest.raises(ValueError):
        PortfolioRule("sched_bounds", {"SPY": 1.0}, rebalance_frequency="monthly", drift_threshold_pct=0.0)


# ── Defensive ETF routing tests ─────────────────────────────────────────────────


def test_defensive_symbol_routes_risk_off_weight_and_rotates_back():
    dates = [
        date(2026, 1, 29),
        date(2026, 1, 30),
        date(2026, 2, 2),
        date(2026, 2, 26),
        date(2026, 2, 27),
        date(2026, 3, 2),
    ]
    bars = {
        "SPY": _bars("SPY", dates, [100, 90, 95, 110, 120, 120]),
        "BIL": _bars("BIL", dates, [100, 100, 100, 101, 102, 102]),
    }
    rule = PortfolioRule(
        "trend_bil",
        {"SPY": 1.0},
        rebalance_frequency="monthly",
        trend_window=2,
        defensive_symbol="BIL",
    )

    result = run_index_backtest(bars, rule, config=_config(), start_date=date(2026, 2, 2))

    # Feb: SPY risk-off -> holds 10 BIL; Mar 2: SPY risk-on -> sells 10 BIL ($1020), buys 8 SPY ($960)
    assert result.equity_curve[0]["gross_exposure"] == 1_000.0
    assert result.equity_curve[2]["equity"] == 1_020.0
    assert result.ending_holdings == {"SPY": 8}
    assert result.final_equity == 1_020.0


def test_defensive_symbol_in_target_weights_receives_freed_weight():
    dates = [date(2026, 1, 29), date(2026, 1, 30), date(2026, 2, 2)]
    bars = {
        "SPY": _bars("SPY", dates, [100, 90, 90]),
        "BIL": _bars("BIL", dates, [100, 100, 100]),
    }
    rule = PortfolioRule(
        "mix_with_bil",
        {"SPY": 0.6, "BIL": 0.4},
        rebalance_frequency="monthly",
        trend_window=2,
        defensive_symbol="BIL",
    )

    result = run_index_backtest(bars, rule, config=_config(), start_date=date(2026, 2, 2))
    assert result.ending_holdings == {"BIL": 10}
    assert result.final_equity == 1_000.0


# ── Hysteresis trend_buffer_pct tests ───────────────────────────────────────────


def test_trend_buffer_hysteresis_prevents_whipsaw():
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(9)]
    # d0,d1 warmup: [100, 110] -> d2 prev_close=110 > SMA 105 -> risk_on=True, buys 10 @ 100
    # d2=112 -> d3: SMA=111, prev_close=112 > 111 (hold)
    # d3=108 -> d4: SMA=110, prev_close=108 < 110 (0.0 buf sells!), >= 104.5 (0.05 buf holds!)
    # d4=112 -> d5: SMA=110, prev_close=112 > 110 (0.0 buf rebuys!)
    # d5=90  -> d6: SMA=101, prev_close=90 < 95.95 -> both flip False and sell
    # d6=95  -> d7: SMA=92.5, prev_close=95 > 92.5 (0.0 buf rebuys!), <= 97.125 (0.05 buf stays out!)
    # d7=110 -> d8: SMA=102.5, prev_close=110 > 107.625 -> 0.05 buf flips True and buys
    closes = [100, 110, 112, 108, 112, 90, 95, 110, 110]
    opens = [100, 110, 100, 112, 108, 112, 90, 95, 110]
    bars = {"SPY": _bars("SPY", dates, closes, opens)}

    no_buffer = run_index_backtest(
        bars,
        PortfolioRule("no_buf", {"SPY": 1.0}, "drift", trend_window=2, drift_threshold_pct=0.50, trend_buffer_pct=0.0),
        config=_config(),
        start_date=dates[2],
    )
    with_buffer = run_index_backtest(
        bars,
        PortfolioRule("buf_5", {"SPY": 1.0}, "drift", trend_window=2, drift_threshold_pct=0.50, trend_buffer_pct=0.05),
        config=_config(),
        start_date=dates[2],
    )

    # no_buffer: buy(d2), sell(d4), buy(d5), sell(d6), buy(d7) = 5 orders
    # with_buffer: buy(d2), sell(d6), buy(d8) = 3 orders (skipped both whipsaws!)
    assert no_buffer.order_count == 5
    assert with_buffer.order_count == 3
    assert with_buffer.ending_holdings == {"SPY": 8}


def test_run_index_study_defensive_fallback_and_offline_cli(tmp_path, monkeypatch):
    import sys
    from scripts import run_index_study

    history_dir = tmp_path / "history"
    output_dir = tmp_path / "backtests"
    history_dir.mkdir()

    # Without BIL/SHY, resolve_defensive_symbol returns None and sync note
    sym, note = run_index_study.resolve_defensive_symbol(history_dir, "BIL")
    assert sym is None
    assert note is not None and "--years 10 --sync" in note

    # Write synthetic CSVs for ALL_SYMBOLS + BIL (250 trading days)
    start = date(2025, 1, 2)
    dates = [start + timedelta(days=i) for i in range(260)]
    for idx, symbol in enumerate([*run_index_study.ALL_SYMBOLS, "BIL"]):
        bars = _bars(
            symbol,
            dates,
            [100.0 + (0.1 if symbol != "BIL" else 0.01) * i + idx for i in range(len(dates))],
        )
        run_index_study.save_history(history_dir, {symbol: bars})

    sym_with_bil, note_with_bil = run_index_study.resolve_defensive_symbol(history_dir, "BIL")
    assert sym_with_bil == "BIL"
    assert note_with_bil is None

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_index_study.py",
            "--years",
            "1",
            "--history-dir",
            str(history_dir),
            "--output-dir",
            str(output_dir),
        ],
    )
    rc = run_index_study.main()
    assert rc == 0
    latest_json = json.loads((output_dir / "index-study-latest.json").read_text(encoding="utf-8"))
    latest_md = (output_dir / "index-study-latest.md").read_text(encoding="utf-8")
    assert latest_json["defensive_symbol"] == "BIL"
    assert len(latest_json["index_strategies"]) == 6
    assert len(latest_json["dca_strategies"]) == 6
    assert "Monthly DCA" in latest_md
    assert "避险 BIL" in latest_md


# ── Drift rebalancing tests ─────────────────────────────────────────────────────


def test_drift_rebalance_skips_below_threshold_and_triggers_at_threshold():
    dates = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    # Start $2000 -> 10 SPY @ 100, 10 QQQ @ 100 (2 orders)
    # Day 1 open: SPY=108, QQQ=92 -> 54%/46%, max drift=4% < 5% -> skip
    # Day 2 open: SPY=110, QQQ=90 -> 55%/45%, max drift=5% >= 5% -> rebalance (2 more orders: sell 1 SPY, buy 1 QQQ)
    bars = {
        "SPY": _bars("SPY", dates, [100, 108, 110], [100, 108, 110]),
        "QQQ": _bars("QQQ", dates, [100, 92, 90], [100, 92, 90]),
    }
    rule = PortfolioRule("drift_5", {"SPY": 0.5, "QQQ": 0.5}, "drift", drift_threshold_pct=0.05)

    result = run_index_backtest(bars, rule, config=_config(initial_cash=2_000.0))

    assert result.order_count == 4
    assert result.ending_holdings == {"SPY": 9, "QQQ": 11}


def test_monthly_with_drift_threshold_only_rebalances_when_drift_exceeded():
    dates = [date(2026, 1, 15), date(2026, 2, 2), date(2026, 3, 2)]
    bars = {
        "SPY": _bars("SPY", dates, [100, 108, 120], [100, 108, 120]),
        "QQQ": _bars("QQQ", dates, [100, 92, 80], [100, 92, 80]),
    }
    rule = PortfolioRule("monthly_drift", {"SPY": 0.5, "QQQ": 0.5}, "monthly", drift_threshold_pct=0.05)

    result = run_index_backtest(bars, rule, config=_config(initial_cash=2_000.0))

    # Feb 2 has 4% drift (< 5%), skipped; Mar 2 has 10% drift (>= 5%), rebalanced (sell 1 SPY, buy 2 QQQ)
    assert result.order_count == 4
    assert result.ending_holdings == {"SPY": 8, "QQQ": 12}


# ── Monthly DCA TWR / XIRR / Drawdown tests ─────────────────────────────────────


def test_monthly_dca_twr_and_drawdown_are_not_distorted_by_deposits():
    dates = [
        date(2026, 1, 2),   # open 100, close 100 -> 10 shares, eq=1000
        date(2026, 1, 15),  # open 100, close 50 -> eq=500 (-50% drawdown)
        date(2026, 2, 2),   # +$1000 contribution -> open 50, close 50 -> buys 20 (30 total), eq=1500 (TWR still 0.5!)
        date(2026, 3, 2),   # +$1000 contribution -> open 50, close 100 -> buys 20 (50 total), eq=5000 (TWR back to 1.0!)
    ]
    bars = {"SPY": _bars("SPY", dates, [100, 50, 50, 100], [100, 100, 50, 50])}
    rule = PortfolioRule("dca_hold", {"SPY": 1.0}, "buy_and_hold")
    cfg = _config(
        initial_cash=1_000.0,
        periodic_contribution=1_000.0,
        contribution_frequency="monthly",
    )

    result = run_index_backtest(bars, rule, config=cfg)

    assert result.total_contributed == 3_000.0
    assert result.final_equity == 5_000.0
    assert result.net_profit == 2_000.0
    assert result.twr_total_return_pct == 0.0
    assert result.total_return_pct == 0.0
    assert result.max_drawdown_pct == -50.0
    assert result.mwr_xirr_pct > 100.0
    assert result.equity_curve[2]["contribution"] == 1_000.0
    assert result.equity_curve[3]["contribution"] == 1_000.0


def test_monthly_dca_deploys_cash_into_underweight_target_without_selling():
    dates = [
        date(2026, 1, 5),   # init $2000 -> 10 SPY @ 100, 10 QQQ @ 100
        date(2026, 2, 2),   # non-quarter month! +$1000 contribution; SPY=250 (strictly overweight >$2250), QQQ=100 (underweight)
    ]
    bars = {
        "SPY": _bars("SPY", dates, [100, 250]),
        "QQQ": _bars("QQQ", dates, [100, 100]),
    }
    rule = PortfolioRule("qtr_dca", {"SPY": 0.5, "QQQ": 0.5}, "quarterly")
    cfg = _config(
        initial_cash=2_000.0,
        periodic_contribution=1_000.0,
        contribution_frequency="monthly",
    )

    result = run_index_backtest(bars, rule, config=cfg)

    # Buy-only must keep 10 SPY (never sell) and put all $1000 into 10 QQQ
    assert result.ending_holdings == {"SPY": 10, "QQQ": 20}
    assert result.final_equity == 4_500.0
    assert result.order_count == 3


def test_dca_xirr_matches_known_annualized_rate():
    # Invest $1000 at start (2025-01-01) and $1000 mid-year (2025-07-02, 182 days).
    # Choose exact end equity on 2026-01-01 (365 days) for 20% annual IRR:
    # 1000 * 1.2^(365/365.25) + 1000 * 1.2^(183/365.25)
    dates = [date(2025, 1, 1), date(2025, 7, 2), date(2026, 1, 1)]
    # Make SPY track price so that 1 share = 1 dollar -> actually fractional shares aren't supported,
    # so use $100k scale to eliminate integer-share rounding error!
    initial = 100_000.0
    contrib = 100_000.0
    # On 2025-01-01: buy 1000 shares @ $100.
    # On 2025-07-02: buy 1000 shares @ $100 (so SPY price=100 on 2025-07-02!).
    # Wait: if SPY price at 2025-07-02 is $100, we own 2000 shares @ $100 = $200,000.
    # For 20% exact IRR: end_equity = 100000*(1.2**(365/365.25)) + 100000*(1.2**(183/365.25))
    target_end_equity = initial * (1.2 ** (365 / 365.25)) + contrib * (1.2 ** (183 / 365.25))
    # On 2025-07-02, contribution happens -> 1000 shares held + 1000 bought = 2000 shares; on 2026-01-01 no contrib
    #Wait: 2026-01-01 is a new month, so monthly contribution would trigger on 2026-01-01! Use quarterly? No, quarterly triggers on 2026-01-01 too!
    # Instead let end date be 2025-12-31 (same month/quarter as Oct, only Jul 2 triggered? Wait: Feb..Dec all trigger monthly).
    # Use quarterly contributions: 2025-01-01 (init), 2025-04-01 (Q2), 2025-07-01 (Q3), 2025-10-01 (Q4), end 2025-12-31!
    q_dates = [
        date(2025, 1, 1),
        date(2025, 4, 1),
        date(2025, 7, 1),
        date(2025, 10, 1),
        date(2025, 12, 31),
    ]
    end_price = (
        initial * (1.2 ** (364 / 365.25))
        + contrib * (1.2 ** (274 / 365.25))
        + contrib * (1.2 ** (183 / 365.25))
        + contrib * (1.2 ** (91 / 365.25))
    ) / 4_000.0
    bars = {"SPY": _bars("SPY", q_dates, [100, 100, 100, 100, end_price], [100, 100, 100, 100, 100])}
    cfg = _config(
        initial_cash=initial,
        periodic_contribution=contrib,
        contribution_frequency="quarterly",
    )

    result = run_index_backtest(bars, PortfolioRule("q_dca", {"SPY": 1.0}, "buy_and_hold"), config=cfg)

    assert result.total_contributed == 400_000.0
    assert result.mwr_xirr_pct == pytest.approx(20.0, abs=0.05)


# ── Sub-period analytics & serialization tests ──────────────────────────────────


def test_annual_breakdown_adjusts_for_cross_year_contributions():
    curve = [
        {"date": date(2024, 12, 30), "equity": 1_000.0, "cash": 0.0, "gross_exposure": 1_000.0},
        {"date": date(2024, 12, 31), "equity": 1_100.0, "cash": 0.0, "gross_exposure": 1_100.0},
        {"date": date(2025, 1, 2), "equity": 2_100.0, "cash": 0.0, "gross_exposure": 2_100.0, "contribution": 1_000.0},
        {"date": date(2025, 12, 31), "equity": 2_310.0, "cash": 0.0, "gross_exposure": 2_310.0},
    ]

    annual = compute_annual_breakdown(curve)

    assert len(annual) == 2
    assert annual[0]["year"] == 2024
    assert annual[0]["return_pct"] == 10.0
    assert annual[0]["start_date"] == date(2024, 12, 30)
    assert annual[0]["end_date"] == date(2024, 12, 31)
    assert annual[0]["avg_exposure_pct"] == 100.0

    # Jan 2 deposit of $1000 made equity 1100+1000=2100 (0% return that day), then +10% to 2310!
    assert annual[1]["year"] == 2025
    assert annual[1]["return_pct"] == 10.0
    assert annual[1]["max_drawdown_pct"] == 0.0


def test_stress_periods_filters_minimum_five_overlapping_days():
    # 7 days inside 2020 COVID (2020-02-19..2020-02-27), only 3 days inside 2022 Bear
    covid_dates = [date(2020, 2, 19) + timedelta(days=i) for i in range(7)]
    bear_dates = [date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 5)]
    curve = [
        {
            "date": d,
            "equity": 1_000.0 - idx * 50.0,
            "cash": 100.0,
            "gross_exposure": 900.0 - idx * 50.0,
        }
        for idx, d in enumerate(covid_dates)
    ] + [
        {
            "date": d,
            "equity": 800.0,
            "cash": 0.0,
            "gross_exposure": 800.0,
        }
        for d in bear_dates
    ]

    stress = compute_stress_periods(curve, windows=STRESS_WINDOWS)

    assert len(stress) == 1
    assert stress[0]["name"] == "2020 COVID"
    assert stress[0]["trading_days"] == 7
    assert stress[0]["return_pct"] == -30.0
    assert stress[0]["max_drawdown_pct"] == -30.0


def test_rolling_windows_computes_distribution_and_benchmark_win_rate():
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
    # 3-step windows: [0->3], [1->4], [2->5]
    # Strategy rises steadily: +10%, +10%, +10%, -5%, +20%
    strat_equities = [100.0, 110.0, 121.0, 133.1, 126.445, 151.734]
    bench_equities = [100.0, 105.0, 110.25, 115.7625, 130.0, 140.0]

    strat_curve = [
        {"date": d, "equity": eq, "cash": 0.0, "gross_exposure": eq}
        for d, eq in zip(dates, strat_equities)
    ]
    bench_curve = [
        {"date": d, "equity": eq, "cash": 0.0, "gross_exposure": eq}
        for d, eq in zip(dates, bench_equities)
    ]

    rolling = compute_rolling_windows(strat_curve, benchmark_curve=bench_curve, window_days=3)
    # Ratios:
    # [0->3]: strat 1.331 vs bench 1.157625 (beat)
    # [1->4]: strat 126.445/110=1.1495 vs bench 130/105=1.2381 (lose)
    # [2->5]: strat 151.734/121=1.254 vs bench 140/110.25=1.2698 (lose)
    assert rolling["sample_count"] == 3
    assert rolling["positive_rate_pct"] == 100.0
    assert rolling["beat_benchmark_rate_pct"] == pytest.approx(33.33, abs=0.01)

    short = compute_rolling_windows(strat_curve, benchmark_curve=bench_curve, window_days=10)
    assert short["sample_count"] == 0
    assert short["beat_benchmark_rate_pct"] == 0.0


def test_result_to_dict_serializes_nested_dates_for_json():
    dates = [date(2020, 2, 18) + timedelta(days=i) for i in range(10)]
    bars = {"SPY": _bars("SPY", dates, [100 - i for i in range(10)])}
    result = run_index_backtest(bars, PortfolioRule("hold", {"SPY": 1.0}, "buy_and_hold"), config=_config())

    assert len(result.annual_breakdown) == 1
    assert len(result.stress_periods) == 1
    serialized = json.dumps(result.to_dict())
    parsed = json.loads(serialized)
    assert parsed["annual_breakdown"][0]["start_date"] == "2020-02-18"
    assert parsed["stress_periods"][0]["name"] == "2020 COVID"
