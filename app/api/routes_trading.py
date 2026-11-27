from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.trading.index_advisor import (
    build_index_core_report,
    load_cached_bars,
    load_index_study_summary,
    load_odd_lot_summary,
)

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HISTORY_DIR = PROJECT_ROOT / "data" / "history"
BACKTESTS_DIR = PROJECT_ROOT / "data" / "backtests"


def _parse_watchlist(raw: str) -> list[str]:
    return [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()]


def _empty_disconnected_snapshot() -> dict:
    return {
        "connected": False,
        "connection_mode": "Unknown",
        "accounts": [],
        "account_summary": [],
        "positions": [],
        "market_data": {},
        "portfolio_values": {},
        "data_freshness": "stale",
        "last_update": None,
    }


@router.get("/trading", response_class=HTMLResponse)
async def trading_page(request: Request):
    return request.app.state.templates.TemplateResponse(request, "trading.html")


@router.get("/api/trading/config")
async def api_trading_config():
    settings = get_settings()
    return {
        "enabled": settings.trading_enabled,
        "mode": settings.trading_mode,
        "watchlist": _parse_watchlist(settings.trading_watchlist),
        "max_positions": settings.trading_max_positions,
        "max_total_exposure_pct": settings.trading_max_total_exposure_pct,
        "max_symbol_exposure_pct": settings.trading_max_symbol_exposure_pct,
        "risk_per_trade_pct": settings.trading_risk_per_trade_pct,
        "min_cash_reserve_pct": settings.trading_min_cash_reserve_pct,
    }


@router.get("/api/trading/index-core")
async def api_trading_index_core(
    request: Request,
    extra_contribution: float = Query(0.0, ge=0.0, le=1_000_000.0),
    drift_threshold_pct: float = Query(0.05, gt=0.0, lt=0.5),
    trend_buffer_pct: float = Query(0.02, ge=0.0, lt=0.2),
    defensive_symbol: str = Query("BIL"),
    override_cash: float | None = Query(None, ge=0.0, le=10_000_000.0),
):
    manager = getattr(request.app.state, "manager", None)
    snapshot = manager.cache.get_snapshot() if manager is not None else _empty_disconnected_snapshot()

    clean_defensive = defensive_symbol.strip().upper() or "BIL"
    bars_by_symbol = load_cached_bars(
        HISTORY_DIR,
        ["SPY", "QQQ", clean_defensive, "SHY"],
    )
    odd_lot_summary = load_odd_lot_summary(BACKTESTS_DIR / "odd-lot-scan-latest.json")
    benchmark_summary = load_index_study_summary(BACKTESTS_DIR / "index-study-latest.json")

    return build_index_core_report(
        snapshot,
        bars_by_symbol,
        extra_contribution=extra_contribution,
        drift_threshold_pct=drift_threshold_pct,
        trend_buffer_pct=trend_buffer_pct,
        defensive_symbol=clean_defensive,
        override_cash=override_cash,
        odd_lot_summary=odd_lot_summary,
        benchmark_summary=benchmark_summary,
    )
