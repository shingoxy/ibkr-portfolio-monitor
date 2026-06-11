"""快照编排服务。"""

from __future__ import annotations

import logging
from datetime import datetime

from app.ibkr.connection_manager import ConnectionManager
from app.repositories.snapshot_repository import SnapshotRepository

logger = logging.getLogger(__name__)


class SnapshotService:
    """编排数据获取和快照存储。"""

    def __init__(self, manager: ConnectionManager, repo: SnapshotRepository) -> None:
        self._manager = manager
        self._repo = repo

    async def save_current_snapshot(self) -> dict:
        """将当前缓存数据保存为快照。"""
        snapshot = self._manager.cache.get_snapshot()
        now = datetime.utcnow()
        errors = []

        account = snapshot["accounts"][0] if snapshot["accounts"] else ""

        try:
            if snapshot["account_summary"]:
                await self._repo.save_account_snapshot(account, snapshot["account_summary"])
        except Exception as e:
            errors.append(f"账户数据保存失败: {e}")
            logger.error("保存账户快照失败: %s", e)

        try:
            if snapshot["positions"]:
                await self._repo.save_position_snapshots(snapshot["positions"])
        except Exception as e:
            errors.append(f"持仓数据保存失败: {e}")
            logger.error("保存持仓快照失败: %s", e)

        try:
            if snapshot["orders"]:
                await self._repo.save_order_snapshots(snapshot["orders"])
        except Exception as e:
            errors.append(f"订单数据保存失败: {e}")
            logger.error("保存订单快照失败: %s", e)

        return {
            "snapshot_id": f"snap_{now.strftime('%Y%m%d_%H%M%S')}",
            "requested_at": now.isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "connection_status": "connected" if snapshot["connected"] else "disconnected",
            "account_data_complete": bool(snapshot["account_summary"]),
            "positions_complete": bool(snapshot["positions"]),
            "market_data_complete": bool(snapshot["market_data"]),
            "orders_complete": True,
            "errors": errors,
            "stale_fields": [] if snapshot["data_freshness"] == "fresh" else ["all"],
        }

    async def auto_snapshot_and_cleanup(self, keep_hours: int = 72) -> None:
        """自动快照 + 清理旧数据。"""
        try:
            await self.save_current_snapshot()
            logger.debug("自动快照完成")
        except Exception as e:
            logger.error("自动快照失败: %s", e)

        try:
            await self._repo.cleanup_old_snapshots(keep_hours)
        except Exception as e:
            logger.error("清理旧快照失败: %s", e)
