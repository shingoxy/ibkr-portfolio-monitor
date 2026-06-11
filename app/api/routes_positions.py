"""持仓路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "positions.html"
    )


@router.get("/api/positions")
async def api_positions(
    request: Request,
    sec_type: str = Query("", description="筛选类型: STK, OPT, ETF"),
    symbol: str = Query("", description="筛选标的"),
    sort_by: str = Query("symbol", description="排序字段"),
    sort_dir: str = Query("asc", description="排序方向: asc, desc"),
):
    manager = request.app.state.manager
    snapshot = manager.cache.get_snapshot()
    positions = snapshot["positions"]

    # 注入行情数据
    mkt = snapshot.get("market_data", {})
    for p in positions:
        con_id = p.get("con_id", 0)
        if con_id in mkt:
            md = mkt[con_id]
            p["delta"] = md.get("delta") or 0
            p["gamma"] = md.get("gamma") or 0
            p["theta"] = md.get("theta") or 0
            p["vega"] = md.get("vega") or 0
            p["implied_vol"] = md.get("implied_vol") or 0
            p["und_price"] = md.get("und_price") or 0

    # 筛选
    if sec_type:
        positions = [p for p in positions if p.get("sec_type", "").upper() == sec_type.upper()]
    if symbol:
        positions = [p for p in positions if symbol.upper() in p.get("symbol", "").upper()]

    # 排序
    reverse = sort_dir == "desc"
    if sort_by in ("unrealized_pnl", "daily_pnl", "market_value", "position",
                   "avg_cost", "market_price", "strike", "delta"):
        positions.sort(key=lambda p: p.get(sort_by, 0) or 0, reverse=reverse)
    elif sort_by == "expiry":
        positions.sort(key=lambda p: p.get("expiry", "") or "", reverse=reverse)
    else:
        positions.sort(key=lambda p: p.get("symbol", ""), reverse=reverse)

    return positions


@router.get("/api/positions/summary")
async def api_positions_summary(request: Request):
    manager = request.app.state.manager
    positions = manager.cache.get_snapshot()["positions"]
    stock_pos = [p for p in positions if p.get("sec_type") == "STK"]
    opt_pos = [p for p in positions if p.get("sec_type") == "OPT"]
    return {
        "total_market_value": sum(p.get("market_value", 0) for p in positions),
        "total_unrealized_pnl": sum(p.get("unrealized_pnl", 0) for p in positions),
        "total_realized_pnl": sum(p.get("realized_pnl", 0) for p in positions),
        "total_daily_pnl": sum(p.get("daily_pnl", 0) for p in positions),
        "stock_count": len(stock_pos),
        "option_count": len(opt_pos),
        "positions": positions,
    }
