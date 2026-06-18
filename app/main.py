"""IBKR 本地只读持仓分析工具 - 应用入口。"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.logging_config import check_read_only, setup_logging
from app.repositories.snapshot_repository import SnapshotRepository
from app.ibkr.connection_manager import ConnectionManager
from app.services.snapshot_service import SnapshotService

logger = logging.getLogger(__name__)


async def _background_snapshot_task(manager: ConnectionManager, repo: SnapshotRepository):
    """后台定时快照任务（每 60 秒）。"""
    svc = SnapshotService(manager, repo)
    while True:
        try:
            await asyncio.sleep(60)
            if manager.connected:
                await svc.auto_snapshot_and_cleanup(keep_hours=72)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("后台快照任务异常: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    check_read_only()
    setup_logging(settings.log_level)

    logger.info("=" * 60)
    logger.info("IBKR 持仓分析工具启动")
    logger.info("模式: 只读")
    logger.info("地址: http://%s:%d", settings.app_host, settings.app_port)
    logger.info("=" * 60)

    repo = SnapshotRepository(settings.database_url)
    await repo.init()

    manager = ConnectionManager({
        "host": settings.ibkr_host,
        "port": settings.ibkr_port,
        "client_id": settings.ibkr_client_id,
        "account": settings.ibkr_account,
        "max_retries": settings.reconnect_max_retries,
        "base_delay": settings.reconnect_base_delay,
        "max_delay": settings.reconnect_max_delay,
        "proxy_host": settings.proxy_host,
        "proxy_port": settings.proxy_port,
        "proxy_type": settings.proxy_type,
    })
    app.state.manager = manager
    app.state.repo = repo

    manager.start()

    # 启动后台快照任务
    snapshot_task = asyncio.create_task(
        _background_snapshot_task(manager, repo)
    )

    yield

    logger.info("正在关闭...")
    snapshot_task.cancel()
    try:
        await snapshot_task
    except asyncio.CancelledError:
        pass
    manager.stop()
    await repo.close()
    logger.info("已关闭")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="IBKR 持仓分析",
        description="Interactive Brokers 本地只读持仓分析工具",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
    app.state.templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

    from app.api import routes_dashboard, routes_positions, routes_strategies, routes_orders, routes_export, routes_trading
    app.include_router(routes_dashboard.router)
    app.include_router(routes_positions.router)
    app.include_router(routes_strategies.router)
    app.include_router(routes_orders.router)
    app.include_router(routes_export.router)
    app.include_router(routes_trading.router)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
