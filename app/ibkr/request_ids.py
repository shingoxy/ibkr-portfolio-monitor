"""请求 ID 管理器：分配、回收、冲突检测。"""

from __future__ import annotations

import threading


class RequestIdManager:
    """线程安全的 reqId 分配器。"""

    # 预留 ID 段
    RESERVED_ACCOUNT_SUMMARY = 9000
    RESERVED_PNL = 9100
    RESERVED_POSITIONS = 9200
    RESERVED_ORDERS = 9300
    RESERVED_EXECUTIONS = 9400
    RESERVED_MARKET_DATA_START = 1000
    RESERVED_MARKET_DATA_END = 8999

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1000
        self._allocated: set[int] = set()
        self._category_map: dict[str, list[int]] = {}

    def allocate(self, category: str = "general") -> int:
        """分配一个新的 reqId。"""
        with self._lock:
            req_id = self._next_id
            while req_id in self._allocated:
                self._next_id += 1
                req_id = self._next_id
                if self._next_id > 99999:
                    self._next_id = 1000

            self._allocated.add(req_id)
            self._category_map.setdefault(category, []).append(req_id)
            self._next_id += 1
            return req_id

    def release(self, req_id: int) -> None:
        """释放一个 reqId。"""
        with self._lock:
            self._allocated.discard(req_id)
            for cat, ids in self._category_map.items():
                if req_id in ids:
                    ids.remove(req_id)
                    break

    def release_category(self, category: str) -> None:
        """释放某个类别的所有 reqId。"""
        with self._lock:
            ids = self._category_map.pop(category, [])
            for rid in ids:
                self._allocated.discard(rid)

    def is_allocated(self, req_id: int) -> bool:
        """检查 reqId 是否已分配。"""
        with self._lock:
            return req_id in self._allocated

    def get_by_category(self, category: str) -> list[int]:
        """获取某个类别的所有 reqId。"""
        with self._lock:
            return list(self._category_map.get(category, []))
