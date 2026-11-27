"""Read-only Index Core Rebalance & 200-SMA Trend Advisor."""

from __future__ import annotations

import csv
import json
import logging
from math import floor
from pathlib import Path
from typing import Any, Sequence

from app.logging_config import mask_account
from app.trading.models import Bar

logger = logging.getLogger(__name__)

DEFAULT_TARGET_WEIGHTS: dict[str, float] = {"SPY": 0.70, "QQQ": 0.30}


def load_cached_bars(history_dir: Path, symbols: Sequence[str]) -> dict[str, list[Bar]]:
    """Load cached daily OHLCV bars from CSV files under ``history_dir``.

    Missing files or unreadable rows are skipped safely so disconnected/partial
    environments degrade gracefully without raising.
    """
    bars_by_symbol: dict[str, list[Bar]] = {}
    if not history_dir.exists():
        return bars_by_symbol

    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        if not symbol or symbol in bars_by_symbol:
            continue
        csv_path = history_dir / f"{symbol}.csv"
        if not csv_path.exists():
            continue
        loaded: list[Bar] = []
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    raw_date = (row.get("date") or "").strip()
                    if not raw_date:
                        continue
                    loaded.append(
                        Bar(
                            symbol=symbol,
                            date=raw_date[:10],
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row.get("volume") or 0.0),
                        )
                    )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Failed to load cached bars from %s: %s", csv_path, exc)
            continue

        if loaded:
            loaded.sort(key=lambda bar: bar.date)
            bars_by_symbol[symbol] = loaded

    return bars_by_symbol


def load_odd_lot_summary(path: Path) -> dict[str, Any]:
    """Load non-expired odd-lot tender opportunities from latest JSON artifact."""
    fallback = {
        "available": False,
        "generated_at": None,
        "as_of_date": None,
        "summary": {},
        "active_opportunities": [],
        "expired_count": 0,
    }
    if not path.exists():
        return fallback

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load odd-lot summary from %s: %s", path, exc)
        return fallback

    if not isinstance(payload, dict):
        return fallback

    opportunities = payload.get("opportunities") or []
    active: list[dict[str, Any]] = []
    expired_count = 0
    for item in opportunities:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        if status == "EXPIRED":
            expired_count += 1
            continue
        active.append(
            {
                "symbol": str(item.get("symbol") or ""),
                "company_name": str(item.get("company_name") or ""),
                "form_type": str(item.get("form_type") or ""),
                "filing_url": str(item.get("filing_url") or ""),
                "offer_type": str(item.get("offer_type") or ""),
                "odd_lot_priority": bool(item.get("odd_lot_priority", False)),
                "max_shares": item.get("max_shares"),
                "min_offer_price": item.get("min_offer_price"),
                "max_offer_price": item.get("max_offer_price"),
                "ask_price": item.get("ask_price"),
                "worst_case_net_profit": item.get("worst_case_net_profit"),
                "best_case_net_profit": item.get("best_case_net_profit"),
                "expiration_date": item.get("expiration_date"),
                "holding_days": item.get("holding_days"),
                "status": status or "UNKNOWN",
                "risk_flags": list(item.get("risk_flags") or []),
            }
        )

    summary_counts = payload.get("summary") or {}
    if isinstance(summary_counts, dict) and isinstance(summary_counts.get("EXPIRED"), int):
        expired_count = max(expired_count, summary_counts["EXPIRED"])

    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "as_of_date": payload.get("as_of_date"),
        "summary": summary_counts if isinstance(summary_counts, dict) else {},
        "active_opportunities": active,
        "expired_count": expired_count,
    }


def load_index_study_summary(path: Path) -> dict[str, Any]:
    """Load lightweight 10-year index study KPIs without equity curves."""
    fallback = {
        "available": False,
        "generated_at": None,
        "initial_cash": None,
        "monthly_contribution": None,
        "requested_years": None,
        "defensive_symbol": None,
        "drift_threshold_pct": None,
        "trend_buffer_pct": None,
        "index_strategies": [],
        "dca_strategies": [],
    }
    if not path.exists():
        return fallback

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load index study summary from %s: %s", path, exc)
        return fallback

    if not isinstance(payload, dict):
        return fallback

    def _extract_kpis(items: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return rows
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "strategy_name": item.get("strategy_name", ""),
                    "start_date": item.get("start_date"),
                    "end_date": item.get("end_date"),
                    "initial_cash": item.get("initial_cash"),
                    "final_equity": item.get("final_equity"),
                    "total_return_pct": item.get("total_return_pct"),
                    "cagr_pct": item.get("cagr_pct"),
                    "max_drawdown_pct": item.get("max_drawdown_pct"),
                    "sharpe_ratio": item.get("sharpe_ratio"),
                    "order_count": item.get("order_count"),
                    "xirr_pct": item.get("xirr_pct"),
                    "twr_cagr_pct": item.get("twr_cagr_pct"),
                    "twr_max_drawdown_pct": item.get("twr_max_drawdown_pct"),
                }
            )
        return rows

    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "initial_cash": payload.get("initial_cash"),
        "monthly_contribution": payload.get("monthly_contribution"),
        "requested_years": payload.get("requested_years"),
        "defensive_symbol": payload.get("defensive_symbol"),
        "drift_threshold_pct": payload.get("drift_threshold_pct"),
        "trend_buffer_pct": payload.get("trend_buffer_pct"),
        "index_strategies": _extract_kpis(payload.get("index_strategies")),
        "dca_strategies": _extract_kpis(payload.get("dca_strategies")),
    }


def evaluate_trend_regime(
    symbol: str,
    bars: Sequence[Bar],
    *,
    trend_window: int = 200,
    trend_buffer_pct: float = 0.02,
) -> dict[str, Any]:
    """Evaluate 200-SMA hysteresis regime matching ``index_backtest._evaluate_weights``."""
    clean_symbol = symbol.strip().upper()
    if trend_window < 2:
        raise ValueError("trend_window must be at least 2")
    if not (0.0 <= trend_buffer_pct < 0.5):
        raise ValueError("trend_buffer_pct must be in [0.0, 0.5)")

    sorted_bars = sorted(bars, key=lambda bar: bar.date)
    if len(sorted_bars) < trend_window:
        return {
            "symbol": clean_symbol,
            "available": False,
            "bars_count": len(sorted_bars),
            "required_bars": trend_window,
            "as_of_date": sorted_bars[-1].date.isoformat() if sorted_bars else None,
            "last_close": float(sorted_bars[-1].close) if sorted_bars else None,
            "sma": None,
            "lower_band": None,
            "upper_band": None,
            "distance_to_sma_pct": None,
            "risk_on": None,
            "regime": "UNAVAILABLE",
            "in_buffer_zone": False,
            "last_switch_date": None,
        }

    closes = [float(bar.close) for bar in sorted_bars]
    risk_on: bool | None = None
    last_switch_date: str | None = None
    sma = 0.0

    for idx in range(trend_window - 1, len(sorted_bars)):
        window_slice = closes[idx - trend_window + 1 : idx + 1]
        sma = sum(window_slice) / trend_window
        close = closes[idx]

        if risk_on is None or trend_buffer_pct == 0.0:
            new_state = close > sma
        elif risk_on:
            new_state = close >= sma * (1.0 - trend_buffer_pct)
        else:
            new_state = close > sma * (1.0 + trend_buffer_pct)

        if risk_on is not None and new_state != risk_on:
            last_switch_date = sorted_bars[idx].date.isoformat()
        risk_on = new_state

    last_bar = sorted_bars[-1]
    last_close = float(last_bar.close)
    lower_band = sma * (1.0 - trend_buffer_pct)
    upper_band = sma * (1.0 + trend_buffer_pct)
    distance_pct = ((last_close / sma) - 1.0) * 100.0 if sma > 0 else 0.0
    in_buffer = lower_band <= last_close <= upper_band

    return {
        "symbol": clean_symbol,
        "available": True,
        "bars_count": len(sorted_bars),
        "required_bars": trend_window,
        "as_of_date": last_bar.date.isoformat(),
        "last_close": round(last_close, 4),
        "sma": round(sma, 4),
        "lower_band": round(lower_band, 4),
        "upper_band": round(upper_band, 4),
        "distance_to_sma_pct": round(distance_pct, 2),
        "risk_on": bool(risk_on),
        "regime": "RISK_ON" if risk_on else "DEFENSIVE",
        "in_buffer_zone": in_buffer,
        "last_switch_date": last_switch_date,
    }


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_account_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw_items = snapshot.get("account_summary") or []
    preferred: dict[str, tuple[int, Any]] = {}
    first_account = ""

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        if not tag:
            continue
        acct = str(item.get("account") or "").strip()
        if acct and not first_account:
            first_account = acct
        currency = str(item.get("currency") or "").strip().upper()
        rank = 2 if currency == "USD" else (1 if currency == "BASE" else 0)
        existing = preferred.get(tag)
        if existing is None or rank > existing[0]:
            preferred[tag] = (rank, item.get("value"))

    tag_map = {tag: val for tag, (_, val) in preferred.items()}
    accounts = snapshot.get("accounts") or []
    primary_raw = str(accounts[0]) if accounts else first_account

    return {
        "account_masked": mask_account(primary_raw) if primary_raw else "",
        "account_type": str(tag_map.get("AccountType") or ""),
        "net_liquidation": _safe_float(tag_map.get("NetLiquidation")),
        "total_cash": _safe_float(tag_map.get("TotalCashValue")),
        "settled_cash": _safe_float(tag_map.get("SettledCash")),
        "available_funds": _safe_float(tag_map.get("AvailableFunds")),
        "buying_power": _safe_float(tag_map.get("BuyingPower")),
        "gross_position_value": _safe_float(tag_map.get("GrossPositionValue")),
    }


def _pick_quote_from_market_data(md: dict[str, Any]) -> float | None:
    for field in ("last", "close"):
        val = _safe_float(md.get(field))
        if val is not None and val > 0:
            return val
    bid = _safe_float(md.get("bid")) or 0.0
    ask = _safe_float(md.get("ask")) or 0.0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if bid > 0:
        return bid
    if ask > 0:
        return ask
    und = _safe_float(md.get("und_price"))
    if und is not None and und > 0:
        return und
    return None


def _aggregate_positions(
    snapshot: dict[str, Any],
    core_symbols: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    core_set = set(core_symbols)
    market_data = snapshot.get("market_data") or {}
    md_by_symbol: dict[str, dict[str, Any]] = {}
    for entry in market_data.values():
        if isinstance(entry, dict):
            sym = str(entry.get("symbol") or "").upper()
            if sym and entry.get("sec_type", "STK") in ("STK", "ETF", ""):
                md_by_symbol[sym] = entry

    core_holdings: dict[str, dict[str, Any]] = {
        sym: {
            "symbol": sym,
            "shares": 0.0,
            "avg_cost": 0.0,
            "cost_basis_sum": 0.0,
            "tws_portfolio_price": None,
            "tws_quote_price": _pick_quote_from_market_data(md_by_symbol.get(sym, {})),
            "tws_market_value": None,
            "con_id": 0,
        }
        for sym in core_symbols
    }

    other_items: list[dict[str, Any]] = []
    totals_by_currency: dict[str, float] = {}

    for pos in snapshot.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        qty = float(_safe_float(pos.get("position")) or 0.0)
        if abs(qty) <= 1e-9:
            continue
        sym = str(pos.get("symbol") or "").strip().upper()
        sec_type = str(pos.get("sec_type") or "").strip().upper()
        currency = str(pos.get("currency") or "USD").strip().upper() or "USD"
        exchange = str(pos.get("exchange") or "").strip().upper()

        if sym in core_set and sec_type in ("STK", "ETF") and currency == "USD":
            bucket = core_holdings[sym]
            avg_cost = float(_safe_float(pos.get("avg_cost")) or 0.0)
            bucket["shares"] += qty
            if avg_cost > 0:
                bucket["cost_basis_sum"] += qty * avg_cost
            con_id = int(pos.get("con_id") or 0)
            if con_id:
                bucket["con_id"] = con_id

            pos_mp = _safe_float(pos.get("market_price"))
            data_status = str(pos.get("data_status") or "").lower()
            if pos_mp is not None and pos_mp > 0:
                if data_status == "portfolio":
                    bucket["tws_portfolio_price"] = pos_mp
                elif bucket["tws_quote_price"] is None:
                    bucket["tws_quote_price"] = pos_mp

            if con_id and con_id in market_data and isinstance(market_data[con_id], dict):
                md_price = _pick_quote_from_market_data(market_data[con_id])
                if md_price is not None and md_price > 0:
                    bucket["tws_quote_price"] = md_price

            pos_mv = _safe_float(pos.get("market_value"))
            if pos_mv is not None and abs(pos_mv) > 0:
                bucket["tws_market_value"] = (bucket["tws_market_value"] or 0.0) + pos_mv
        else:
            mv = float(_safe_float(pos.get("market_value")) or 0.0)
            totals_by_currency[currency] = totals_by_currency.get(currency, 0.0) + mv
            acct_raw = str(pos.get("account") or "")
            other_items.append(
                {
                    "account": mask_account(acct_raw) if acct_raw else "",
                    "symbol": sym,
                    "local_symbol": str(pos.get("local_symbol") or ""),
                    "sec_type": sec_type,
                    "currency": currency,
                    "exchange": exchange,
                    "position": qty,
                    "avg_cost": _safe_float(pos.get("avg_cost")) or 0.0,
                    "market_price": _safe_float(pos.get("market_price")) or 0.0,
                    "market_value": mv,
                    "unrealized_pnl": _safe_float(pos.get("unrealized_pnl")) or 0.0,
                    "expiry": str(pos.get("expiry") or ""),
                    "strike": _safe_float(pos.get("strike")) or 0.0,
                    "right": str(pos.get("right") or ""),
                }
            )

    for bucket in core_holdings.values():
        if abs(bucket["shares"]) > 1e-9 and bucket["cost_basis_sum"] > 0:
            bucket["avg_cost"] = bucket["cost_basis_sum"] / bucket["shares"]

    usd_only = set(totals_by_currency.keys()) <= {"USD"}
    other_positions = {
        "count": len(other_items),
        "total_market_value": round(totals_by_currency.get("USD", 0.0), 2) if usd_only else None,
        "totals_by_currency": {cur: round(val, 2) for cur, val in sorted(totals_by_currency.items())},
        "has_multi_currency": len(totals_by_currency) > 1,
        "items": other_items,
    }
    return core_holdings, other_positions


def _resolve_reference_prices(
    core_symbols: Sequence[str],
    core_holdings: dict[str, dict[str, Any]],
    bars_by_symbol: dict[str, list[Bar]],
    warnings: list[str],
) -> tuple[dict[str, float], dict[str, str]]:
    prices: dict[str, float] = {}
    sources: dict[str, str] = {}

    for sym in core_symbols:
        holding = core_holdings[sym]
        if holding["tws_portfolio_price"] is not None and holding["tws_portfolio_price"] > 0:
            prices[sym] = float(holding["tws_portfolio_price"])
            sources[sym] = "tws_portfolio"
            continue
        if holding["tws_quote_price"] is not None and holding["tws_quote_price"] > 0:
            prices[sym] = float(holding["tws_quote_price"])
            sources[sym] = "tws_quote"
            continue
        bars = bars_by_symbol.get(sym) or []
        if bars:
            latest_bar = max(bars, key=lambda bar: bar.date)
            if latest_bar.close > 0:
                prices[sym] = float(latest_bar.close)
                sources[sym] = "history_close"
                continue
        if holding["avg_cost"] > 0:
            prices[sym] = float(holding["avg_cost"])
            sources[sym] = "avg_cost"
            continue

        prices[sym] = 0.0
        sources[sym] = "unavailable"
        warnings.append(f"Missing reference price for {sym}; run index study --sync or connect TWS.")

    return prices, sources


def _plan_dca_underweight_orders(
    *,
    budget: float,
    plan_equity: float,
    shares_map: dict[str, float],
    target_weights: dict[str, float],
    prices: dict[str, float],
    limit_buffer_pct: float,
    commission_per_order: float,
) -> tuple[list[dict[str, Any]], dict[str, float], float]:
    active_symbols = [
        sym for sym in sorted(target_weights, key=lambda s: target_weights[s], reverse=True)
        if target_weights.get(sym, 0.0) > 0 and prices.get(sym, 0.0) > 0
    ]
    post_shares = dict(shares_map)
    if not active_symbols or budget <= commission_per_order:
        return [], post_shares, budget

    planned = {sym: 0 for sym in active_symbols}
    sim_cash = budget

    while True:
        best_sym = None
        best_deficit = 0.0
        for sym in active_symbols:
            limit_price = round(prices[sym] * (1.0 + limit_buffer_pct), 2)
            comm = commission_per_order if planned[sym] == 0 else 0.0
            if sim_cash + 1e-9 < limit_price + comm:
                continue
            current_val = (shares_map[sym] + planned[sym]) * prices[sym]
            deficit = plan_equity * target_weights[sym] - current_val
            if deficit > best_deficit:
                best_deficit = deficit
                best_sym = sym
        if best_sym is None:
            break
        limit_price = round(prices[best_sym] * (1.0 + limit_buffer_pct), 2)
        comm = commission_per_order if planned[best_sym] == 0 else 0.0
        planned[best_sym] += 1
        sim_cash -= limit_price + comm

    orders: list[dict[str, Any]] = []
    for sym in active_symbols:
        qty = planned[sym]
        if qty <= 0:
            continue
        limit_price = round(prices[sym] * (1.0 + limit_buffer_pct), 2)
        post_shares[sym] = shares_map[sym] + qty
        orders.append(
            {
                "symbol": sym,
                "action": "BUY",
                "quantity": qty,
                "reference_price": round(prices[sym], 2),
                "suggested_limit_price": limit_price,
                "estimated_notional": round(qty * prices[sym], 2),
                "reason": "定投优先补足低配核心仓位（不卖出已有持仓）",
                "manual_only": True,
            }
        )

    return orders, post_shares, sim_cash


def _plan_full_rebalance_orders(
    *,
    core_symbols: Sequence[str],
    cash_available_before: float,
    plan_equity: float,
    shares_map: dict[str, float],
    target_weights: dict[str, float],
    prices: dict[str, float],
    limit_buffer_pct: float,
    commission_per_order: float,
    status: str,
) -> tuple[list[dict[str, Any]], dict[str, float], float]:
    target_qty: dict[str, int] = {}
    for sym in core_symbols:
        w = target_weights.get(sym, 0.0)
        p = prices.get(sym, 0.0)
        if w <= 0 or p <= 0:
            target_qty[sym] = 0
        else:
            limit_buy = p * (1.0 + limit_buffer_pct)
            target_qty[sym] = floor((plan_equity * w) / limit_buy)

    post_shares = dict(shares_map)
    sell_orders: list[dict[str, Any]] = []
    cash_pool = cash_available_before

    # Sells first
    for sym in core_symbols:
        cur_qty = shares_map[sym]
        tgt = target_qty[sym]
        raw_sell = cur_qty - tgt
        if raw_sell <= 1e-9:
            continue
        if tgt == 0:
            qty: int | float = int(cur_qty) if abs(cur_qty - round(cur_qty)) < 1e-6 else round(cur_qty, 4)
        else:
            qty = floor(raw_sell)
        if qty <= 0:
            continue
        p = prices[sym]
        limit_sell = round(p * (1.0 - limit_buffer_pct), 2)
        cash_pool += float(qty) * p - commission_per_order
        post_shares[sym] = max(0.0, cur_qty - float(qty))
        if status == "REGIME_SWITCH_REQUIRED" and target_weights.get(sym, 0.0) == 0.0:
            reason = "200日均线破位切换防守，清仓转入避险资产"
        else:
            reason = "超配减仓以恢复目标权重"
        sell_orders.append(
            {
                "symbol": sym,
                "action": "SELL",
                "quantity": qty,
                "reference_price": round(p, 2),
                "suggested_limit_price": limit_sell,
                "estimated_notional": round(float(qty) * p, 2),
                "reason": reason,
                "manual_only": True,
            }
        )

    # Planned buys
    buy_symbols = [
        sym for sym in sorted(core_symbols, key=lambda s: target_weights.get(s, 0.0), reverse=True)
        if target_weights.get(sym, 0.0) > 0 and prices.get(sym, 0.0) > 0
    ]
    planned_buys: dict[str, int] = {}
    for sym in buy_symbols:
        delta = target_qty[sym] - floor(post_shares[sym])
        if delta > 0:
            planned_buys[sym] = delta

    def _total_buy_outlay(buys: dict[str, int]) -> float:
        outlay = 0.0
        for sym, qty in buys.items():
            if qty > 0:
                limit_buy = round(prices[sym] * (1.0 + limit_buffer_pct), 2)
                outlay += qty * limit_buy + commission_per_order
        return outlay

    # Greedy trim if conservative limit pricing + commissions exceed available cash_pool
    while planned_buys and _total_buy_outlay(planned_buys) > cash_pool + 1e-9:
        candidates = [sym for sym, qty in planned_buys.items() if qty > 0]
        if not candidates:
            break
        trim_sym = min(
            candidates,
            key=lambda s: (
                (plan_equity * target_weights[s] - (post_shares[s] + planned_buys[s]) * prices[s]),
                target_weights[s],
            ),
        )
        planned_buys[trim_sym] -= 1
        if planned_buys[trim_sym] <= 0:
            del planned_buys[trim_sym]

    buy_orders: list[dict[str, Any]] = []
    for sym in buy_symbols:
        qty = planned_buys.get(sym, 0)
        if qty <= 0:
            continue
        p = prices[sym]
        limit_buy = round(p * (1.0 + limit_buffer_pct), 2)
        cash_pool -= qty * limit_buy + commission_per_order
        post_shares[sym] = post_shares[sym] + qty
        if status == "INITIAL_ALLOCATION":
            reason = "初始建仓至目标配置"
        elif status == "REGIME_SWITCH_REQUIRED":
            reason = "200日均线切换，按目标配比买入"
        else:
            reason = "低配补仓至目标权重"
        buy_orders.append(
            {
                "symbol": sym,
                "action": "BUY",
                "quantity": qty,
                "reference_price": round(p, 2),
                "suggested_limit_price": limit_buy,
                "estimated_notional": round(qty * p, 2),
                "reason": reason,
                "manual_only": True,
            }
        )

    return sell_orders + buy_orders, post_shares, cash_pool


def _build_single_plan(
    *,
    plan_id: str,
    name: str,
    description: str,
    is_trend_plan: bool,
    core_symbols: Sequence[str],
    effective_defensive: str,
    target_weights: dict[str, float],
    shares_map: dict[str, float],
    prices: dict[str, float],
    price_sources: dict[str, str],
    cash_base: float,
    extra_contribution: float,
    core_positions_value: float,
    plan_equity: float,
    drift_threshold_pct: float,
    limit_buffer_pct: float,
    commission_per_order: float,
) -> dict[str, Any]:
    current_weights = {
        sym: ((shares_map[sym] * prices[sym]) / plan_equity if plan_equity > 0 else 0.0)
        for sym in core_symbols
    }
    max_drift = max(
        (abs(current_weights[sym] - target_weights.get(sym, 0.0)) for sym in core_symbols),
        default=0.0,
    )

    required_symbols = [
        sym for sym in core_symbols
        if target_weights.get(sym, 0.0) > 0 or abs(shares_map.get(sym, 0.0)) > 1e-9
    ]
    missing_price = any(prices.get(sym, 0.0) <= 0 for sym in required_symbols)

    if plan_equity <= 0:
        status = "NO_CAPITAL"
    elif missing_price:
        status = "MISSING_PRICE"
    elif core_positions_value <= 0:
        status = "INITIAL_ALLOCATION"
    elif is_trend_plan and (
        any(target_weights.get(sym, 0.0) == 0.0 and shares_map.get(sym, 0.0) > 1e-9 for sym in core_symbols)
        or (
            target_weights.get(effective_defensive, 0.0) > 0
            and abs(current_weights.get(effective_defensive, 0.0) - target_weights.get(effective_defensive, 0.0)) >= drift_threshold_pct
        )
    ):
        status = "REGIME_SWITCH_REQUIRED"
    elif max_drift >= drift_threshold_pct:
        status = "REBALANCE_TRIGGERED"
    elif extra_contribution > 0:
        status = "DCA_BUY_ONLY"
    else:
        status = "HOLD"

    cash_pool = cash_base + extra_contribution
    if status in ("HOLD", "NO_CAPITAL", "MISSING_PRICE"):
        suggested_orders: list[dict[str, Any]] = []
        post_shares = dict(shares_map)
        estimated_cash_after = cash_pool
    elif status == "DCA_BUY_ONLY":
        suggested_orders, post_shares, remaining_budget = _plan_dca_underweight_orders(
            budget=extra_contribution,
            plan_equity=plan_equity,
            shares_map=shares_map,
            target_weights=target_weights,
            prices=prices,
            limit_buffer_pct=limit_buffer_pct,
            commission_per_order=commission_per_order,
        )
        estimated_cash_after = cash_base + remaining_budget
    else:
        suggested_orders, post_shares, estimated_cash_after = _plan_full_rebalance_orders(
            core_symbols=core_symbols,
            cash_available_before=cash_pool,
            plan_equity=plan_equity,
            shares_map=shares_map,
            target_weights=target_weights,
            prices=prices,
            limit_buffer_pct=limit_buffer_pct,
            commission_per_order=commission_per_order,
            status=status,
        )

    post_equity = max(
        0.0,
        estimated_cash_after + sum(post_shares[s] * prices[s] for s in core_symbols),
    )

    weight_rows: list[dict[str, Any]] = []
    for sym in core_symbols:
        mv = shares_map[sym] * prices[sym]
        cur_w = current_weights[sym] * 100.0
        tgt_w = target_weights.get(sym, 0.0) * 100.0
        post_mv = post_shares[sym] * prices[sym]
        post_w = (post_mv / post_equity * 100.0) if post_equity > 0 else 0.0
        weight_rows.append(
            {
                "symbol": sym,
                "shares": round(shares_map[sym], 4),
                "price": round(prices[sym], 2),
                "price_source": price_sources[sym],
                "market_value": round(mv, 2),
                "current_weight_pct": round(cur_w, 2),
                "target_weight_pct": round(tgt_w, 2),
                "drift_pct": round(cur_w - tgt_w, 2),
                "post_trade_shares": round(post_shares[sym], 4),
                "post_trade_weight_pct": round(post_w, 2),
            }
        )

    summary_messages = {
        "NO_CAPITAL": "当前无可用资金或模拟资金为 0；可输入“离线模拟资金”或“新增定投”预览下单计划。",
        "MISSING_PRICE": "部分核心 ETF 缺少有效参考价格，请先连接 TWS 或同步历史日线。",
        "INITIAL_ALLOCATION": f"当前尚未持有核心 ETF，可按目标比例建仓（共 {len(suggested_orders)} 笔手工参考委托）。",
        "REGIME_SWITCH_REQUIRED": f"200 日趋势缓冲信号触发切仓（最大偏离 {max_drift * 100:.2f}%），建议按清单手动调仓。",
        "REBALANCE_TRIGGERED": f"最大权重偏离 {max_drift * 100:.2f}% 已达再平衡阈值（{drift_threshold_pct * 100:.1f}%），建议按清单调仓。",
        "DCA_BUY_ONLY": (
            f"未触及再平衡阈值（最大偏离 {max_drift * 100:.2f}%），新增定投 ${extra_contribution:,.2f} 优先买入低配标的。"
            if suggested_orders
            else f"新增定投 ${extra_contribution:,.2f} 不足以按限价买入 1 股，建议先留存现金待下次合并买入。"
        ),
        "HOLD": f"当前最大权重偏离 {max_drift * 100:.2f}%，低于 {drift_threshold_pct * 100:.1f}% 阈值，继续持有观望即可。",
    }

    return {
        "plan_id": plan_id,
        "name": name,
        "description": description,
        "status": status,
        "summary_text": summary_messages[status],
        "max_drift_pct": round(max_drift * 100.0, 2),
        "drift_threshold_pct": round(drift_threshold_pct * 100.0, 2),
        "rebalance_triggered": status in ("INITIAL_ALLOCATION", "REBALANCE_TRIGGERED", "REGIME_SWITCH_REQUIRED"),
        "target_weights": {sym: round(target_weights.get(sym, 0.0), 4) for sym in core_symbols},
        "weight_rows": weight_rows,
        "suggested_orders": suggested_orders,
        "estimated_cash_after": round(estimated_cash_after, 2),
    }


def build_index_core_report(
    snapshot: dict[str, Any],
    bars_by_symbol: dict[str, list[Bar]],
    *,
    extra_contribution: float = 0.0,
    drift_threshold_pct: float = 0.05,
    trend_window: int = 200,
    trend_buffer_pct: float = 0.02,
    defensive_symbol: str = "BIL",
    target_spy_weight: float = 0.70,
    target_qqq_weight: float = 0.30,
    limit_buffer_pct: float = 0.002,
    commission_per_order: float = 1.0,
    override_cash: float | None = None,
    odd_lot_summary: dict[str, Any] | None = None,
    benchmark_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a strictly read-only Index Core Monitor & Rebalance Advisory report."""
    warnings: list[str] = []

    effective_defensive = defensive_symbol.strip().upper() or "BIL"
    if effective_defensive == "BIL" and not bars_by_symbol.get("BIL") and bars_by_symbol.get("SHY"):
        effective_defensive = "SHY"
        warnings.append("BIL history unavailable; falling back to SHY as defensive Treasury ETF.")

    core_symbols = list(dict.fromkeys(["SPY", "QQQ", effective_defensive]))

    account = _parse_account_summary(snapshot)
    connected = bool(snapshot.get("connected", False))
    connection_mode = str(snapshot.get("connection_mode") or "Unknown")
    last_update = snapshot.get("last_update")
    last_update_iso = last_update.isoformat() if hasattr(last_update, "isoformat") else last_update

    if override_cash is not None:
        cash_base = float(override_cash)
        is_simulated_cash = True
        cash_source = "override_cash"
    elif account["settled_cash"] is not None and account["settled_cash"] > 0:
        cash_base = float(account["settled_cash"])
        is_simulated_cash = False
        cash_source = "settled_cash"
    elif account["total_cash"] is not None and account["total_cash"] >= 0:
        cash_base = float(account["total_cash"])
        is_simulated_cash = False
        cash_source = "total_cash"
    elif account["available_funds"] is not None and account["available_funds"] >= 0:
        cash_base = float(account["available_funds"])
        is_simulated_cash = False
        cash_source = "available_funds"
    else:
        cash_base = 0.0
        is_simulated_cash = False
        cash_source = "none"

    if not connected and override_cash is None:
        warnings.append("TWS 未连接，当前仅可结合缓存日线查看 200 日趋势信号；可填入“离线模拟资金”演练调仓。")

    core_holdings, other_positions = _aggregate_positions(snapshot, core_symbols)
    prices, price_sources = _resolve_reference_prices(
        core_symbols, core_holdings, bars_by_symbol, warnings
    )
    shares_map = {sym: float(core_holdings[sym]["shares"]) for sym in core_symbols}

    core_positions_value = sum(shares_map[sym] * prices[sym] for sym in core_symbols)
    plan_equity = core_positions_value + cash_base + float(extra_contribution)

    regimes = {
        sym: evaluate_trend_regime(
            sym,
            bars_by_symbol.get(sym, []),
            trend_window=trend_window,
            trend_buffer_pct=trend_buffer_pct,
        )
        for sym in ("SPY", "QQQ")
    }

    plan_a_weights: dict[str, float] = {
        "SPY": float(target_spy_weight),
        "QQQ": float(target_qqq_weight),
    }
    if effective_defensive not in plan_a_weights:
        plan_a_weights[effective_defensive] = 0.0

    plan_b_weights: dict[str, float] = {}
    freed_weight = 0.0
    for sym, base_w in (("SPY", float(target_spy_weight)), ("QQQ", float(target_qqq_weight))):
        regime = regimes[sym]
        if not regime["available"]:
            warnings.append(f"trend_regime_unavailable:{sym} (defaulting to RISK_ON weight).")
            plan_b_weights[sym] = base_w
        elif regime["risk_on"]:
            plan_b_weights[sym] = base_w
        else:
            plan_b_weights[sym] = 0.0
            freed_weight += base_w
    plan_b_weights[effective_defensive] = plan_b_weights.get(effective_defensive, 0.0) + freed_weight

    plan_a = _build_single_plan(
        plan_id="drift_rebalance",
        name="方案 A：SPY 70% / QQQ 30%（5% 偏离阈值再平衡）",
        description="长期满仓持有美股核心宽基，仅当偏离达到阈值或新增定投时微调，极低换手。",
        is_trend_plan=False,
        core_symbols=core_symbols,
        effective_defensive=effective_defensive,
        target_weights=plan_a_weights,
        shares_map=shares_map,
        prices=prices,
        price_sources=price_sources,
        cash_base=cash_base,
        extra_contribution=float(extra_contribution),
        core_positions_value=core_positions_value,
        plan_equity=plan_equity,
        drift_threshold_pct=drift_threshold_pct,
        limit_buffer_pct=limit_buffer_pct,
        commission_per_order=commission_per_order,
    )

    plan_b = _build_single_plan(
        plan_id="trend_200sma_bil",
        name=f"方案 B：SPY 70% / QQQ 30% + 200 日均线 ±{trend_buffer_pct * 100:.0f}% 切 {effective_defensive}",
        description=f"收盘价跌破 200 日均线 -{trend_buffer_pct * 100:.0f}% 切换至短期美债 {effective_defensive} 防守，重新站上 +{trend_buffer_pct * 100:.0f}% 恢复进攻。",
        is_trend_plan=True,
        core_symbols=core_symbols,
        effective_defensive=effective_defensive,
        target_weights=plan_b_weights,
        shares_map=shares_map,
        prices=prices,
        price_sources=price_sources,
        cash_base=cash_base,
        extra_contribution=float(extra_contribution),
        core_positions_value=core_positions_value,
        plan_equity=plan_equity,
        drift_threshold_pct=drift_threshold_pct,
        limit_buffer_pct=limit_buffer_pct,
        commission_per_order=commission_per_order,
    )

    if other_positions["count"] > 0:
        warnings.append(
            f"检测到 {other_positions['count']} 项非核心/期权持仓（未纳入指数再平衡拆算），详见下方明细。"
        )

    return {
        "read_only": True,
        "manual_execution_only": True,
        "connection": {
            "connected": connected,
            "connection_mode": connection_mode,
            "data_freshness": str(snapshot.get("data_freshness") or "stale"),
            "last_update": last_update_iso,
        },
        "account": account,
        "capital": {
            "core_positions_value": round(core_positions_value, 2),
            "cash_base": round(cash_base, 2),
            "cash_source": cash_source,
            "is_simulated_cash": is_simulated_cash,
            "extra_contribution": round(float(extra_contribution), 2),
            "plan_equity": round(plan_equity, 2),
            "defensive_symbol": effective_defensive,
        },
        "regimes": regimes,
        "plans": {
            "drift_rebalance": plan_a,
            "trend_200sma_bil": plan_b,
        },
        "other_positions": other_positions,
        "odd_lot_summary": odd_lot_summary or {
            "available": False,
            "active_opportunities": [],
            "expired_count": 0,
        },
        "benchmark_summary": benchmark_summary or {
            "available": False,
            "index_strategies": [],
            "dca_strategies": [],
        },
        "warnings": warnings,
    }
