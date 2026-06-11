"""订单路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "orders.html"
    )


@router.get("/api/orders")
async def api_orders(request: Request):
    manager = request.app.state.manager
    return manager.cache.get_snapshot().get("orders", [])


@router.get("/api/orders/completed")
async def api_completed_orders(request: Request):
    manager = request.app.state.manager
    return manager.cache.get_snapshot().get("completed_orders", [])


@router.get("/api/executions")
async def api_executions(request: Request):
    manager = request.app.state.manager
    return manager.cache.get_snapshot().get("executions", [])
