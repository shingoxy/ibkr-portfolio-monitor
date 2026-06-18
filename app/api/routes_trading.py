from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings

router = APIRouter()


def _parse_watchlist(raw: str) -> list[str]:
    return [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()]


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
