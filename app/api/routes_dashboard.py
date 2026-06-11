"""仪表盘路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "dashboard.html"
    )


@router.get("/api/account")
async def api_account(request: Request):
    manager = request.app.state.manager
    snapshot = manager.cache.get_snapshot()
    acct_map = {item["tag"]: item["value"] for item in snapshot["account_summary"]}
    return {
        "account": snapshot["accounts"][0] if snapshot["accounts"] else "",
        "items": acct_map,
        "pnl": snapshot.get("pnl", {}),
        "connected": snapshot["connected"],
        "connection_mode": snapshot.get("connection_mode", "Unknown"),
        "data_freshness": snapshot.get("data_freshness", "stale"),
        "last_update": snapshot["last_update"].isoformat() if snapshot["last_update"] else None,
    }


@router.get("/api/status")
async def api_status(request: Request):
    manager = request.app.state.manager
    return manager.get_connection_status()


@router.post("/api/refresh")
async def refresh_data(request: Request):
    manager = request.app.state.manager
    manager.refresh_all()
    return {"status": "ok", "message": "数据刷新已触发"}
