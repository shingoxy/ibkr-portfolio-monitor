from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from math import exp, floor, log, sqrt
from statistics import median, pstdev

from app.trading.models import Bar

VALID_CONTRIBUTION_FREQUENCIES = {"none", "monthly", "quarterly"}
VALID_REBALANCE_FREQUENCIES = {"buy_and_hold", "monthly", "quarterly", "drift"}

STRESS_WINDOWS: tuple[dict[str, object], ...] = (
    {"name": "2018 Q4", "start_date": date(2018, 9, 20), "end_date": date(2018, 12, 24)},
    {"name": "2020 COVID", "start_date": date(2020, 2, 19), "end_date": date(2020, 3, 23)},
    {"name": "2022 Bear", "start_date": date(2022, 1, 3), "end_date": date(2022, 10, 12)},
    {"name": "2025 Tariff", "start_date": date(2025, 2, 19), "end_date": date(2025, 4, 8)},
)


@dataclass(frozen=True)
class IndexBacktestConfig:
    initial_cash: float = 11_000.0
    commission_per_order: float = 1.0
    slippage_bps: float = 5.0
    periodic_contribution: float = 0.0
    contribution_frequency: str = "none"

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.commission_per_order < 0:
            raise ValueError("commission_per_order must be non-negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if self.periodic_contribution < 0:
            raise ValueError("periodic_contribution must be non-negative")
        if self.contribution_frequency not in VALID_CONTRIBUTION_FREQUENCIES:
            raise ValueError("unsupported contribution_frequency")


@dataclass(frozen=True)
class PortfolioRule:
    name: str
    target_weights: dict[str, float]
    rebalance_frequency: str = "quarterly"
    trend_window: int | None = None
    defensive_symbol: str | None = None
    trend_buffer_pct: float = 0.0
    drift_threshold_pct: float | None = None

    def __post_init__(self) -> None:
        if not self.target_weights:
            raise ValueError("target_weights must not be empty")
        if any(weight < 0 for weight in self.target_weights.values()):
            raise ValueError("target weights must be non-negative")
        if sum(self.target_weights.values()) > 1.0 + 1e-9:
            raise ValueError("target weights must sum to at most 1")
        if self.rebalance_frequency not in VALID_REBALANCE_FREQUENCIES:
            raise ValueError("unsupported rebalance frequency")
        if self.trend_window is not None and self.trend_window < 2:
            raise ValueError("trend_window must be at least 2")
        if self.defensive_symbol is not None and not self.defensive_symbol.strip():
            raise ValueError("defensive_symbol must not be blank")
        if not (0.0 <= self.trend_buffer_pct < 0.5):
            raise ValueError("trend_buffer_pct must be in [0.0, 0.5)")
        if self.rebalance_frequency == "drift":
            if self.drift_threshold_pct is None or not (0.0 < self.drift_threshold_pct < 1.0):
                raise ValueError("drift rebalance requires 0 < drift_threshold_pct < 1")
        elif self.drift_threshold_pct is not None and not (0.0 < self.drift_threshold_pct < 1.0):
            raise ValueError("drift_threshold_pct must be between 0 and 1")


@dataclass
class PortfolioBacktestResult:
    strategy_name: str
    start_date: date
    end_date: date
    initial_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    average_exposure_pct: float
    order_count: int
    total_costs: float
    equity_curve: list[dict]
    ending_holdings: dict[str, int]
    twr_total_return_pct: float = 0.0
    twr_cagr_pct: float = 0.0
    total_contributed: float = 0.0
    net_profit: float = 0.0
    mwr_xirr_pct: float = 0.0
    annual_breakdown: list[dict] = field(default_factory=list)
    stress_periods: list[dict] = field(default_factory=list)
    rolling_1y: dict = field(default_factory=dict)
    rolling_3y: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["start_date"] = _serialize_date(self.start_date)
        payload["end_date"] = _serialize_date(self.end_date)
        for point in payload["equity_curve"]:
            point["date"] = _serialize_date(point["date"])
        for row in payload["annual_breakdown"]:
            if "start_date" in row:
                row["start_date"] = _serialize_date(row["start_date"])
            if "end_date" in row:
                row["end_date"] = _serialize_date(row["end_date"])
        for row in payload["stress_periods"]:
            if "start_date" in row:
                row["start_date"] = _serialize_date(row["start_date"])
            if "end_date" in row:
                row["end_date"] = _serialize_date(row["end_date"])
        return payload


def _serialize_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def run_index_backtest(
    bars_by_symbol: dict[str, list[Bar]],
    rule: PortfolioRule,
    config: IndexBacktestConfig | None = None,
    start_date: date | None = None,
    benchmark_curve: list[dict] | None = None,
) -> PortfolioBacktestResult:
    """Run a daily open-execution index portfolio backtest with optional trend, drift, defensive routing, and DCA."""
    config = config or IndexBacktestConfig()
    target_symbols = list(rule.target_weights)
    symbols = list(dict.fromkeys([*target_symbols, *([rule.defensive_symbol] if rule.defensive_symbol else [])]))
    normalized = {
        symbol: sorted(bars_by_symbol.get(symbol, []), key=lambda bar: bar.date)
        for symbol in symbols
    }
    missing = [symbol for symbol, bars in normalized.items() if not bars]
    if missing:
        raise ValueError(f"missing bars for: {', '.join(missing)}")

    bars_on_date = {
        symbol: {bar.date: bar for bar in bars}
        for symbol, bars in normalized.items()
    }
    common_dates = sorted(set.intersection(*(set(items) for items in bars_on_date.values())))
    if not common_dates:
        raise ValueError("symbols have no common trading dates")

    cash = config.initial_cash
    holdings = {symbol: 0 for symbol in symbols}
    histories: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    equity_curve: list[dict] = []
    order_count = 0
    total_costs = 0.0
    initialized = False
    previous_evaluation_date: date | None = None
    risk_on: dict[str, bool] = {}
    active_weights: dict[str, float] = {}

    for current_date in common_dates:
        day_bars = {symbol: bars_on_date[symbol][current_date] for symbol in symbols}
        if start_date is not None and current_date < start_date:
            for symbol, bar in day_bars.items():
                histories[symbol].append(bar)
            continue

        if not initialized:
            if rule.trend_window is not None and any(
                len(histories[symbol]) < rule.trend_window for symbol in symbols
            ):
                if start_date is None:
                    for symbol, bar in day_bars.items():
                        histories[symbol].append(bar)
                    continue
                raise ValueError("insufficient pre-start history for trend filter")

            open_prices = {symbol: bar.open for symbol, bar in day_bars.items()}
            active_weights, risk_on = _evaluate_weights(rule, histories, risk_on)
            cash, orders, costs = _rebalance(cash, holdings, open_prices, active_weights, config)
            order_count += orders
            total_costs += costs
            initialized = True
            day_contribution = 0.0
        else:
            open_prices = {symbol: bar.open for symbol, bar in day_bars.items()}
            day_contribution = 0.0
            if (
                config.periodic_contribution > 0
                and config.contribution_frequency in {"monthly", "quarterly"}
                and _is_new_period(current_date, previous_evaluation_date, config.contribution_frequency)
            ):
                day_contribution = config.periodic_contribution
                cash += day_contribution

            should_rebalance = False
            if rule.rebalance_frequency == "drift" or _is_rebalance_date(
                current_date,
                previous_evaluation_date,
                rule.rebalance_frequency,
            ):
                candidate_weights, new_risk_on = _evaluate_weights(rule, histories, risk_on)
                trend_changed = new_risk_on != risk_on
                if rule.drift_threshold_pct is not None:
                    drift = _max_weight_drift(cash, holdings, open_prices, candidate_weights, symbols)
                    should_rebalance = trend_changed or (drift + 1e-12 >= rule.drift_threshold_pct)
                else:
                    should_rebalance = True
                risk_on = new_risk_on
                active_weights = candidate_weights

            if should_rebalance:
                cash, orders, costs = _rebalance(cash, holdings, open_prices, active_weights, config)
                order_count += orders
                total_costs += costs
            elif day_contribution > 0:
                if rule.rebalance_frequency == "buy_and_hold":
                    cash, orders, costs = _buy_only_by_weights(
                        cash, holdings, open_prices, active_weights, config
                    )
                else:
                    cash, orders, costs = _buy_underweight_only(
                        cash, holdings, open_prices, active_weights, config
                    )
                order_count += orders
                total_costs += costs

        close_exposure = sum(
            holdings[symbol] * day_bars[symbol].close for symbol in symbols
        )
        equity = cash + close_exposure
        point: dict[str, object] = {
            "date": current_date,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "gross_exposure": round(close_exposure, 2),
        }
        if day_contribution > 0:
            point["contribution"] = round(day_contribution, 2)
        equity_curve.append(point)

        for symbol, bar in day_bars.items():
            histories[symbol].append(bar)
        previous_evaluation_date = current_date

    if not equity_curve:
        raise ValueError("start_date is after the available history")
    return _build_result(
        rule.name,
        config,
        equity_curve,
        holdings,
        order_count,
        total_costs,
        benchmark_curve=benchmark_curve,
    )


def _evaluate_weights(
    rule: PortfolioRule,
    histories: dict[str, list[Bar]],
    previous_risk_on: dict[str, bool],
) -> tuple[dict[str, float], dict[str, bool]]:
    if rule.trend_window is None:
        weights = dict(rule.target_weights)
        if rule.defensive_symbol is not None and rule.defensive_symbol not in weights:
            weights[rule.defensive_symbol] = 0.0
        return weights, dict(previous_risk_on)

    weights: dict[str, float] = {}
    new_risk_on: dict[str, bool] = {}
    freed_weight = 0.0

    for symbol, base_weight in rule.target_weights.items():
        if symbol == rule.defensive_symbol:
            weights[symbol] = base_weight
            continue

        closes = [bar.close for bar in histories[symbol][-rule.trend_window :]]
        sma = sum(closes) / len(closes)
        prev_close = closes[-1]

        if rule.trend_buffer_pct == 0.0 or symbol not in previous_risk_on:
            state = prev_close > sma
        elif previous_risk_on[symbol]:
            state = prev_close >= sma * (1.0 - rule.trend_buffer_pct)
        else:
            state = prev_close > sma * (1.0 + rule.trend_buffer_pct)

        new_risk_on[symbol] = state
        if state:
            weights[symbol] = base_weight
        else:
            weights[symbol] = 0.0
            freed_weight += base_weight

    if rule.defensive_symbol is not None:
        weights[rule.defensive_symbol] = weights.get(rule.defensive_symbol, 0.0) + freed_weight

    return weights, new_risk_on


def _is_new_period(
    current_date: date,
    previous_date: date | None,
    frequency: str,
) -> bool:
    if previous_date is None or frequency in {"none", "buy_and_hold", "drift"}:
        return False
    if frequency == "monthly":
        return (current_date.year, current_date.month) != (previous_date.year, previous_date.month)
    return (current_date.year, (current_date.month - 1) // 3) != (
        previous_date.year,
        (previous_date.month - 1) // 3,
    )


def _is_rebalance_date(
    current_date: date,
    previous_date: date | None,
    frequency: str,
) -> bool:
    return _is_new_period(current_date, previous_date, frequency)


def _max_weight_drift(
    cash: float,
    holdings: dict[str, int],
    open_prices: dict[str, float],
    effective_weights: dict[str, float],
    symbols: list[str],
) -> float:
    open_equity = cash + sum(holdings[s] * open_prices[s] for s in symbols)
    if open_equity <= 0:
        return 0.0
    return max(
        abs((holdings[s] * open_prices[s]) / open_equity - effective_weights.get(s, 0.0))
        for s in symbols
    )


def _execute_planned_buys(
    cash: float,
    holdings: dict[str, int],
    open_prices: dict[str, float],
    planned_buys: dict[str, int],
    config: IndexBacktestConfig,
) -> tuple[float, int, float]:
    orders = 0
    costs = 0.0
    for symbol, quantity in planned_buys.items():
        if quantity <= 0:
            continue
        fill_price = open_prices[symbol] * (1 + config.slippage_bps / 10_000)
        total_outlay = quantity * fill_price + config.commission_per_order
        if total_outlay > cash + 1e-9:
            affordable = floor(max(0.0, cash - config.commission_per_order) / fill_price)
            quantity = min(quantity, affordable)
            if quantity <= 0:
                continue
            total_outlay = quantity * fill_price + config.commission_per_order
        cash -= total_outlay
        holdings[symbol] += quantity
        orders += 1
        costs += quantity * (fill_price - open_prices[symbol]) + config.commission_per_order
    return cash, orders, costs


def _buy_only_by_weights(
    cash: float,
    holdings: dict[str, int],
    open_prices: dict[str, float],
    effective_weights: dict[str, float],
    config: IndexBacktestConfig,
) -> tuple[float, int, float]:
    active_symbols = [
        s for s in sorted(effective_weights, key=lambda sym: effective_weights[sym], reverse=True)
        if effective_weights.get(s, 0.0) > 0
    ]
    if not active_symbols or cash <= config.commission_per_order:
        return cash, 0, 0.0

    planned = {symbol: 0 for symbol in active_symbols}
    sim_cash = cash
    initial_pool = cash

    # First pass: allocate available cash proportionally to target weights.
    for symbol in active_symbols:
        fill_price = open_prices[symbol] * (1 + config.slippage_bps / 10_000)
        budget = min(sim_cash, initial_pool * effective_weights[symbol])
        qty = floor(max(0.0, budget - config.commission_per_order) / fill_price)
        if qty > 0:
            planned[symbol] = qty
            sim_cash -= qty * fill_price + config.commission_per_order

    # Second pass: deploy any leftover cash that can buy an affordable target share.
    equity = cash + sum(holdings[s] * open_prices[s] for s in holdings)
    while True:
        best_symbol = None
        best_score = -float("inf")
        for symbol in active_symbols:
            fill_price = open_prices[symbol] * (1 + config.slippage_bps / 10_000)
            comm = config.commission_per_order if planned[symbol] == 0 else 0.0
            if sim_cash + 1e-9 < fill_price + comm:
                continue
            target_dollar = equity * effective_weights[symbol]
            current_dollar = (holdings[symbol] + planned[symbol]) * open_prices[symbol]
            deficit = target_dollar - current_dollar
            if deficit > best_score:
                best_score = deficit
                best_symbol = symbol
        if best_symbol is None or best_score <= 0:
            break
        fill_price = open_prices[best_symbol] * (1 + config.slippage_bps / 10_000)
        comm = config.commission_per_order if planned[best_symbol] == 0 else 0.0
        planned[best_symbol] += 1
        sim_cash -= fill_price + comm

    return _execute_planned_buys(cash, holdings, open_prices, planned, config)


def _buy_underweight_only(
    cash: float,
    holdings: dict[str, int],
    open_prices: dict[str, float],
    effective_weights: dict[str, float],
    config: IndexBacktestConfig,
) -> tuple[float, int, float]:
    active_symbols = [
        s for s in sorted(effective_weights, key=lambda sym: effective_weights[sym], reverse=True)
        if effective_weights.get(s, 0.0) > 0
    ]
    if not active_symbols or cash <= config.commission_per_order:
        return cash, 0, 0.0

    planned = {symbol: 0 for symbol in active_symbols}
    sim_cash = cash
    equity = cash + sum(holdings[s] * open_prices[s] for s in holdings)

    while True:
        best_symbol = None
        best_deficit = 0.0
        for symbol in active_symbols:
            fill_price = open_prices[symbol] * (1 + config.slippage_bps / 10_000)
            comm = config.commission_per_order if planned[symbol] == 0 else 0.0
            if sim_cash + 1e-9 < fill_price + comm:
                continue
            deficit = equity * effective_weights[symbol] - (holdings[symbol] + planned[symbol]) * open_prices[symbol]
            if deficit > best_deficit:
                best_deficit = deficit
                best_symbol = symbol
        if best_symbol is None:
            break
        fill_price = open_prices[best_symbol] * (1 + config.slippage_bps / 10_000)
        comm = config.commission_per_order if planned[best_symbol] == 0 else 0.0
        planned[best_symbol] += 1
        sim_cash -= fill_price + comm

    return _execute_planned_buys(cash, holdings, open_prices, planned, config)


def _rebalance(
    cash: float,
    holdings: dict[str, int],
    open_prices: dict[str, float],
    target_weights: dict[str, float],
    config: IndexBacktestConfig,
) -> tuple[float, int, float]:
    equity = cash + sum(holdings[symbol] * open_prices[symbol] for symbol in holdings)
    target_shares = {
        symbol: floor((equity * target_weights.get(symbol, 0.0)) / open_prices[symbol])
        for symbol in holdings
    }
    orders = 0
    costs = 0.0

    for symbol in holdings:
        quantity = holdings[symbol] - target_shares[symbol]
        if quantity <= 0:
            continue
        fill_price = open_prices[symbol] * (1 - config.slippage_bps / 10_000)
        cash += quantity * fill_price - config.commission_per_order
        holdings[symbol] -= quantity
        orders += 1
        costs += quantity * (open_prices[symbol] - fill_price) + config.commission_per_order

    buy_order = sorted(holdings, key=lambda symbol: target_weights.get(symbol, 0.0), reverse=True)
    for symbol in buy_order:
        requested = target_shares[symbol] - holdings[symbol]
        if requested <= 0:
            continue
        fill_price = open_prices[symbol] * (1 + config.slippage_bps / 10_000)
        affordable = floor(max(0.0, cash - config.commission_per_order) / fill_price)
        quantity = min(requested, affordable)
        if quantity <= 0:
            continue
        cash -= quantity * fill_price + config.commission_per_order
        holdings[symbol] += quantity
        orders += 1
        costs += quantity * (fill_price - open_prices[symbol]) + config.commission_per_order

    return cash, orders, costs


def _twr_factors(
    equity_curve: list[dict],
    initial_cash: float | None = None,
) -> tuple[list[float], list[float]]:
    """Return cumulative TWR wealth factors (len N) and daily TWR returns r_1..r_{N-1} (len N-1)."""
    if not equity_curve:
        return [], []

    first_equity = float(equity_curve[0]["equity"])
    factors = [first_equity / initial_cash if initial_cash else 1.0]
    returns: list[float] = []

    for idx in range(1, len(equity_curve)):
        prev_equity = float(equity_curve[idx - 1]["equity"])
        contrib = float(equity_curve[idx].get("contribution", 0.0) or 0.0)
        curr_equity = float(equity_curve[idx]["equity"])
        denom = prev_equity + contrib
        daily_return = (curr_equity / denom - 1.0) if denom > 0 else 0.0
        returns.append(daily_return)
        factors.append(factors[-1] * (1.0 + daily_return))
    return factors, returns


def _max_drawdown_from_factors(factors: list[float], initial_peak: float = 0.0) -> float:
    peak = initial_peak
    max_dd = 0.0
    for factor in factors:
        peak = max(peak, factor)
        if peak > 0:
            max_dd = min(max_dd, factor / peak - 1.0)
    return max_dd


def _compute_xirr(
    start_date: date,
    end_date: date,
    initial_cash: float,
    equity_curve: list[dict],
) -> float:
    """Compute money-weighted annualized IRR (XIRR) as a decimal rate."""
    elapsed_days = (end_date - start_date).days
    if elapsed_days <= 0:
        return 0.0

    cash_flows: list[tuple[float, float]] = [(0.0, -initial_cash)]
    for point in equity_curve:
        contrib = float(point.get("contribution", 0.0) or 0.0)
        if contrib > 0:
            t_years = (_as_date(point["date"]) - start_date).days / 365.25
            cash_flows.append((t_years, -contrib))

    final_equity = float(equity_curve[-1]["equity"])
    t_end = elapsed_days / 365.25
    cash_flows.append((t_end, final_equity))

    # Check terminal same-day net cash flow limit as u -> -infinity.
    terminal_net = sum(cf for t, cf in cash_flows if abs(t - t_end) < 1e-12)
    if terminal_net <= 0:
        return -1.0

    total_net = sum(cf for _, cf in cash_flows)
    if abs(total_net) < 1e-12:
        return 0.0

    # h(u) = sum(cf_i * exp(u * (t_end - t_i))) is strictly decreasing in u = ln(1 + r).
    def h(u: float) -> float:
        total = 0.0
        for t, cf in cash_flows:
            exponent = u * (t_end - t)
            if exponent > 700.0:
                total += -float("inf") if cf < 0 else float("inf")
            elif exponent < -700.0:
                continue
            else:
                total += cf * exp(exponent)
        return total

    if total_net > 0:
        u_low = 0.0
        u_high = 1.0
        while u_high < 700.0 and h(u_high) > 0:
            u_high = min(700.0, u_high * 2.0)
            if u_high == 700.0 and h(u_high) > 0:
                break
    else:
        u_high = 0.0
        u_low = -1.0
        while u_low > -700.0 and h(u_low) < 0:
            u_low = max(-700.0, u_low * 2.0)
            if u_low == -700.0 and h(u_low) < 0:
                break

    for _ in range(100):
        u_mid = 0.5 * (u_low + u_high)
        val = h(u_mid)
        if abs(val) < 1e-10:
            u_low = u_high = u_mid
            break
        if val > 0:
            u_low = u_mid
        else:
            u_high = u_mid

    u_root = max(-700.0, min(700.0, 0.5 * (u_low + u_high)))
    return exp(u_root) - 1.0


def compute_annual_breakdown(equity_curve: list[dict]) -> list[dict]:
    """Compute per-calendar-year TWR return, max drawdown, and average exposure."""
    if not equity_curve:
        return []

    factors, _ = _twr_factors(equity_curve)
    dates = [_as_date(point["date"]) for point in equity_curve]
    indices_by_year: dict[int, list[int]] = {}
    for idx, d in enumerate(dates):
        indices_by_year.setdefault(d.year, []).append(idx)

    rows: list[dict] = []
    for year in sorted(indices_by_year):
        idxs = indices_by_year[year]
        first_idx, last_idx = idxs[0], idxs[-1]
        base_factor = factors[first_idx - 1] if first_idx > 0 else factors[first_idx]
        year_return = (factors[last_idx] / base_factor - 1.0) if base_factor > 0 else 0.0
        year_dd = _max_drawdown_from_factors(
            [factors[i] for i in idxs],
            initial_peak=base_factor,
        )
        exposures = [
            float(equity_curve[i]["gross_exposure"]) / float(equity_curve[i]["equity"])
            for i in idxs
            if float(equity_curve[i]["equity"]) > 0
        ]
        avg_exp = (sum(exposures) / len(exposures)) if exposures else 0.0
        rows.append(
            {
                "year": year,
                "start_date": dates[first_idx],
                "end_date": dates[last_idx],
                "return_pct": round(year_return * 100, 2),
                "max_drawdown_pct": round(year_dd * 100, 2),
                "avg_exposure_pct": round(avg_exp * 100, 2),
            }
        )
    return rows


def compute_stress_periods(
    equity_curve: list[dict],
    windows: tuple | list = STRESS_WINDOWS,
) -> list[dict]:
    """Compute TWR return, max drawdown, and average exposure for covered stress windows (>= 5 trading days)."""
    if not equity_curve:
        return []

    factors, _ = _twr_factors(equity_curve)
    dates = [_as_date(point["date"]) for point in equity_curve]
    rows: list[dict] = []

    for window in windows:
        if isinstance(window, dict):
            name = str(window["name"])
            w_start = _as_date(window.get("start_date") or window["start"])
            w_end = _as_date(window.get("end_date") or window["end"])
        else:
            name, raw_start, raw_end = window
            name = str(name)
            w_start = _as_date(raw_start)
            w_end = _as_date(raw_end)

        idxs = [i for i, d in enumerate(dates) if w_start <= d <= w_end]
        if len(idxs) < 5:
            continue

        first_idx, last_idx = idxs[0], idxs[-1]
        base_factor = factors[first_idx]
        period_return = (factors[last_idx] / base_factor - 1.0) if base_factor > 0 else 0.0
        period_dd = _max_drawdown_from_factors([factors[i] for i in idxs])
        exposures = [
            float(equity_curve[i]["gross_exposure"]) / float(equity_curve[i]["equity"])
            for i in idxs
            if float(equity_curve[i]["equity"]) > 0
        ]
        avg_exp = (sum(exposures) / len(exposures)) if exposures else 0.0
        rows.append(
            {
                "name": name,
                "start_date": dates[first_idx],
                "end_date": dates[last_idx],
                "trading_days": len(idxs),
                "return_pct": round(period_return * 100, 2),
                "max_drawdown_pct": round(period_dd * 100, 2),
                "avg_exposure_pct": round(avg_exp * 100, 2),
            }
        )
    return rows


def compute_rolling_windows(
    equity_curve: list[dict],
    benchmark_curve: list[dict] | None = None,
    window_days: int = 252,
) -> dict:
    """Compute rolling window CAGR distribution and win-rate vs optional benchmark."""
    if window_days <= 0:
        raise ValueError("window_days must be positive")

    if len(equity_curve) <= window_days:
        return {
            "window_days": window_days,
            "sample_count": 0,
            "median_cagr_pct": 0.0,
            "min_cagr_pct": 0.0,
            "max_cagr_pct": 0.0,
            "positive_rate_pct": 0.0,
            "beat_benchmark_rate_pct": None if benchmark_curve is None else 0.0,
        }

    factors, _ = _twr_factors(equity_curve)
    dates = [_as_date(point["date"]) for point in equity_curve]

    bench_factor_by_date: dict[date, float] = {}
    if benchmark_curve:
        bench_factors, _ = _twr_factors(benchmark_curve)
        for point, b_factor in zip(benchmark_curve, bench_factors):
            bench_factor_by_date[_as_date(point["date"])] = b_factor

    cagrs: list[float] = []
    beat_count = 0
    compared_count = 0

    for start_idx in range(len(equity_curve) - window_days):
        end_idx = start_idx + window_days
        start_d, end_d = dates[start_idx], dates[end_idx]
        elapsed_days = max(1, (end_d - start_d).days)
        ratio = factors[end_idx] / factors[start_idx] if factors[start_idx] > 0 else 0.0
        cagr = (ratio ** (365.25 / elapsed_days) - 1.0) if ratio > 0 else -1.0
        cagrs.append(cagr)

        if benchmark_curve and start_d in bench_factor_by_date and end_d in bench_factor_by_date:
            b_start = bench_factor_by_date[start_d]
            b_end = bench_factor_by_date[end_d]
            b_ratio = b_end / b_start if b_start > 0 else 0.0
            b_cagr = (b_ratio ** (365.25 / elapsed_days) - 1.0) if b_ratio > 0 else -1.0
            compared_count += 1
            if cagr > b_cagr:
                beat_count += 1

    positive_rate = sum(1 for val in cagrs if val > 0) / len(cagrs) * 100.0
    beat_rate: float | None = None
    if benchmark_curve is not None:
        beat_rate = round((beat_count / compared_count * 100.0) if compared_count > 0 else 0.0, 2)

    return {
        "window_days": window_days,
        "sample_count": len(cagrs),
        "median_cagr_pct": round(median(cagrs) * 100.0, 2),
        "min_cagr_pct": round(min(cagrs) * 100.0, 2),
        "max_cagr_pct": round(max(cagrs) * 100.0, 2),
        "positive_rate_pct": round(positive_rate, 2),
        "beat_benchmark_rate_pct": beat_rate,
    }


def _build_result(
    strategy_name: str,
    config: IndexBacktestConfig,
    equity_curve: list[dict],
    holdings: dict[str, int],
    order_count: int,
    total_costs: float,
    benchmark_curve: list[dict] | None = None,
) -> PortfolioBacktestResult:
    start_date = _as_date(equity_curve[0]["date"])
    end_date = _as_date(equity_curve[-1]["date"])
    final_equity = float(equity_curve[-1]["equity"])
    total_contributed = config.initial_cash + sum(
        float(point.get("contribution", 0.0) or 0.0) for point in equity_curve
    )
    net_profit = final_equity - total_contributed
    has_contributions = total_contributed > config.initial_cash + 1e-9

    elapsed_days = max(1, (end_date - start_date).days)
    factors, returns = _twr_factors(equity_curve, initial_cash=config.initial_cash)
    if has_contributions:
        twr_total_return = factors[-1] - 1.0
        twr_cagr = (factors[-1] ** (365.25 / elapsed_days) - 1.0) if factors[-1] > 0 else -1.0
        total_return = twr_total_return
        cagr = twr_cagr
        max_drawdown = _max_drawdown_from_factors(factors)
        mwr_xirr = _compute_xirr(start_date, end_date, config.initial_cash, equity_curve)
    else:
        total_return = final_equity / config.initial_cash - 1.0
        cagr = (final_equity / config.initial_cash) ** (365.25 / elapsed_days) - 1.0
        twr_total_return = total_return
        twr_cagr = cagr
        peak = 0.0
        max_drawdown = 0.0
        for point in equity_curve:
            equity = float(point["equity"])
            peak = max(peak, equity)
            if peak:
                max_drawdown = min(max_drawdown, equity / peak - 1.0)
        mwr_xirr = cagr if (end_date - start_date).days > 0 else 0.0

    exposures = [
        float(point["gross_exposure"]) / float(point["equity"]) if float(point["equity"]) else 0.0
        for point in equity_curve
    ]

    volatility = pstdev(returns) * sqrt(252) if len(returns) > 1 else 0.0
    daily_std = pstdev(returns) if len(returns) > 1 else 0.0
    mean_return = sum(returns) / len(returns) if returns else 0.0
    sharpe = mean_return / daily_std * sqrt(252) if daily_std else 0.0

    return PortfolioBacktestResult(
        strategy_name=strategy_name,
        start_date=start_date,
        end_date=end_date,
        initial_cash=config.initial_cash,
        final_equity=round(final_equity, 2),
        total_return_pct=round(total_return * 100, 2),
        cagr_pct=round(cagr * 100, 2),
        max_drawdown_pct=round(max_drawdown * 100, 2),
        annualized_volatility_pct=round(volatility * 100, 2),
        sharpe_ratio=round(sharpe, 3),
        average_exposure_pct=round(sum(exposures) / len(exposures) * 100, 2),
        order_count=order_count,
        total_costs=round(total_costs, 2),
        equity_curve=equity_curve,
        ending_holdings={symbol: quantity for symbol, quantity in holdings.items() if quantity},
        twr_total_return_pct=round(twr_total_return * 100, 2),
        twr_cagr_pct=round(twr_cagr * 100, 2),
        total_contributed=round(total_contributed, 2),
        net_profit=round(net_profit, 2),
        mwr_xirr_pct=round(mwr_xirr * 100, 2),
        annual_breakdown=compute_annual_breakdown(equity_curve),
        stress_periods=compute_stress_periods(equity_curve),
        rolling_1y=compute_rolling_windows(equity_curve, benchmark_curve=benchmark_curve, window_days=252),
        rolling_3y=compute_rolling_windows(equity_curve, benchmark_curve=benchmark_curve, window_days=756),
    )
