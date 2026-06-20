"""IBKR TWS API 客户端实现（EWrapper + EClient）。

严格只读：不实现 placeOrder, cancelOrder, reqGlobalCancel, exerciseOptions。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import TickerId, OrderId

from app.logging_config import validate_no_forbidden_methods
from app.ibkr.errors import (
    translate_error, is_connectivity_error, is_info_message,
    requires_resubscribe, CONNECTIVITY_CODES,
)

logger = logging.getLogger(__name__)

# 完整的账户摘要标签列表（修正：ReqTEquity 非 RegTEquity）
ACCOUNT_TAGS = ",".join([
    "AccountType",
    "NetLiquidation",
    "TotalCashValue",
    "SettledCash",
    "AccruedCash",
    "BuyingPower",
    "EquityWithLoanValue",
    "PreviousEquityWithLoanValue",
    "GrossPositionValue",
    "ReqTEquity",
    "ReqTMargin",
    "SMA",
    "InitMarginReq",
    "MaintMarginReq",
    "FullInitMarginReq",
    "FullMaintMarginReq",
    "AvailableFunds",
    "ExcessLiquidity",
    "FullAvailableFunds",
    "FullExcessLiquidity",
    "LookAheadNextChange",
    "LookAheadInitMarginReq",
    "LookAheadMaintMarginReq",
    "LookAheadAvailableFunds",
    "LookAheadExcessLiquidity",
    "Cushion",
    "DayTradesRemaining",
    "Leverage",
    "HighestSeverity",
])


class IBKRClient(EWrapper, EClient):
    """IBKR TWS API 客户端，仅实现只读回调。"""

    def __init__(self) -> None:
        EClient.__init__(self, self)

        # 连接状态
        self.connected_event = threading.Event()
        self.accounts: list[str] = []

        # 连接状态变化信号
        self._connection_lost = threading.Event()
        self._connection_restored = threading.Event()
        self._need_resubscribe = threading.Event()

        # 数据队列
        self.account_summary_queue: queue.Queue = queue.Queue()
        self.account_summary_end = threading.Event()
        self.position_queue: queue.Queue = queue.Queue()
        self.position_end = threading.Event()
        self.order_queue: queue.Queue = queue.Queue()
        self.order_end = threading.Event()
        self.execution_queue: queue.Queue = queue.Queue()
        self.execution_end = threading.Event()
        self.completed_order_queue: queue.Queue = queue.Queue()
        self.completed_order_end = threading.Event()
        self.historical_data_queue: queue.Queue = queue.Queue()
        self.historical_data_end_queue: queue.Queue = queue.Queue()

        # PnL 数据（流式推送）
        self._pnl_lock = threading.Lock()
        self._pnl_data: dict = {}  # reqId -> {daily, unrealized, realized}
        self._pnl_single_data: dict = {}  # reqId -> {pos, daily, unrealized, realized, value}

        # 行情数据
        self.market_data_queue: queue.Queue = queue.Queue()
        self._ticker_contracts: dict[int, dict] = {}
        self._ticker_counter = 1000
        self._ticker_lock = threading.Lock()

        # 错误队列
        self.error_queue: queue.Queue = queue.Queue()

        # 当前账户
        self.current_account: str = ""

        # 连接模式
        self._is_paper: bool | None = None  # None=未知, True=Paper, False=Live

        # 校验无禁止方法
        validate_no_forbidden_methods(self.__class__)

    # ── 连接回调 ──────────────────────────────────────────────────────

    def nextValidId(self, orderId: OrderId) -> None:
        """连接成功回调。"""
        logger.info("连接成功，NextValidId=%d", orderId)
        self.connected_event.set()
        self._connection_lost.clear()

    def managedAccounts(self, accountsList: str) -> None:
        """接收到管理的账户列表。"""
        self.accounts = [a.strip() for a in accountsList.split(",") if a.strip()]
        if self.accounts:
            self.current_account = self.accounts[0]
        logger.info("管理账户: %s", [a[:4] + "****" for a in self.accounts])

    def error(self, reqId: TickerId, errorCode: int, errorString: str,
              advancedOrderRejectJson: str = "") -> None:
        """错误和信息回调。"""
        # 信息性消息
        if is_info_message(errorCode):
            logger.debug("IBKR 信息: code=%d msg=%s", errorCode, errorString)
            return

        # 连接状态变化
        if errorCode in CONNECTIVITY_CODES:
            msg = translate_error(errorCode)
            logger.warning("连接状态变化: %s", msg)

            if errorCode == 1100:
                self._connection_lost.set()
                self._connection_restored.clear()
            elif errorCode == 1101:
                self._connection_restored.set()
                self._need_resubscribe.set()
                self._connection_lost.clear()
            elif errorCode == 1102:
                self._connection_restored.set()
                self._connection_lost.clear()

            self.error_queue.put({
                "reqId": reqId, "errorCode": errorCode,
                "errorString": msg, "type": "connectivity",
            })
            return

        # 连接错误
        if errorCode in (502, 503, 504, 509, 520):
            msg = translate_error(errorCode)
            logger.error("连接错误: %s", msg)
            self.error_queue.put({
                "reqId": reqId, "errorCode": errorCode,
                "errorString": msg, "type": "connection",
            })
            return

        # 行情数据类型提示（非错误）
        if errorCode in (354, 10168):
            logger.info("行情提示: %s", errorString)
            return

        # 数据请求错误
        msg = translate_error(errorCode)
        logger.warning("IBKR 错误: reqId=%d code=%d msg=%s (%s)",
                        reqId, errorCode, errorString, msg)
        self.error_queue.put({
            "reqId": reqId, "errorCode": errorCode,
            "errorString": errorString, "type": "data",
        })

    # ── 账户摘要回调 ──────────────────────────────────────────────────

    def accountSummary(self, reqId: int, account: str, tag: str,
                       value: str, currency: str) -> None:
        self.account_summary_queue.put({
            "account": account, "tag": tag,
            "value": value, "currency": currency,
        })

    def accountSummaryEnd(self, reqId: int) -> None:
        logger.info("账户摘要传输完成 reqId=%d", reqId)
        self.account_summary_end.set()

    # ── 持仓回调 ──────────────────────────────────────────────────────

    def position(self, account: str, contract: Contract,
                 pos: float, avgCost: float) -> None:
        """接收到单条持仓数据。

        注意: avgCost 是每股/每单位成本，不含 multiplier。
        期权总成本 = position * avgCost * multiplier
        """
        item = {
            "account": account,
            "symbol": contract.symbol,
            "local_symbol": getattr(contract, "localSymbol", "") or "",
            "sec_type": contract.secType,
            "con_id": contract.conId,
            "currency": contract.currency or "USD",
            "exchange": contract.exchange or "",
            "primary_exchange": getattr(contract, "primaryExchange", "") or "",
            "position": pos,
            "avg_cost": avgCost,
            "strike": getattr(contract, "strike", 0.0) or 0.0,
            "right": getattr(contract, "right", "") or "",
            "expiry": getattr(contract, "lastTradeDateOrContractMonth", "") or "",
            "multiplier": float(getattr(contract, "multiplier", 100) or 100),
            "trading_class": getattr(contract, "tradingClass", "") or "",
        }
        self.position_queue.put(item)

    def positionEnd(self) -> None:
        logger.info("持仓传输完成")
        self.position_end.set()

    # ── 订单回调 ──────────────────────────────────────────────────────

    def openOrder(self, orderId: int, contract: Contract, order: Any,
                  orderState: Any) -> None:
        item = {
            "account": getattr(order, "account", self.current_account) or self.current_account,
            "order_id": orderId,
            "perm_id": getattr(order, "permId", 0),
            "symbol": contract.symbol,
            "sec_type": contract.secType,
            "action": getattr(order, "action", "") or "",
            "order_type": getattr(order, "orderType", "") or "",
            "total_qty": getattr(order, "totalQuantity", 0) or 0,
            "lmt_price": getattr(order, "lmtPrice", 0) or 0,
            "aux_price": getattr(order, "auxPrice", 0) or 0,
            "stop_price": getattr(order, "auxPrice", 0) or 0,
            "parent_id": getattr(order, "parentId", 0),
            "oca_group": getattr(order, "ocaGroup", "") or "",
            "status": getattr(orderState, "status", "") or "",
            "filled": 0.0,
            "remaining": getattr(order, "totalQuantity", 0) or 0,
            "avg_fill_price": 0.0,
        }
        self.order_queue.put(item)

    def orderStatus(self, orderId: int, status: str, filled: float,
                    remaining: float, avgFillPrice: float, **kwargs: Any) -> None:
        logger.debug("订单状态: orderId=%d status=%s filled=%.1f remaining=%.1f",
                      orderId, status, filled, remaining)

    def openOrderEnd(self) -> None:
        logger.info("挂单传输完成")
        self.order_end.set()

    # ── 执行回调 ──────────────────────────────────────────────────────

    def execDetails(self, reqId: int, contract: Contract, execution: Any) -> None:
        item = {
            "exec_id": getattr(execution, "execId", ""),
            "time": getattr(execution, "time", ""),
            "account": getattr(execution, "acctNumber", ""),
            "exchange": getattr(execution, "exchange", ""),
            "side": getattr(execution, "side", ""),
            "shares": getattr(execution, "shares", 0),
            "price": getattr(execution, "price", 0),
            "order_id": getattr(execution, "orderId", 0),
            "perm_id": getattr(execution, "permId", 0),
            "symbol": contract.symbol,
            "sec_type": contract.secType,
        }
        self.execution_queue.put(item)

    def execDetailsEnd(self, reqId: int) -> None:
        logger.info("执行详情传输完成 reqId=%d", reqId)
        self.execution_end.set()

    def commissionReport(self, commissionReport: Any) -> None:
        logger.debug("佣金报告: execId=%s commission=%.2f",
                      getattr(commissionReport, "execId", ""),
                      getattr(commissionReport, "commission", 0))

    # ── 完成订单回调 ──────────────────────────────────────────────────

    def completedOrder(self, contract: Contract, order: Any, orderState: Any) -> None:
        item = {
            "account": getattr(order, "account", self.current_account) or self.current_account,
            "order_id": getattr(order, "orderId", 0),
            "perm_id": getattr(order, "permId", 0),
            "symbol": contract.symbol,
            "sec_type": contract.secType,
            "action": getattr(order, "action", "") or "",
            "order_type": getattr(order, "orderType", "") or "",
            "total_qty": getattr(order, "totalQuantity", 0) or 0,
            "lmt_price": getattr(order, "lmtPrice", 0) or 0,
            "aux_price": getattr(order, "auxPrice", 0) or 0,
            "status": getattr(orderState, "status", "") or "",
            "filled": getattr(orderState, "filled", 0) or 0,
            "remaining": getattr(orderState, "remaining", 0) or 0,
            "avg_fill_price": getattr(orderState, "avgFillPrice", 0) or 0,
            "completed_time": getattr(orderState, "completedTime", "") or "",
            "completed_status": getattr(orderState, "completedStatus", "") or "",
        }
        self.completed_order_queue.put(item)

    def completedOrdersEnd(self) -> None:
        logger.info("完成订单传输完成")
        self.completed_order_end.set()

    # ── 行情回调 ──────────────────────────────────────────────────────

    def tickPrice(self, tickerId: TickerId, field: int, price: float,
                  attrib: Any) -> None:
        with self._ticker_lock:
            info = self._ticker_contracts.get(tickerId, {})
        if not info:
            return
        # 过滤无效价格（-1 或 None 表示数据不可用）
        if price is None or price < 0:
            return
        field_map = {1: "bid", 2: "ask", 4: "last", 6: "high", 7: "low", 9: "close", 14: "open"}
        fname = field_map.get(field)
        if fname and price > 0:
            info[fname] = price
            with self._ticker_lock:
                self._ticker_contracts[tickerId] = info

    def tickSize(self, tickerId: TickerId, field: int, size: int) -> None:
        with self._ticker_lock:
            info = self._ticker_contracts.get(tickerId, {})
        if not info:
            return
        if field == 8:  # volume
            info["volume"] = size
            with self._ticker_lock:
                self._ticker_contracts[tickerId] = info

    def tickOptionComputation(self, tickerId: TickerId, field: int,
                              tickAttrib: int, impliedVol: float,
                              delta: float, optPrice: float,
                              pvDividend: float, gamma: float,
                              vega: float, theta: float,
                              undPrice: float) -> None:
        with self._ticker_lock:
            info = self._ticker_contracts.get(tickerId, {})
        if not info:
            return
        if field in (10, 11, 12, 13):
            # 过滤 None 值，用 0 替代
            safe = lambda v: v if v is not None else 0.0
            info.update({
                "implied_vol": safe(impliedVol), "delta": safe(delta),
                "gamma": safe(gamma), "vega": safe(vega), "theta": safe(theta),
                "und_price": safe(undPrice), "opt_price": safe(optPrice),
            })
            with self._ticker_lock:
                self._ticker_contracts[tickerId] = info
            self.market_data_queue.put({
                "ticker_id": tickerId,
                "con_id": info.get("con_id", 0),
                "symbol": info.get("symbol", ""),
                "sec_type": info.get("sec_type", ""),
                "implied_vol": safe(impliedVol), "delta": safe(delta),
                "gamma": safe(gamma), "vega": safe(vega), "theta": safe(theta),
                "und_price": safe(undPrice),
            })

    def tickSnapshotEnd(self, tickerId: TickerId) -> None:
        logger.debug("行情快照完成 tickerId=%d", tickerId)

    # ── 历史行情回调 ──────────────────────────────────────────────────

    def historicalData(self, reqId: int, bar: Any) -> None:
        self.historical_data_queue.put({
            "req_id": reqId,
            "date": getattr(bar, "date", ""),
            "open": getattr(bar, "open", 0.0) or 0.0,
            "high": getattr(bar, "high", 0.0) or 0.0,
            "low": getattr(bar, "low", 0.0) or 0.0,
            "close": getattr(bar, "close", 0.0) or 0.0,
            "volume": getattr(bar, "volume", 0) or 0,
        })

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        self.historical_data_end_queue.put({
            "req_id": reqId,
            "start": start,
            "end": end,
        })

    # ── PnL 回调 ──────────────────────────────────────────────────────

    def pnl(self, reqId: int, dailyPnL: float,
            unrealizedPnL: float, realizedPnL: float) -> None:
        with self._pnl_lock:
            self._pnl_data[reqId] = {
                "daily": dailyPnL,
                "unrealized": unrealizedPnL,
                "realized": realizedPnL,
            }

    def pnlSingle(self, reqId: int, pos: int, dailyPnL: float,
                  unrealizedPnL: float, realizedPnL: float, value: float) -> None:
        with self._pnl_lock:
            self._pnl_single_data[reqId] = {
                "pos": pos, "daily": dailyPnL,
                "unrealized": unrealizedPnL,
                "realized": realizedPnL, "value": value,
            }

    # ── 辅助方法 ──────────────────────────────────────────────────────

    def allocate_ticker_id(self, contract_info: dict) -> int:
        with self._ticker_lock:
            self._ticker_counter += 1
            tid = self._ticker_counter
            self._ticker_contracts[tid] = contract_info
            return tid

    def get_ticker_info(self, ticker_id: int) -> dict:
        with self._ticker_lock:
            return dict(self._ticker_contracts.get(ticker_id, {}))

    def clear_ticker(self, ticker_id: int) -> None:
        with self._ticker_lock:
            self._ticker_contracts.pop(ticker_id, None)

    def get_pnl_data(self) -> dict:
        with self._pnl_lock:
            return dict(self._pnl_data)

    def get_pnl_single_data(self) -> dict:
        with self._pnl_lock:
            return dict(self._pnl_single_data)

    def drain_account_summary(self) -> list[dict]:
        items = []
        while not self.account_summary_queue.empty():
            try:
                items.append(self.account_summary_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_positions(self) -> list[dict]:
        items = []
        while not self.position_queue.empty():
            try:
                items.append(self.position_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_orders(self) -> list[dict]:
        items = []
        while not self.order_queue.empty():
            try:
                items.append(self.order_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_executions(self) -> list[dict]:
        items = []
        while not self.execution_queue.empty():
            try:
                items.append(self.execution_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_completed_orders(self) -> list[dict]:
        items = []
        while not self.completed_order_queue.empty():
            try:
                items.append(self.completed_order_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_market_data(self) -> list[dict]:
        items = []
        while not self.market_data_queue.empty():
            try:
                items.append(self.market_data_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_historical_data(self) -> list[dict]:
        items = []
        while not self.historical_data_queue.empty():
            try:
                items.append(self.historical_data_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_historical_data_end(self) -> list[dict]:
        items = []
        while not self.historical_data_end_queue.empty():
            try:
                items.append(self.historical_data_end_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def drain_errors(self) -> list[dict]:
        items = []
        while not self.error_queue.empty():
            try:
                items.append(self.error_queue.get_nowait())
            except queue.Empty:
                break
        return items
