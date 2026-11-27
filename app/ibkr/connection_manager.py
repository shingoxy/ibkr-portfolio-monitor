"""IBKR 连接管理器：指数退避重连、数据请求编排、内存缓存。"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime
from typing import Optional

from app.ibkr.client import IBKRClient, ACCOUNT_TAGS
from app.ibkr.errors import requires_resubscribe
from app.ibkr.request_ids import RequestIdManager

logger = logging.getLogger(__name__)


def _patch_socket_for_proxy(proxy_host: str, proxy_port: int, proxy_type: str = "socks5") -> None:
    """Monkey-patch ibapi 的 socket 创建，使其通过代理连接。

    注意：此函数会修改全局 socket.socket，调用方必须在连接完成后调用 _restore_socket()。
    """
    import socks

    proxy_map = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
    }
    stype = proxy_map.get(proxy_type.lower(), socks.SOCKS5)

    _orig_socket = socket.socket

    def _proxy_socket(*args, **kwargs):
        s = socks.socksocket(*args, **kwargs)
        s.setproxy(stype, proxy_host, proxy_port)
        return s

    socket.socket = _proxy_socket
    # 返回原始 socket 以便恢复
    return _orig_socket


class DataCache:
    """线程安全的内存数据缓存。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.account_summary: list[dict] = []
        self.positions: list[dict] = []
        self.orders: list[dict] = []
        self.executions: list[dict] = []
        self.completed_orders: list[dict] = []
        self.market_data: dict[int, dict] = {}  # con_id -> greeks/quotes
        self.portfolio_values: dict[int, dict] = {}  # con_id -> updatePortfolio
        self.pnl: dict = {}  # daily, unrealized, realized
        self.pnl_single: dict[int, dict] = {}  # con_id -> pnl data
        self.last_update: Optional[datetime] = None
        self.connected: bool = False
        self.connection_mode: str = "Unknown"  # Paper / Live / Unknown
        self.accounts: list[str] = []
        self.error_message: str = ""
        self.data_freshness: str = "stale"  # fresh / stale / partial

    def update_account_summary(self, items: list[dict]) -> None:
        with self._lock:
            merged: dict[tuple[str, str, str], dict] = {
                (
                    str(existing.get("account") or ""),
                    str(existing.get("tag") or ""),
                    str(existing.get("currency") or ""),
                ): dict(existing)
                for existing in self.account_summary
            }
            for item in items:
                key = (
                    str(item.get("account") or ""),
                    str(item.get("tag") or ""),
                    str(item.get("currency") or ""),
                )
                merged[key] = dict(item)
            self.account_summary = list(merged.values())
            self.last_update = datetime.utcnow()
            self.data_freshness = "fresh"

    def update_positions(self, items: list[dict]) -> None:
        def _pos_key(p: dict) -> tuple:
            con_id = int(p.get("con_id") or 0)
            acct = str(p.get("account") or "")
            if con_id > 0:
                return ("con", acct, con_id)
            return (
                "sym",
                acct,
                str(p.get("symbol") or ""),
                str(p.get("sec_type") or ""),
                str(p.get("expiry") or ""),
                float(p.get("strike") or 0.0),
                str(p.get("right") or ""),
            )

        with self._lock:
            merged: dict[tuple, dict] = {
                _pos_key(existing): dict(existing)
                for existing in self.positions
                if abs(float(existing.get("position") or 0)) > 1e-9
            }
            for item in items:
                key = _pos_key(item)
                qty = float(item.get("position") or 0)
                if abs(qty) <= 1e-9:
                    merged.pop(key, None)
                else:
                    merged[key] = dict(item)
            self.positions = list(merged.values())
            self.last_update = datetime.utcnow()

    def update_orders(self, items: list[dict]) -> None:
        with self._lock:
            self.orders = items
            self.last_update = datetime.utcnow()

    def update_executions(self, items: list[dict]) -> None:
        with self._lock:
            self.executions = items
            self.last_update = datetime.utcnow()

    def update_completed_orders(self, items: list[dict]) -> None:
        with self._lock:
            self.completed_orders = items
            self.last_update = datetime.utcnow()

    def update_market_data(self, items: list[dict]) -> None:
        with self._lock:
            for item in items:
                con_id = item.get("con_id", 0)
                if con_id:
                    existing = self.market_data.get(con_id, {})
                    existing.update(item)
                    self.market_data[con_id] = existing
            self.last_update = datetime.utcnow()

    def update_pnl(self, data: dict) -> None:
        with self._lock:
            self.pnl = data
            self.last_update = datetime.utcnow()

    def update_pnl_single(self, data: dict) -> None:
        with self._lock:
            self.pnl_single = data
            self.last_update = datetime.utcnow()

    def update_portfolio_values(self, values: dict[int, dict]) -> None:
        with self._lock:
            self.portfolio_values = {k: dict(v) for k, v in values.items()}
            self.last_update = datetime.utcnow()

    def mark_stale(self) -> None:
        with self._lock:
            self.data_freshness = "stale"

    @staticmethod
    def _pick_market_price(pos: dict, md: dict) -> float:
        for field in ("last", "close"):
            val = md.get(field)
            if val is not None and float(val) > 0:
                return float(val)
        bid = float(md.get("bid") or 0)
        ask = float(md.get("ask") or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        if pos.get("sec_type", "") in ("STK", "ETF"):
            und = float(md.get("und_price") or 0)
            if und > 0:
                return und
        return 0.0

    def get_snapshot(self) -> dict:
        with self._lock:
            positions = [dict(p) for p in self.positions]
            market_data = {k: dict(v) for k, v in self.market_data.items()}
            portfolio_values = {k: dict(v) for k, v in self.portfolio_values.items()}

            for p in positions:
                con_id = p.get("con_id", 0)
                if con_id in portfolio_values:
                    pv = portfolio_values[con_id]
                    if (pv.get("market_price") or 0) > 0:
                        p["market_price"] = float(pv["market_price"])
                    p["market_value"] = float(pv.get("market_value", p.get("market_value", 0)) or 0)
                    p["unrealized_pnl"] = float(pv.get("unrealized_pnl", p.get("unrealized_pnl", 0)) or 0)
                    p["realized_pnl"] = float(pv.get("realized_pnl", p.get("realized_pnl", 0)) or 0)
                    p["data_status"] = p.get("data_status") or "portfolio"

                if (p.get("market_price") or 0) <= 0 and con_id in market_data:
                    md = market_data[con_id]
                    price = self._pick_market_price(p, md)
                    if price > 0:
                        qty = float(p.get("position", 0) or 0)
                        mult = float(p.get("multiplier", 100) or 100) if p.get("sec_type") == "OPT" else 1.0
                        avg_cost = float(p.get("avg_cost", 0) or 0)
                        p["market_price"] = price
                        p["market_value"] = qty * price * mult
                        if avg_cost > 0:
                            p["unrealized_pnl"] = (price - avg_cost) * qty * mult
                        p["data_status"] = "delayed" if md.get("is_delayed") else "realtime"

            return {
                "account_summary": [dict(a) for a in self.account_summary],
                "positions": positions,
                "orders": [dict(o) for o in self.orders],
                "executions": [dict(e) for e in self.executions],
                "completed_orders": [dict(c) for c in self.completed_orders],
                "market_data": market_data,
                "portfolio_values": portfolio_values,
                "pnl": dict(self.pnl),
                "pnl_single": {k: dict(v) if isinstance(v, dict) else v for k, v in self.pnl_single.items()},
                "last_update": self.last_update,
                "connected": self.connected,
                "connection_mode": self.connection_mode,
                "accounts": list(self.accounts),
                "error_message": self.error_message,
                "data_freshness": self.data_freshness,
            }


class ConnectionManager:
    """管理 IBKR 连接生命周期，支持指数退避重连。"""

    def __init__(self, settings: dict) -> None:
        self._host = settings.get("host", "127.0.0.1")
        self._port = settings.get("port", 7497)
        self._client_id = settings.get("client_id", 17)
        self._account = settings.get("account", "")
        self._max_retries = settings.get("max_retries", 20)
        self._base_delay = settings.get("base_delay", 1.0)
        self._max_delay = settings.get("max_delay", 60.0)

        # 代理设置
        self._proxy_host = settings.get("proxy_host", "")
        self._proxy_port = settings.get("proxy_port", 0)
        self._proxy_type = settings.get("proxy_type", "socks5")
        self._proxy_patched = False

        self._client: Optional[IBKRClient] = None
        self._api_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._collector_thread: Optional[threading.Thread] = None
        self._reconnect_thread: Optional[threading.Thread] = None
        self._running = False
        self._retry_count = 0

        self.cache = DataCache()
        self.req_ids = RequestIdManager()
        self._option_tickers: dict[int, int] = {}  # ticker_id -> con_id

    @property
    def connected(self) -> bool:
        return self.cache.connected

    def start(self) -> None:
        """启动连接（非阻塞，在后台线程中执行）。"""
        logger.info("正在连接 IBKR TWS API: %s:%d (ClientId=%d)",
                     self._host, self._port, self._client_id)
        self._running = True
        self._connect_thread = threading.Thread(
            target=self._connect_with_retry, daemon=True, name="ibkr-connect"
        )
        self._connect_thread.start()

    def stop(self) -> None:
        """断开连接。"""
        self._running = False
        if self._client:
            logger.info("正在断开 IBKR 连接...")
            if getattr(self._client, "current_account", ""):
                try:
                    self._client.reqAccountUpdates(False, self._client.current_account)
                except Exception:
                    pass
            self._client.disconnect()
        self.cache.connected = False
        logger.info("IBKR 连接已断开")

    def _connect_with_retry(self) -> None:
        """使用指数退避进行连接。"""
        # 代理设置（仅需配置一次）
        if self._proxy_host and self._proxy_port and not self._proxy_patched:
            try:
                import socks  # noqa: F401 - 验证 PySocks 已安装
                self._proxy_patched = True
                logger.info("代理已配置: %s://%s:%d", self._proxy_type, self._proxy_host, self._proxy_port)
            except ImportError:
                logger.error("需要 PySocks 库才能使用代理: pip install PySocks")
                return

        while self._running and self._retry_count < self._max_retries:
            # 仅在连接阶段临时应用代理
            orig_socket = None
            if self._proxy_patched:
                orig_socket = _patch_socket_for_proxy(
                    self._proxy_host, self._proxy_port, self._proxy_type
                )

            try:
                self._client = IBKRClient()
                self._client.connect(self._host, self._port, self._client_id)

                # 恢复原始 socket（代理仅用于连接阶段）
                if orig_socket:
                    socket.socket = orig_socket

                # 启动 API 消息循环
                self._api_thread = threading.Thread(
                    target=self._run_api_loop, daemon=True, name="ibkr-api"
                )
                self._api_thread.start()

                # 等待连接就绪
                connected = self._client.connected_event.wait(timeout=30.0)
                if connected:
                    self._on_connected()
                    return

                logger.warning("连接超时（第 %d 次）", self._retry_count + 1)
                self._client.disconnect()

            except Exception as e:
                logger.error("连接异常（第 %d 次）: %s", self._retry_count + 1, e)
                # 确保恢复原始 socket
                if orig_socket:
                    socket.socket = orig_socket

            # 指数退避
            delay = min(self._base_delay * (2 ** self._retry_count), self._max_delay)
            logger.info("%.1f 秒后重试...", delay)
            time.sleep(delay)
            self._retry_count += 1

        if self._retry_count >= self._max_retries:
            msg = f"连接失败，已重试 {self._max_retries} 次"
            logger.error(msg)
            self.cache.error_message = msg

    def _on_connected(self) -> None:
        """连接成功后的初始化。"""
        self._retry_count = 0
        self.cache.connected = True
        self.cache.accounts = self._client.accounts
        self.cache.error_message = ""

        # 判断 Paper/Live（基于端口）
        if self._port in (7497, 4002):
            self.cache.connection_mode = "Paper"
        elif self._port in (7496, 4001):
            self.cache.connection_mode = "Live"
        else:
            self.cache.connection_mode = "Unknown"

        # 设置账户
        if self._account:
            self._client.current_account = self._account
        elif self._client.accounts:
            self._client.current_account = self._client.accounts[0]

        logger.info("连接成功 [%s]，账户: %s",
                     self.cache.connection_mode, self._client.current_account)

        # 发起初始数据请求
        self._request_initial_data()

        # 启动轮询线程
        if not self._poll_thread or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="ibkr-poll"
            )
            self._poll_thread.start()

        # 启动数据收集线程
        if not self._collector_thread or not self._collector_thread.is_alive():
            self._collector_thread = threading.Thread(
                target=self._collector_loop, daemon=True, name="ibkr-collector"
            )
            self._collector_thread.start()

        # 启动错误监控线程
        self._error_monitor_thread = threading.Thread(
            target=self._error_monitor_loop, daemon=True, name="ibkr-errors"
        )
        self._error_monitor_thread.start()

    def _run_api_loop(self) -> None:
        try:
            self._client.run()
        except Exception as e:
            logger.error("API 线程异常: %s", e)
            self.cache.connected = False
            self.cache.error_message = str(e)
            if self._running:
                self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """调度重连。"""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="ibkr-reconnect"
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """重连循环。"""
        self.cache.mark_stale()
        self._retry_count = 0
        logger.info("开始重连...")
        self._connect_with_retry()

    def _error_monitor_loop(self) -> None:
        """监控错误队列，处理连接状态变化。"""
        while self._running:
            try:
                errors = []
                while not self._client.error_queue.empty():
                    try:
                        errors.append(self._client.error_queue.get_nowait())
                    except Exception:
                        break

                for err in errors:
                    code = err.get("errorCode", 0)
                    if requires_resubscribe(code):
                        logger.info("需要重新订阅数据 (code=%d)", code)
                        self._resubscribe_all()
                    elif err.get("type") == "connection":
                        self._schedule_reconnect()

                time.sleep(1)
            except Exception as e:
                logger.error("错误监控线程异常: %s", e)

    def _request_initial_data(self) -> None:
        """发起初始数据请求。"""
        if not self._client:
            return

        # 设置行情类型：4=冻结延迟（同时兼容实盘订阅与无实时权限账户的盘中/盘后估值）
        # 1=实时, 2=冻结, 3=延迟, 4=冻结延迟
        self._client.reqMarketDataType(4)
        logger.info("已设置行情类型: 冻结延迟行情 (type=4)")

        self._client.account_summary_end.clear()
        self._client.reqAccountSummary(
            RequestIdManager.RESERVED_ACCOUNT_SUMMARY, "All", ACCOUNT_TAGS
        )
        logger.info("已请求账户摘要")

        self._client.position_end.clear()
        self._client.reqPositions()
        logger.info("已请求持仓数据")

        self._client.order_end.clear()
        self._client.reqAllOpenOrders()
        logger.info("已请求挂单数据")

        # 请求 PnL 与只读持仓估值
        acct = self._client.current_account
        if acct:
            self._client.reqAccountUpdates(True, acct)
            logger.info("已订阅账户只读持仓估值 (reqAccountUpdates)")
            self._client.reqPnL(
                RequestIdManager.RESERVED_PNL, acct, ""
            )
            logger.info("已请求账户 PnL")

        # 请求执行历史
        from ibapi.execution import ExecutionFilter
        self._client.execution_end.clear()
        self._client.reqExecutions(
            RequestIdManager.RESERVED_EXECUTIONS, ExecutionFilter()
        )
        logger.info("已请求执行历史")

        # 请求完成订单
        self._client.completed_order_end.clear()
        self._client.reqCompletedOrders(False)  # False = 所有订单
        logger.info("已请求完成订单")

    def _resubscribe_all(self) -> None:
        """重新订阅所有数据。"""
        if not self._client or not self.cache.connected:
            return
        logger.info("重新订阅所有数据...")
        self._request_initial_data()
        # 重新订阅期权行情
        for ticker_id, con_id in list(self._option_tickers.items()):
            info = self._client.get_ticker_info(ticker_id)
            if info:
                logger.debug("重新订阅行情 tickerId=%d conId=%d", ticker_id, con_id)

    def _poll_loop(self) -> None:
        """轮询线程：定期请求非订阅型数据。"""
        while self._running:
            try:
                time.sleep(10)
                if not self._client or not self.cache.connected:
                    continue
                self._client.order_end.clear()
                self._client.reqAllOpenOrders()
            except Exception as e:
                logger.error("轮询线程异常: %s", e)

    def _collector_loop(self) -> None:
        """数据收集线程。"""
        while self._running:
            try:
                time.sleep(2)
                if not self._client:
                    continue

                # 收集账户摘要
                acct_items = self._client.drain_account_summary()
                if acct_items:
                    self.cache.update_account_summary(acct_items)

                # 收集持仓
                pos_items = self._client.drain_positions()
                if pos_items:
                    self.cache.update_positions(pos_items)
                    self._subscribe_option_market_data(pos_items)

                # 收集订单
                order_items = self._client.drain_orders()
                if order_items:
                    self.cache.update_orders(order_items)

                # 收集执行
                exec_items = self._client.drain_executions()
                if exec_items:
                    self.cache.update_executions(exec_items)

                # 收集完成订单
                completed_items = self._client.drain_completed_orders()
                if completed_items:
                    self.cache.update_completed_orders(completed_items)

                # 收集行情
                mkt_items = self._client.drain_market_data()
                if mkt_items:
                    self.cache.update_market_data(mkt_items)

                # 收集 updatePortfolio 持仓估值
                portfolio_values = self._client.get_portfolio_values()
                if portfolio_values:
                    self.cache.update_portfolio_values(portfolio_values)

                # 收集 PnL
                pnl_data = self._client.get_pnl_data()
                if pnl_data:
                    # 取第一个 PnL 订阅的数据
                    for req_id, data in pnl_data.items():
                        self.cache.update_pnl(data)
                        break
                pnl_single = self._client.get_pnl_single_data()
                if pnl_single:
                    self.cache.update_pnl_single(pnl_single)

            except Exception as e:
                logger.error("数据收集线程异常: %s", e)

    def _subscribe_option_market_data(self, positions: list[dict]) -> None:
        """为持仓订阅行情（期权 + 股票/ETF）。"""
        if not self._client:
            return

        from ibapi.contract import Contract

        # 记录已订阅的标的（含币种），避免重复
        subscribed_underlyings: set[tuple[str, str]] = set()

        for pos in positions:
            con_id = pos.get("con_id", 0)
            already = any(cid == con_id for cid in self._option_tickers.values())
            if already:
                continue

            active = len(self._client._ticker_contracts)
            if active >= 95:
                logger.warning("行情线接近上限 (%d/100)，跳过", active)
                break

            sec_type = pos.get("sec_type", "")

            # 订阅期权行情
            if sec_type == "OPT":
                contract = Contract()
                contract.conId = con_id
                contract.symbol = pos["symbol"]
                contract.secType = "OPT"
                contract.exchange = "SMART"
                contract.currency = pos.get("currency", "USD")
                contract.strike = pos.get("strike", 0)
                contract.right = pos.get("right", "")
                contract.lastTradeDateOrContractMonth = pos.get("expiry", "")
                mult = pos.get("multiplier", 100)
                contract.multiplier = str(int(mult)) if mult else "100"
                if pos.get("trading_class"):
                    contract.tradingClass = pos["trading_class"]

                contract_info = {
                    "con_id": con_id, "symbol": pos["symbol"],
                    "sec_type": "OPT", "strike": pos.get("strike", 0),
                    "right": pos.get("right", ""), "expiry": pos.get("expiry", ""),
                }
                ticker_id = self._client.allocate_ticker_id(contract_info)
                self._option_tickers[ticker_id] = con_id

                self._client.reqMktData(ticker_id, contract, "", False, False, [])
                logger.info("订阅期权行情: %s %s %s conId=%d",
                             pos["symbol"], pos.get("right"), pos.get("expiry"), con_id)

            # 订阅股票/ETF 行情（获取标的价格）
            elif sec_type in ("STK", "ETF"):
                symbol = pos.get("symbol", "")
                currency = (pos.get("currency") or "USD").strip().upper() or "USD"
                sub_key = (symbol, currency)
                if sub_key in subscribed_underlyings:
                    continue
                subscribed_underlyings.add(sub_key)

                pos_exchange = (pos.get("exchange") or "").strip().upper()
                contract = Contract()
                contract.conId = con_id
                contract.symbol = symbol
                contract.secType = sec_type
                contract.exchange = pos_exchange if (currency != "USD" and pos_exchange) else "SMART"
                contract.currency = currency
                if pos.get("primary_exchange"):
                    contract.primaryExchange = pos["primary_exchange"]

                contract_info = {
                    "con_id": con_id, "symbol": symbol,
                    "sec_type": sec_type, "currency": currency,
                }
                ticker_id = self._client.allocate_ticker_id(contract_info)
                self._option_tickers[ticker_id] = con_id

                self._client.reqMktData(ticker_id, contract, "", False, False, [])
                logger.info("订阅股票行情: %s (%s/%s) conId=%d", symbol, contract.exchange, currency, con_id)

    def get_connection_status(self) -> dict:
        """获取连接状态。"""
        return {
            "connected": self.cache.connected,
            "connection_mode": self.cache.connection_mode,
            "accounts": self.cache.accounts,
            "last_update": self.cache.last_update.isoformat() if self.cache.last_update else None,
            "error_message": self.cache.error_message,
            "data_freshness": self.cache.data_freshness,
        }

    def refresh_all(self) -> None:
        """手动触发全量刷新。"""
        if not self._client or not self.cache.connected:
            return

        self._client.cancelAccountSummary(RequestIdManager.RESERVED_ACCOUNT_SUMMARY)
        time.sleep(0.1)
        self._client.account_summary_end.clear()
        self._client.reqAccountSummary(
            RequestIdManager.RESERVED_ACCOUNT_SUMMARY, "All", ACCOUNT_TAGS
        )

        self._client.cancelPositions()
        time.sleep(0.1)
        self._client.position_end.clear()
        self._client.reqPositions()

        self._client.order_end.clear()
        self._client.reqAllOpenOrders()

        logger.info("手动刷新已触发")
