"""导出路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from app.services.export_service import ExportService

router = APIRouter()


@router.get("/api/export/markdown")
async def export_markdown(request: Request):
    manager = request.app.state.manager
    svc = ExportService(manager)
    result = svc.export_markdown()
    return PlainTextResponse(result.content, media_type="text/markdown; charset=utf-8")


@router.get("/api/export/json")
async def export_json(request: Request, local: bool = Query(False)):
    manager = request.app.state.manager
    svc = ExportService(manager)
    result = svc.export_json(local=local)
    return JSONResponse(content={
        "format": "json",
        "content": result.content,
        "exported_at": result.exported_at.isoformat() if result.exported_at else None,
    })
