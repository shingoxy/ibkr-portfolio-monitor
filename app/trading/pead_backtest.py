from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from math import floor, sqrt
from statistics import pstdev, stdev
from typing import Any, Sequence

from app.trading.models import Bar
from app.trading.pead_strategy import (
    EarningsEvent,
    PEADCandidate,
    PEADCandidateEvaluation,
    PEADConfig,
    evaluate_pead_candidate,
    resolve_entry_bar_index,
)

DEFAULT_UNIVERSE_NOTE = (
    "Static local universe without delisted stocks; survivorship bias may overstate "
    "long-only PEAD performance."
)


@dataclass(frozen=True)
class PEADBacktestConfig:
    """Execution, transaction cost, and T+1 cash account constraints for PEAD."""

    initial_cash: float = 11_000.0
    commission_per_order: float = 1.0
    sec_fee_rate: float = 0.0000278
    finra_taf_per_share: float = 0.000166
    finra_taf_max_per_order: float = 8.30
    slippage_bps: float = 20.0
    max_positions: int = 4
    max_symbol_exposure_pct: float = 0.25
    max_total_exposure_pct: float = 0.80
    min_cash_reserve_pct: float = 0.10
    enforce_t_plus_1_settled_cash: bool = True
    universe_note: str = DEFAULT_UNIVERSE_NOTE

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.commission_per_order < 0:
            raise ValueError("commission_per_order must be non-negative")
        if self.sec_fee_rate < 0:
            raise ValueError("sec_fee_rate must be non-negative")
        if self.finra_taf_per_share < 0:
            raise ValueError("finra_taf_per_share must be non-negative")
        if self.finra_taf_max_per_order < 0:
            raise ValueError("finra_taf_max_per_order must be non-negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least 1")
        if not 0 < self.max_symbol_exposure_pct <= 1.0:
            raise ValueError("max_symbol_exposure_pct must be in (0, 1]")
        if not 0 < self.max_total_exposure_pct <= 1.0:
            raise ValueError("max_total_exposure_pct must be in (0, 1]")
        if not 0 <= self.min_cash_reserve_pct < 1.0:
            raise ValueError("min_cash_reserve_pct must be in [0, 1)")


@dataclass
class PEADTrade:
    """Closed PEAD trade with itemized execution costs and attribution."""

    symbol: str
    announcement_date: date
    session: str
    entry_date: date
    exit_date: date
    quantity: int
    raw_entry_price: float
    entry_price: float
    raw_exit_price: float
    exit_price: float
    entry_commission: float
    exit_commission: float
    sec_fee: float
    finra_taf_fee: float
    slippage_cost: float
    total_fees: float
    pnl: float
    return_pct: float
    eps_surprise_pct: float
    revenue_surprise_pct: float | None
    gap_pct: float
    bars_held: int
    exit_reason: str
    used_trades_prices: bool = False


@dataclass
class PEADPeriodMetrics:
    """Performance, benchmark comparison, and robustness metrics for a sample slice."""

    start_date: date | None
    end_date: date | None
    initial_equity: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    has_benchmark: bool
    benchmark_total_return_pct: float
    benchmark_cagr_pct: float
    benchmark_max_drawdown_pct: float
    benchmark_sharpe_ratio: float
    excess_return_vs_spy_pct: float
    closed_trades: int
    win_rate_pct: float
    realized_pnl: float
    avg_trade_return_pct: float
    pnl_excluding_top3: float
    return_pct_excluding_top3: float
    trade_t_statistic: float
    total_commissions: float
    total_sec_fees: float
    total_finra_taf_fees: float
    total_slippage_cost: float
    total_costs: float


@dataclass
class PEADKillCriteriaResult:
    """Quantitative evaluation of NO2_EXECUTION_PLAN.md Section 5 kill rules."""

    killed: bool
    status: str
    evaluated_scope: str
    slippage_bps: float
    triggered_rules: list[str]
    reasons: list[str]
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PEADPosition:
    symbol: str
    announcement_date: date
    session: str
    entry_date: date
    quantity: int
    raw_entry_price: float
    entry_price: float
    entry_commission: float
    entry_slippage_cost: float
    stop_price: float
    eps_surprise_pct: float
    revenue_surprise_pct: float | None
    gap_pct: float
    used_trades_prices: bool
    bars_held: int = 0


@dataclass
class PEADBacktestResult:
    """Full PEAD backtest output including Dev/Holdout splits, audits, and kill status."""

    start_date: date
    end_date: date
    initial_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    has_benchmark: bool
    benchmark_total_return_pct: float
    benchmark_cagr_pct: float
    benchmark_max_drawdown_pct: float
    benchmark_sharpe_ratio: float
    excess_return_vs_spy_pct: float
    closed_trades: int
    win_rate_pct: float
    realized_pnl: float
    avg_trade_return_pct: float
    pnl_excluding_top3: float
    return_pct_excluding_top3: float
    trade_t_statistic: float
    slippage_bps: float
    total_commissions: float
    total_sec_fees: float
    total_finra_taf_fees: float
    total_slippage_cost: float
    total_costs: float
    total_events_evaluated: int
    accepted_candidates_count: int
    rejected_candidates_count: int
    rejection_reason_counts: dict[str, int]
    blocked_by_unsettled_cash_count: int
    blocked_by_max_positions_count: int
    blocked_by_exposure_or_reserve_count: int
    skipped_existing_position_count: int
    holdout_start_date: date | None
    dev_metrics: PEADPeriodMetrics | None
    holdout_metrics: PEADPeriodMetrics | None
    price_discrepancy_audit: dict[str, Any]
    survivorship_bias_disclosure: str
    universe_note: str
    kill_criteria: PEADKillCriteriaResult
    equity_curve: list[dict[str, Any]]
    trades: list[PEADTrade]
    open_positions: list[dict[str, Any]]
    evaluations: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        _serialize_dates_in_place(payload)
        return payload


@dataclass
class PEADSlippageScenario:
    """Single slippage scenario in a stress-test matrix."""

    slippage_bps: float
    result: PEADBacktestResult
    kill_criteria: PEADKillCriteriaResult


@dataclass
class PEADSlippageMatrixResult:
    """Results across the PEAD slippage stress grid (e.g. 10 / 20 / 30 bps)."""

    slippage_grid: list[float]
    runs: list[PEADSlippageScenario]

    @property
    def results_by_bps(self) -> dict[float, PEADBacktestResult]:
        return {run.slippage_bps: run.result for run in self.runs}

    def to_dict(self) -> dict[str, Any]:
        return {
            "slippage_grid": list(self.slippage_grid),
            "runs": [
                {
                    "slippage_bps": run.slippage_bps,
                    "kill_criteria": asdict(run.kill_criteria),
                    "result": run.result.to_dict(),
                }
                for run in self.runs
            ],
        }


def run_pead_backtest(
    events: Sequence[EarningsEvent],
    bars_by_symbol: dict[str, list[Bar]],
    strategy_config: PEADConfig | None = None,
    backtest_config: PEADBacktestConfig | None = None,
    trades_bars_by_symbol: dict[str, list[Bar]] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    holdout_start_date: date | None = None,
) -> PEADBacktestResult:
    """Run a lookahead-free, T+1 SettledCash-constrained long-only PEAD backtest."""
    strategy_config = strategy_config or PEADConfig()
    backtest_config = backtest_config or PEADBacktestConfig()

    normalized_adj = _normalize_bars(bars_by_symbol)
    if not normalized_adj:
        raise ValueError("bars_by_symbol must contain at least one bar")
    normalized_trades = _normalize_bars(trades_bars_by_symbol or {})

    adj_by_symbol_date = {
        symbol: {bar.date: bar for bar in bars}
        for symbol, bars in normalized_adj.items()
    }
    trades_by_symbol_date = {
        symbol: {bar.date: bar for bar in bars}
        for symbol, bars in normalized_trades.items()
    }
    exec_by_symbol_date: dict[str, dict[date, Bar]] = {}
    for symbol in set(adj_by_symbol_date) | set(trades_by_symbol_date):
        exec_by_symbol_date[symbol] = (
            trades_by_symbol_date[symbol]
            if symbol in trades_by_symbol_date
            else adj_by_symbol_date[symbol]
        )

    all_dates = sorted(
        {
            bar_date
            for symbol_map in adj_by_symbol_date.values()
            for bar_date in symbol_map
        }
        | {
            bar_date
            for symbol_map in trades_by_symbol_date.values()
            for bar_date in symbol_map
        }
    )
    sim_dates = [
        bar_date
        for bar_date in all_dates
        if (start_date is None or bar_date >= start_date)
        and (end_date is None or bar_date <= end_date)
    ]
    if not sim_dates:
        raise ValueError("no trading dates fall within [start_date, end_date]")

    spy_bars = normalized_adj.get("SPY")

    evaluations: list[PEADCandidateEvaluation] = []
    candidates_by_date: dict[date, list[PEADCandidate]] = {}
    rejection_counts: dict[str, int] = {}
    event_audit_rows: list[dict[str, Any]] = []

    for event in sorted(events, key=lambda item: (item.announcement_date, item.symbol)):
        sym_adj_bars = normalized_adj.get(event.symbol)
        if not sym_adj_bars:
            rejection_counts["missing_symbol_bars"] = rejection_counts.get("missing_symbol_bars", 0) + 1
            evaluations.append(
                PEADCandidateEvaluation(
                    accepted=False,
                    symbol=event.symbol,
                    announcement_date=event.announcement_date,
                    session=event.session,
                    rejection_reason="missing_symbol_bars",
                    rejection_reasons=["missing_symbol_bars"],
                )
            )
            continue

        sym_trades_bars = normalized_trades.get(event.symbol)
        evaluation = evaluate_pead_candidate(
            event=event,
            bars=sym_adj_bars,
            spy_bars=spy_bars,
            config=strategy_config,
            trades_bars=sym_trades_bars,
        )

        if sym_trades_bars is not None:
            adj_eval = evaluate_pead_candidate(
                event=event,
                bars=sym_adj_bars,
                spy_bars=spy_bars,
                config=strategy_config,
                trades_bars=None,
            )
            audit_row = _build_event_price_audit_row(
                event=event,
                sym_adj_bars=sym_adj_bars,
                sym_trades_bars=sym_trades_bars,
                adj_eval=adj_eval,
                trades_eval=evaluation,
            )
            if audit_row is not None:
                event_audit_rows.append(audit_row)

        if evaluation.entry_date is not None:
            if start_date is not None and evaluation.entry_date < start_date:
                continue
            if end_date is not None and evaluation.entry_date > end_date:
                continue

        evaluations.append(evaluation)
        if evaluation.accepted and evaluation.candidate is not None:
            candidates_by_date.setdefault(evaluation.candidate.entry_date, []).append(
                evaluation.candidate
            )
        elif evaluation.rejection_reason:
            reason = evaluation.rejection_reason
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    price_discrepancy_audit = _build_price_discrepancy_audit(
        adj_by_symbol_date=adj_by_symbol_date,
        trades_by_symbol_date=trades_by_symbol_date,
        event_audit_rows=event_audit_rows,
    )

    settled_cash = float(backtest_config.initial_cash)
    unsettled_entries: list[tuple[date, float]] = []
    positions: dict[str, _PEADPosition] = {}
    last_prices: dict[str, float] = {}
    trades: list[PEADTrade] = []
    equity_curve: list[dict[str, Any]] = []

    blocked_by_unsettled_cash = 0
    blocked_by_max_positions = 0
    blocked_by_exposure_or_reserve = 0
    skipped_existing_position = 0

    for day_idx, current_date in enumerate(sim_dates):
        # 1. Morning T+1 settlement of prior sales maturing today or earlier
        if unsettled_entries:
            remaining_unsettled: list[tuple[date, float]] = []
            for settle_date, amount in unsettled_entries:
                if settle_date <= current_date:
                    settled_cash += amount
                else:
                    remaining_unsettled.append((settle_date, amount))
            unsettled_entries = remaining_unsettled

        next_settlement_date = (
            sim_dates[day_idx + 1]
            if day_idx + 1 < len(sim_dates)
            else current_date + timedelta(days=1)
        )

        day_exec_bars = {
            symbol: symbol_bars[current_date]
            for symbol, symbol_bars in exec_by_symbol_date.items()
            if current_date in symbol_bars
        }
        open_prices = {symbol: bar.open for symbol, bar in day_exec_bars.items()}

        # 2. Morning exits at open: time stop (bars_held >= holding_days) or gap-through stop
        for symbol in list(positions):
            bar = day_exec_bars.get(symbol)
            if bar is None:
                continue
            position = positions[symbol]
            exit_reason: str | None = None
            if position.bars_held >= strategy_config.holding_days:
                exit_reason = "holding_period_exit"
            elif position.stop_price > 0 and bar.open <= position.stop_price:
                exit_reason = "stop_loss_gap_open"

            if exit_reason is not None:
                settled_cash = _close_position(
                    symbol=symbol,
                    position=position,
                    raw_exit_price=bar.open,
                    exit_date=current_date,
                    exit_reason=exit_reason,
                    backtest_config=backtest_config,
                    settled_cash=settled_cash,
                    unsettled_entries=unsettled_entries,
                    next_settlement_date=next_settlement_date,
                    positions=positions,
                    trades=trades,
                )

        # 3. Morning entries at open (deterministic priority by EPS surprise, gap, symbol)
        day_candidates = sorted(
            candidates_by_date.get(current_date, []),
            key=lambda item: (-item.eps_surprise_pct, -item.gap_pct, item.symbol),
        )
        for candidate in day_candidates:
            symbol = candidate.symbol
            if symbol in positions:
                skipped_existing_position += 1
                continue
            if len(positions) >= backtest_config.max_positions:
                blocked_by_max_positions += 1
                continue

            unsettled_cash = sum(amount for _, amount in unsettled_entries)
            total_cash = settled_cash + unsettled_cash
            equity, gross_exposure = _portfolio_value(
                total_cash=total_cash,
                positions=positions,
                current_prices=open_prices,
                last_prices=last_prices,
            )

            raw_open = candidate.entry_open
            entry_fill_price = raw_open * (1.0 + backtest_config.slippage_bps / 10_000.0)
            symbol_cap = floor((equity * backtest_config.max_symbol_exposure_pct) / entry_fill_price)
            total_room = max(0.0, equity * backtest_config.max_total_exposure_pct - gross_exposure)
            total_cap = floor(total_room / entry_fill_price)
            reserve = equity * backtest_config.min_cash_reserve_pct

            spendable_settled = max(
                0.0, settled_cash - reserve - backtest_config.commission_per_order
            )
            spendable_total = max(
                0.0, total_cash - reserve - backtest_config.commission_per_order
            )
            effective_spendable = (
                spendable_settled
                if backtest_config.enforce_t_plus_1_settled_cash
                else spendable_total
            )
            cash_cap = floor(effective_spendable / entry_fill_price)
            total_cash_cap = floor(spendable_total / entry_fill_price)

            quantity = min(symbol_cap, total_cap, cash_cap)
            if quantity <= 0:
                would_have_qty = min(symbol_cap, total_cap, total_cash_cap)
                if backtest_config.enforce_t_plus_1_settled_cash and would_have_qty > 0:
                    blocked_by_unsettled_cash += 1
                else:
                    blocked_by_exposure_or_reserve += 1
                continue

            entry_commission = backtest_config.commission_per_order
            settled_cash -= quantity * entry_fill_price + entry_commission
            entry_slippage = quantity * (entry_fill_price - raw_open)
            stop_price = (
                entry_fill_price * (1.0 - strategy_config.stop_loss_pct)
                if strategy_config.stop_loss_pct > 0
                else 0.0
            )
            positions[symbol] = _PEADPosition(
                symbol=symbol,
                announcement_date=candidate.announcement_date,
                session=candidate.session.value,
                entry_date=current_date,
                quantity=quantity,
                raw_entry_price=raw_open,
                entry_price=entry_fill_price,
                entry_commission=entry_commission,
                entry_slippage_cost=entry_slippage,
                stop_price=stop_price,
                eps_surprise_pct=candidate.eps_surprise_pct,
                revenue_surprise_pct=candidate.revenue_surprise_pct,
                gap_pct=candidate.gap_pct,
                used_trades_prices=candidate.used_trades_prices,
                bars_held=0,
            )

        # 4. Intraday stop-loss check & holding-day increment on active positions
        for symbol in list(positions):
            bar = day_exec_bars.get(symbol)
            if bar is None:
                continue
            position = positions[symbol]
            last_prices[symbol] = bar.close
            if position.stop_price > 0 and bar.low <= position.stop_price:
                settled_cash = _close_position(
                    symbol=symbol,
                    position=position,
                    raw_exit_price=position.stop_price,
                    exit_date=current_date,
                    exit_reason="stop_loss_intraday",
                    backtest_config=backtest_config,
                    settled_cash=settled_cash,
                    unsettled_entries=unsettled_entries,
                    next_settlement_date=next_settlement_date,
                    positions=positions,
                    trades=trades,
                )
            else:
                position.bars_held += 1

        for symbol, bar in day_exec_bars.items():
            last_prices[symbol] = bar.close

        close_prices = {symbol: bar.close for symbol, bar in day_exec_bars.items()}
        unsettled_cash = sum(amount for _, amount in unsettled_entries)
        total_cash = settled_cash + unsettled_cash
        equity, exposure = _portfolio_value(
            total_cash=total_cash,
            positions=positions,
            current_prices=close_prices,
            last_prices=last_prices,
        )
        equity_curve.append(
            {
                "date": current_date,
                "equity": round(equity, 2),
                "cash": round(total_cash, 2),
                "settled_cash": round(settled_cash, 2),
                "unsettled_cash": round(unsettled_cash, 2),
                "gross_exposure": round(exposure, 2),
            }
        )

    return _assemble_backtest_result(
        backtest_config=backtest_config,
        spy_bars=spy_bars or [],
        equity_curve=equity_curve,
        trades=trades,
        positions=positions,
        evaluations=evaluations,
        rejection_counts=rejection_counts,
        blocked_by_unsettled_cash=blocked_by_unsettled_cash,
        blocked_by_max_positions=blocked_by_max_positions,
        blocked_by_exposure_or_reserve=blocked_by_exposure_or_reserve,
        skipped_existing_position=skipped_existing_position,
        holdout_start_date=holdout_start_date,
        price_discrepancy_audit=price_discrepancy_audit,
    )


def run_pead_slippage_matrix(
    events: Sequence[EarningsEvent],
    bars_by_symbol: dict[str, list[Bar]],
    strategy_config: PEADConfig | None = None,
    backtest_config: PEADBacktestConfig | None = None,
    trades_bars_by_symbol: dict[str, list[Bar]] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    holdout_start_date: date | None = None,
    slippage_grid: Sequence[float] = (10.0, 20.0, 30.0),
) -> PEADSlippageMatrixResult:
    """Run PEAD backtests across a slippage stress matrix (default 10 / 20 / 30 bps)."""
    base_config = backtest_config or PEADBacktestConfig()
    grid = [float(bps) for bps in slippage_grid]
    if not grid:
        raise ValueError("slippage_grid must not be empty")

    runs: list[PEADSlippageScenario] = []
    for bps in grid:
        scenario_config = replace(base_config, slippage_bps=bps)
        result = run_pead_backtest(
            events=events,
            bars_by_symbol=bars_by_symbol,
            strategy_config=strategy_config,
            backtest_config=scenario_config,
            trades_bars_by_symbol=trades_bars_by_symbol,
            start_date=start_date,
            end_date=end_date,
            holdout_start_date=holdout_start_date,
        )
        kill_result = evaluate_pead_kill_criteria(result, slippage_bps=bps)
        runs.append(
            PEADSlippageScenario(
                slippage_bps=bps,
                result=result,
                kill_criteria=kill_result,
            )
        )
    return PEADSlippageMatrixResult(slippage_grid=grid, runs=runs)


def evaluate_pead_kill_criteria(
    result: PEADBacktestResult,
    slippage_bps: float | None = None,
) -> PEADKillCriteriaResult:
    """Evaluate NO2_EXECUTION_PLAN.md Section 5 quantitative kill criteria."""
    effective_slippage = result.slippage_bps if slippage_bps is None else float(slippage_bps)
    if result.holdout_metrics is not None:
        scope = "holdout"
        metrics: PEADPeriodMetrics | PEADBacktestResult = result.holdout_metrics
    else:
        scope = "full_sample"
        metrics = result

    triggered_rules: list[str] = []
    reasons: list[str] = []

    if metrics.sharpe_ratio < 1.0:
        triggered_rules.append("sharpe_below_1.0")
        reasons.append(
            f"{scope} Sharpe ratio {metrics.sharpe_ratio:.3f} < 1.000 at "
            f"{effective_slippage:.1f} bps slippage"
        )

    if (
        metrics.has_benchmark
        and metrics.total_return_pct < metrics.benchmark_total_return_pct
        and metrics.max_drawdown_pct <= metrics.benchmark_max_drawdown_pct
    ):
        triggered_rules.append("underperforms_spy_without_drawdown_reduction")
        reasons.append(
            f"{scope} return {metrics.total_return_pct:.2f}% trails SPY "
            f"{metrics.benchmark_total_return_pct:.2f}% without reducing drawdown "
            f"({metrics.max_drawdown_pct:.2f}% vs SPY {metrics.benchmark_max_drawdown_pct:.2f}%)"
        )

    if metrics.pnl_excluding_top3 <= 0.0:
        triggered_rules.append("pnl_excluding_top3_non_positive")
        reasons.append(
            f"{scope} realized PnL excluding top 3 trades is ${metrics.pnl_excluding_top3:.2f} (<= $0.00)"
        )

    killed = bool(triggered_rules)
    return PEADKillCriteriaResult(
        killed=killed,
        status="KILL" if killed else "PASS",
        evaluated_scope=scope,
        slippage_bps=effective_slippage,
        triggered_rules=triggered_rules,
        reasons=reasons,
        metrics_snapshot={
            "sharpe_ratio": metrics.sharpe_ratio,
            "total_return_pct": metrics.total_return_pct,
            "max_drawdown_pct": metrics.max_drawdown_pct,
            "benchmark_total_return_pct": metrics.benchmark_total_return_pct,
            "benchmark_max_drawdown_pct": metrics.benchmark_max_drawdown_pct,
            "pnl_excluding_top3": metrics.pnl_excluding_top3,
            "return_pct_excluding_top3": metrics.return_pct_excluding_top3,
            "trade_t_statistic": metrics.trade_t_statistic,
            "closed_trades": metrics.closed_trades,
        },
    )


def _normalize_bars(bars_by_symbol: dict[str, list[Bar]]) -> dict[str, list[Bar]]:
    normalized: dict[str, list[Bar]] = {}
    for symbol, bars in bars_by_symbol.items():
        if not bars:
            continue
        by_date = {bar.date: bar for bar in sorted(bars, key=lambda item: item.date)}
        normalized[symbol.strip().upper()] = [by_date[key] for key in sorted(by_date)]
    return normalized


def _portfolio_value(
    total_cash: float,
    positions: dict[str, _PEADPosition],
    current_prices: dict[str, float],
    last_prices: dict[str, float],
) -> tuple[float, float]:
    exposure = sum(
        position.quantity * current_prices.get(symbol, last_prices.get(symbol, position.entry_price))
        for symbol, position in positions.items()
    )
    return total_cash + exposure, exposure


def _close_position(
    symbol: str,
    position: _PEADPosition,
    raw_exit_price: float,
    exit_date: date,
    exit_reason: str,
    backtest_config: PEADBacktestConfig,
    settled_cash: float,
    unsettled_entries: list[tuple[date, float]],
    next_settlement_date: date,
    positions: dict[str, _PEADPosition],
    trades: list[PEADTrade],
) -> float:
    positions.pop(symbol)
    exit_fill_price = raw_exit_price * (1.0 - backtest_config.slippage_bps / 10_000.0)
    gross_proceeds = position.quantity * exit_fill_price
    exit_commission = backtest_config.commission_per_order
    sec_fee = gross_proceeds * backtest_config.sec_fee_rate
    taf_fee = min(
        position.quantity * backtest_config.finra_taf_per_share,
        backtest_config.finra_taf_max_per_order,
    )
    net_proceeds = gross_proceeds - exit_commission - sec_fee - taf_fee
    if backtest_config.enforce_t_plus_1_settled_cash and net_proceeds >= 0:
        unsettled_entries.append((next_settlement_date, net_proceeds))
    else:
        settled_cash += net_proceeds

    entry_cost = position.quantity * position.entry_price + position.entry_commission
    pnl = net_proceeds - entry_cost
    exit_slippage = position.quantity * (raw_exit_price - exit_fill_price)
    slippage_cost = position.entry_slippage_cost + exit_slippage
    total_fees = position.entry_commission + exit_commission + sec_fee + taf_fee

    trades.append(
        PEADTrade(
            symbol=symbol,
            announcement_date=position.announcement_date,
            session=position.session,
            entry_date=position.entry_date,
            exit_date=exit_date,
            quantity=position.quantity,
            raw_entry_price=round(position.raw_entry_price, 4),
            entry_price=round(position.entry_price, 6),
            raw_exit_price=round(raw_exit_price, 4),
            exit_price=round(exit_fill_price, 6),
            entry_commission=round(position.entry_commission, 6),
            exit_commission=round(exit_commission, 6),
            sec_fee=round(sec_fee, 6),
            finra_taf_fee=round(taf_fee, 6),
            slippage_cost=round(slippage_cost, 6),
            total_fees=round(total_fees, 6),
            pnl=round(pnl, 4),
            return_pct=round((pnl / entry_cost) * 100.0, 4) if entry_cost else 0.0,
            eps_surprise_pct=position.eps_surprise_pct,
            revenue_surprise_pct=position.revenue_surprise_pct,
            gap_pct=position.gap_pct,
            bars_held=position.bars_held,
            exit_reason=exit_reason,
            used_trades_prices=position.used_trades_prices,
        )
    )
    return settled_cash


def _assemble_backtest_result(
    backtest_config: PEADBacktestConfig,
    spy_bars: list[Bar],
    equity_curve: list[dict[str, Any]],
    trades: list[PEADTrade],
    positions: dict[str, _PEADPosition],
    evaluations: list[PEADCandidateEvaluation],
    rejection_counts: dict[str, int],
    blocked_by_unsettled_cash: int,
    blocked_by_max_positions: int,
    blocked_by_exposure_or_reserve: int,
    skipped_existing_position: int,
    holdout_start_date: date | None,
    price_discrepancy_audit: dict[str, Any],
) -> PEADBacktestResult:
    full_metrics = _compute_period_metrics(
        curve=equity_curve,
        trades=trades,
        spy_bars=spy_bars,
        initial_equity=backtest_config.initial_cash,
        previous_equity=backtest_config.initial_cash,
        anchor_spy_close=None,
    )

    dev_metrics: PEADPeriodMetrics | None = None
    holdout_metrics: PEADPeriodMetrics | None = None
    if holdout_start_date is not None:
        dev_curve = [point for point in equity_curve if point["date"] < holdout_start_date]
        holdout_curve = [point for point in equity_curve if point["date"] >= holdout_start_date]
        dev_trades = [trade for trade in trades if trade.entry_date < holdout_start_date]
        holdout_trades = [trade for trade in trades if trade.entry_date >= holdout_start_date]

        dev_metrics = _compute_period_metrics(
            curve=dev_curve,
            trades=dev_trades,
            spy_bars=spy_bars,
            initial_equity=backtest_config.initial_cash,
            previous_equity=backtest_config.initial_cash,
            anchor_spy_close=None,
        )
        holdout_base_equity = (
            dev_curve[-1]["equity"] if dev_curve else backtest_config.initial_cash
        )
        anchor_spy_close: float | None = None
        if dev_curve and spy_bars:
            prior_spy = [bar for bar in spy_bars if bar.date <= dev_curve[-1]["date"]]
            if prior_spy:
                anchor_spy_close = prior_spy[-1].close
        holdout_metrics = _compute_period_metrics(
            curve=holdout_curve,
            trades=holdout_trades,
            spy_bars=spy_bars,
            initial_equity=holdout_base_equity,
            previous_equity=holdout_base_equity,
            anchor_spy_close=anchor_spy_close,
            include_first_return=bool(dev_curve),
        )

    open_positions = [
        {
            "symbol": pos.symbol,
            "entry_date": pos.entry_date,
            "quantity": pos.quantity,
            "entry_price": round(pos.entry_price, 4),
            "stop_price": round(pos.stop_price, 4),
            "bars_held": pos.bars_held,
        }
        for pos in sorted(positions.values(), key=lambda item: item.symbol)
    ]

    accepted_count = sum(1 for item in evaluations if item.accepted)
    rejected_count = sum(1 for item in evaluations if not item.accepted)
    eval_summaries = [
        {
            "symbol": item.symbol,
            "announcement_date": item.announcement_date.isoformat(),
            "session": item.session.value,
            "entry_date": item.entry_date.isoformat() if item.entry_date else None,
            "accepted": item.accepted,
            "rejection_reason": item.rejection_reason,
            "rejection_reasons": list(item.rejection_reasons),
            "details": dict(item.details),
        }
        for item in evaluations
    ]

    survivorship_disclosure = (
        f"SURVIVORSHIP BIAS DISCLOSURE: {backtest_config.universe_note} "
        "Unless delisted equities are explicitly included in the input universe and event "
        "feed, historical backtests of long-only post-earnings drift overstate true "
        "out-of-sample returns and Sharpe ratios."
    )

    preliminary = PEADBacktestResult(
        start_date=equity_curve[0]["date"],
        end_date=equity_curve[-1]["date"],
        initial_cash=backtest_config.initial_cash,
        final_equity=full_metrics.final_equity,
        total_return_pct=full_metrics.total_return_pct,
        cagr_pct=full_metrics.cagr_pct,
        max_drawdown_pct=full_metrics.max_drawdown_pct,
        annualized_volatility_pct=full_metrics.annualized_volatility_pct,
        sharpe_ratio=full_metrics.sharpe_ratio,
        has_benchmark=full_metrics.has_benchmark,
        benchmark_total_return_pct=full_metrics.benchmark_total_return_pct,
        benchmark_cagr_pct=full_metrics.benchmark_cagr_pct,
        benchmark_max_drawdown_pct=full_metrics.benchmark_max_drawdown_pct,
        benchmark_sharpe_ratio=full_metrics.benchmark_sharpe_ratio,
        excess_return_vs_spy_pct=full_metrics.excess_return_vs_spy_pct,
        closed_trades=full_metrics.closed_trades,
        win_rate_pct=full_metrics.win_rate_pct,
        realized_pnl=full_metrics.realized_pnl,
        avg_trade_return_pct=full_metrics.avg_trade_return_pct,
        pnl_excluding_top3=full_metrics.pnl_excluding_top3,
        return_pct_excluding_top3=full_metrics.return_pct_excluding_top3,
        trade_t_statistic=full_metrics.trade_t_statistic,
        slippage_bps=backtest_config.slippage_bps,
        total_commissions=full_metrics.total_commissions,
        total_sec_fees=full_metrics.total_sec_fees,
        total_finra_taf_fees=full_metrics.total_finra_taf_fees,
        total_slippage_cost=full_metrics.total_slippage_cost,
        total_costs=full_metrics.total_costs,
        total_events_evaluated=len(evaluations),
        accepted_candidates_count=accepted_count,
        rejected_candidates_count=rejected_count,
        rejection_reason_counts=dict(sorted(rejection_counts.items())),
        blocked_by_unsettled_cash_count=blocked_by_unsettled_cash,
        blocked_by_max_positions_count=blocked_by_max_positions,
        blocked_by_exposure_or_reserve_count=blocked_by_exposure_or_reserve,
        skipped_existing_position_count=skipped_existing_position,
        holdout_start_date=holdout_start_date,
        dev_metrics=dev_metrics,
        holdout_metrics=holdout_metrics,
        price_discrepancy_audit=price_discrepancy_audit,
        survivorship_bias_disclosure=survivorship_disclosure,
        universe_note=backtest_config.universe_note,
        kill_criteria=PEADKillCriteriaResult(
            killed=False,
            status="PENDING",
            evaluated_scope="full_sample",
            slippage_bps=backtest_config.slippage_bps,
            triggered_rules=[],
            reasons=[],
        ),
        equity_curve=equity_curve,
        trades=trades,
        open_positions=open_positions,
        evaluations=eval_summaries,
    )
    preliminary.kill_criteria = evaluate_pead_kill_criteria(
        preliminary, slippage_bps=backtest_config.slippage_bps
    )
    return preliminary


def _compute_period_metrics(
    curve: list[dict[str, Any]],
    trades: list[PEADTrade],
    spy_bars: list[Bar],
    initial_equity: float,
    previous_equity: float,
    anchor_spy_close: float | None,
    include_first_return: bool = False,
) -> PEADPeriodMetrics:
    total_commissions = sum(t.entry_commission + t.exit_commission for t in trades)
    total_sec_fees = sum(t.sec_fee for t in trades)
    total_finra_taf_fees = sum(t.finra_taf_fee for t in trades)
    total_slippage_cost = sum(t.slippage_cost for t in trades)
    total_costs = total_commissions + total_sec_fees + total_finra_taf_fees + total_slippage_cost

    trade_returns = [t.return_pct for t in trades]
    winners = sum(1 for t in trades if t.pnl > 0)
    realized_pnl = sum(t.pnl for t in trades)
    avg_trade_return = sum(trade_returns) / len(trade_returns) if trade_returns else 0.0
    pnl_ex_top3 = _compute_pnl_excluding_top3(trades)
    ret_ex_top3 = (pnl_ex_top3 / initial_equity * 100.0) if initial_equity > 0 else 0.0
    t_stat = _compute_trade_t_statistic(trade_returns)

    if not curve:
        return PEADPeriodMetrics(
            start_date=None,
            end_date=None,
            initial_equity=round(initial_equity, 2),
            final_equity=round(initial_equity, 2),
            total_return_pct=0.0,
            cagr_pct=0.0,
            max_drawdown_pct=0.0,
            annualized_volatility_pct=0.0,
            sharpe_ratio=0.0,
            has_benchmark=False,
            benchmark_total_return_pct=0.0,
            benchmark_cagr_pct=0.0,
            benchmark_max_drawdown_pct=0.0,
            benchmark_sharpe_ratio=0.0,
            excess_return_vs_spy_pct=0.0,
            closed_trades=len(trades),
            win_rate_pct=round(winners / len(trades) * 100.0, 2) if trades else 0.0,
            realized_pnl=round(realized_pnl, 2),
            avg_trade_return_pct=round(avg_trade_return, 4),
            pnl_excluding_top3=round(pnl_ex_top3, 2),
            return_pct_excluding_top3=round(ret_ex_top3, 2),
            trade_t_statistic=round(t_stat, 4),
            total_commissions=round(total_commissions, 4),
            total_sec_fees=round(total_sec_fees, 4),
            total_finra_taf_fees=round(total_finra_taf_fees, 4),
            total_slippage_cost=round(total_slippage_cost, 4),
            total_costs=round(total_costs, 4),
        )

    period_start = curve[0]["date"]
    period_end = curve[-1]["date"]
    final_equity = float(curve[-1]["equity"])
    total_return = (final_equity / initial_equity - 1.0) if initial_equity > 0 else 0.0
    elapsed_days = max(1, (period_end - period_start).days)
    cagr = (
        (final_equity / initial_equity) ** (365.25 / elapsed_days) - 1.0
        if initial_equity > 0 and final_equity > 0
        else 0.0
    )

    peak = previous_equity
    max_drawdown = 0.0
    returns: list[float] = []
    prev_eq = previous_equity
    for idx, point in enumerate(curve):
        eq = float(point["equity"])
        peak = max(peak, eq)
        if peak > 0:
            max_drawdown = min(max_drawdown, eq / peak - 1.0)
        if idx > 0 or include_first_return:
            if prev_eq > 0:
                returns.append(eq / prev_eq - 1.0)
        prev_eq = eq

    vol = pstdev(returns) * sqrt(252) if len(returns) > 1 else 0.0
    daily_std = pstdev(returns) if len(returns) > 1 else 0.0
    mean_ret = sum(returns) / len(returns) if returns else 0.0
    sharpe = (mean_ret / daily_std * sqrt(252)) if daily_std > 0 else 0.0

    period_spy = [bar for bar in spy_bars if period_start <= bar.date <= period_end]
    has_benchmark = (
        len(period_spy) > 1 or (anchor_spy_close is not None and len(period_spy) >= 1)
    )
    benchmark_return = 0.0
    benchmark_cagr = 0.0
    benchmark_drawdown = 0.0
    benchmark_sharpe = 0.0
    if has_benchmark:
        base_spy_close = anchor_spy_close if anchor_spy_close is not None else period_spy[0].close
        benchmark_return = period_spy[-1].close / base_spy_close - 1.0
        benchmark_cagr = (
            (1.0 + benchmark_return) ** (365.25 / elapsed_days) - 1.0
            if benchmark_return > -1.0
            else -1.0
        )
        bench_peak = base_spy_close
        bench_returns: list[float] = []
        prev_spy_close = anchor_spy_close
        for bar in period_spy:
            bench_peak = max(bench_peak, bar.close)
            if bench_peak > 0:
                benchmark_drawdown = min(benchmark_drawdown, bar.close / bench_peak - 1.0)
            if prev_spy_close is not None and prev_spy_close > 0:
                bench_returns.append(bar.close / prev_spy_close - 1.0)
            prev_spy_close = bar.close
        bench_std = pstdev(bench_returns) if len(bench_returns) > 1 else 0.0
        bench_mean = sum(bench_returns) / len(bench_returns) if bench_returns else 0.0
        benchmark_sharpe = (bench_mean / bench_std * sqrt(252)) if bench_std > 0 else 0.0

    total_return_pct = round(total_return * 100.0, 2)
    benchmark_return_pct = round(benchmark_return * 100.0, 2)

    return PEADPeriodMetrics(
        start_date=period_start,
        end_date=period_end,
        initial_equity=round(initial_equity, 2),
        final_equity=round(final_equity, 2),
        total_return_pct=total_return_pct,
        cagr_pct=round(cagr * 100.0, 2),
        max_drawdown_pct=round(max_drawdown * 100.0, 2),
        annualized_volatility_pct=round(vol * 100.0, 2),
        sharpe_ratio=round(sharpe, 3),
        has_benchmark=has_benchmark,
        benchmark_total_return_pct=benchmark_return_pct,
        benchmark_cagr_pct=round(benchmark_cagr * 100.0, 2),
        benchmark_max_drawdown_pct=round(benchmark_drawdown * 100.0, 2),
        benchmark_sharpe_ratio=round(benchmark_sharpe, 3),
        excess_return_vs_spy_pct=round(total_return_pct - benchmark_return_pct, 2),
        closed_trades=len(trades),
        win_rate_pct=round(winners / len(trades) * 100.0, 2) if trades else 0.0,
        realized_pnl=round(realized_pnl, 2),
        avg_trade_return_pct=round(avg_trade_return, 4),
        pnl_excluding_top3=round(pnl_ex_top3, 2),
        return_pct_excluding_top3=round(ret_ex_top3, 2),
        trade_t_statistic=round(t_stat, 4),
        total_commissions=round(total_commissions, 4),
        total_sec_fees=round(total_sec_fees, 4),
        total_finra_taf_fees=round(total_finra_taf_fees, 4),
        total_slippage_cost=round(total_slippage_cost, 4),
        total_costs=round(total_costs, 4),
    )


def _compute_pnl_excluding_top3(trades: Sequence[PEADTrade]) -> float:
    if len(trades) <= 3:
        return 0.0
    ordered = sorted(trades, key=lambda item: item.pnl, reverse=True)
    return sum(item.pnl for item in ordered[3:])


def _compute_trade_t_statistic(trade_returns: Sequence[float]) -> float:
    n = len(trade_returns)
    if n < 2:
        return 0.0
    sample_std = stdev(trade_returns)
    if sample_std <= 1e-12:
        return 0.0
    mean_ret = sum(trade_returns) / n
    return mean_ret / (sample_std / sqrt(n))


def _build_event_price_audit_row(
    event: EarningsEvent,
    sym_adj_bars: list[Bar],
    sym_trades_bars: list[Bar],
    adj_eval: PEADCandidateEvaluation,
    trades_eval: PEADCandidateEvaluation,
) -> dict[str, Any] | None:
    adj_idx = resolve_entry_bar_index(sym_adj_bars, event)
    if adj_idx is None or adj_idx == 0:
        return None
    adj_entry = sym_adj_bars[adj_idx]
    adj_prev = sym_adj_bars[adj_idx - 1]
    trades_by_date = {bar.date: bar for bar in sym_trades_bars}
    trades_entry = trades_by_date.get(adj_entry.date)
    trades_prior = [bar for bar in sym_trades_bars if bar.date < adj_entry.date]
    if trades_entry is None or not trades_prior:
        return {
            "symbol": event.symbol,
            "announcement_date": event.announcement_date.isoformat(),
            "entry_date": adj_entry.date.isoformat(),
            "missing_trades_bar": True,
            "adj_accepted": adj_eval.accepted,
            "trades_accepted": trades_eval.accepted,
            "decision_changed": adj_eval.accepted != trades_eval.accepted,
        }
    trades_prev = trades_prior[-1]
    adj_gap = adj_entry.open / adj_prev.close - 1.0
    trades_gap = trades_entry.open / trades_prev.close - 1.0
    open_diff_pct = (trades_entry.open - adj_entry.open) / trades_entry.open * 100.0
    prev_close_diff_pct = (trades_prev.close - adj_prev.close) / trades_prev.close * 100.0
    return {
        "symbol": event.symbol,
        "announcement_date": event.announcement_date.isoformat(),
        "entry_date": adj_entry.date.isoformat(),
        "missing_trades_bar": False,
        "adj_prev_close": round(adj_prev.close, 4),
        "trades_prev_close": round(trades_prev.close, 4),
        "adj_entry_open": round(adj_entry.open, 4),
        "trades_entry_open": round(trades_entry.open, 4),
        "adj_gap_pct": round(adj_gap * 100.0, 4),
        "trades_gap_pct": round(trades_gap * 100.0, 4),
        "gap_diff_bps": round((trades_gap - adj_gap) * 10_000.0, 2),
        "open_diff_pct": round(open_diff_pct, 4),
        "prev_close_diff_pct": round(prev_close_diff_pct, 4),
        "adj_accepted": adj_eval.accepted,
        "trades_accepted": trades_eval.accepted,
        "decision_changed": adj_eval.accepted != trades_eval.accepted,
    }


def _build_price_discrepancy_audit(
    adj_by_symbol_date: dict[str, dict[date, Bar]],
    trades_by_symbol_date: dict[str, dict[date, Bar]],
    event_audit_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not trades_by_symbol_date:
        return {
            "trades_bars_provided": False,
            "used_trades_for_execution": False,
            "compared_symbols": [],
            "compared_bar_count": 0,
            "bars_with_price_diff_gt_1pct": 0,
            "bars_with_price_diff_gt_5pct": 0,
            "max_open_diff_pct": 0.0,
            "max_close_diff_pct": 0.0,
            "mean_abs_close_diff_pct": 0.0,
            "decision_changed_count": 0,
            "symbol_summaries": [],
            "event_discrepancies": [],
            "warnings": [
                "No unadjusted TRADES bars provided; opening gaps, stops, and integer share counts used ADJUSTED_LAST."
            ],
        }

    compared_symbols = sorted(set(adj_by_symbol_date) & set(trades_by_symbol_date))
    total_bars = 0
    gt_1pct = 0
    gt_5pct = 0
    max_open_diff_pct = 0.0
    max_close_diff_pct = 0.0
    close_diff_sum = 0.0
    symbol_summaries: list[dict[str, Any]] = []

    for symbol in compared_symbols:
        adj_map = adj_by_symbol_date[symbol]
        trades_map = trades_by_symbol_date[symbol]
        common_dates = sorted(set(adj_map) & set(trades_map))
        sym_gt_1pct = 0
        sym_gt_5pct = 0
        sym_max_open = 0.0
        sym_max_close = 0.0
        for bar_date in common_dates:
            adj_bar = adj_map[bar_date]
            trades_bar = trades_map[bar_date]
            open_diff = abs(trades_bar.open - adj_bar.open) / trades_bar.open * 100.0
            close_diff = abs(trades_bar.close - adj_bar.close) / trades_bar.close * 100.0
            total_bars += 1
            close_diff_sum += close_diff
            max_open_diff_pct = max(max_open_diff_pct, open_diff)
            max_close_diff_pct = max(max_close_diff_pct, close_diff)
            sym_max_open = max(sym_max_open, open_diff)
            sym_max_close = max(sym_max_close, close_diff)
            if max(open_diff, close_diff) > 1.0:
                gt_1pct += 1
                sym_gt_1pct += 1
            if max(open_diff, close_diff) > 5.0:
                gt_5pct += 1
                sym_gt_5pct += 1
        symbol_summaries.append(
            {
                "symbol": symbol,
                "compared_bars": len(common_dates),
                "max_open_diff_pct": round(sym_max_open, 4),
                "max_close_diff_pct": round(sym_max_close, 4),
                "bars_with_diff_gt_1pct": sym_gt_1pct,
                "bars_with_diff_gt_5pct": sym_gt_5pct,
            }
        )

    decision_changed_count = sum(1 for row in event_audit_rows if row.get("decision_changed"))
    warnings: list[str] = []
    if gt_5pct > 0:
        warnings.append(
            f"Detected {gt_5pct} overlapping bars with >5% TRADES vs ADJUSTED_LAST divergence (likely splits or large dividends)."
        )
    if decision_changed_count > 0:
        warnings.append(
            f"{decision_changed_count} event(s) had different acceptance decisions under TRADES vs ADJUSTED_LAST."
        )

    return {
        "trades_bars_provided": True,
        "used_trades_for_execution": True,
        "compared_symbols": compared_symbols,
        "compared_bar_count": total_bars,
        "bars_with_price_diff_gt_1pct": gt_1pct,
        "bars_with_price_diff_gt_5pct": gt_5pct,
        "max_open_diff_pct": round(max_open_diff_pct, 4),
        "max_close_diff_pct": round(max_close_diff_pct, 4),
        "mean_abs_close_diff_pct": round(close_diff_sum / total_bars, 4) if total_bars else 0.0,
        "decision_changed_count": decision_changed_count,
        "symbol_summaries": symbol_summaries,
        "event_discrepancies": event_audit_rows,
        "warnings": warnings,
    }


def _serialize_dates_in_place(obj: Any) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, date):
                obj[key] = value.isoformat()
            else:
                _serialize_dates_in_place(value)
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            if isinstance(value, date):
                obj[idx] = value.isoformat()
            else:
                _serialize_dates_in_place(value)
    return obj
