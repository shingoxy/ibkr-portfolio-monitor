# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

IBKR 本地只读持仓分析工具 — 通过 Interactive Brokers TWS API 读取美股账户数据，在本地网页展示持仓、盈亏、期权组合识别和风险分析。

**严格只读**：不允许任何下单/取消/行权操作，配置层 + 运行时 + AST 扫描三重保障。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动应用（需先启动 TWS 并启用 API）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 检查 IBKR 连接
python scripts/check_connection.py --port 7497

# 运行全部测试（46 个）
python -m pytest tests/ -v

# 运行单个测试文件
python -m pytest tests/test_strategy_detector.py -v

# 运行匹配关键词的测试
python -m pytest tests/ -v -k "read_only"
```

## 架构核心

### 数据流

```
TWS API (ibapi) → IBKRClient (EWrapper 回调) → 线程安全队列
    → ConnectionManager._collector_loop() (每 2 秒 drain 队列)
    → DataCache (线程安全内存缓存)
    → FastAPI routes → Jinja2 模板渲染 / JSON API
    → SnapshotService → SnapshotRepository → SQLite (异步, aiosqlite)
```

关键点：
- **IBKRClient** 运行在独立的 API 消息循环线程中 (`client.run()` 阻塞)
- TWS 推送数据通过回调进入 `queue.Queue`，由 `_collector_loop` 定期 drain 到 `DataCache`
- `DataCache` 是所有 API 路由的唯一数据源 — 路由不直接访问 IBKRClient
- API 路由读取 DataCache 时通过 `get_snapshot()` 获取深拷贝，避免并发问题

### 连接生命周期

1. `ConnectionManager.start()` → 后台线程执行 `_connect_with_retry()`（指数退避，最多 20 次）
2. 连接成功后 `_on_connected()` → 发起初始数据请求 → 启动 poll/collector/error-monitor 线程
3. 断线时 `error()` 回调检测到 1100 → 触发 `_schedule_reconnect()`
4. `ConnectionManager.stop()` 清理所有线程并断开连接

### 模块职责

| 模块 | 职责 |
|------|------|
| `app/config.py` | Pydantic Settings，从 `.env` 加载。强制 `app_host` 仅允许 localhost，`read_only` 必须为 true |
| `app/logging_config.py` | 日志脱敏过滤器（自动遮盖 U12345678 → U12****78）、禁止交易方法的运行时检查 |
| `app/ibkr/client.py` | EWrapper + EClient 实现。仅实现只读回调（accountSummary, position, openOrder, execDetails, pnl, tickPrice/OptionComputation 等）。构造时自动校验类中无禁止方法 |
| `app/ibkr/connection_manager.py` | 连接生命周期管理、指数退避重连、数据收集轮询。`DataCache` 是线程安全的内存缓存 |
| `app/ibkr/errors.py` | 错误码到中文消息的映射、连接性错误判断 |
| `app/ibkr/request_ids.py` | reqId 管理，预留 ID 常量 |
| `app/ibkr/contracts.py` | 合约构建辅助 |
| `app/models/schemas.py` | Pydantic v2 数据模型（AccountSnapshot, PositionItem, OptionCombo, PayoffCurve 等） |
| `app/services/strategy_detector.py` | 14 种期权策略自动识别。按标的+到期日分组，按优先级匹配（Iron Condor → Iron Butterfly → Butterfly → 两腿 → 跨期 → 含股票 → 单腿/自定义） |
| `app/services/payoff_engine.py` | 到期收益曲线计算。正确处理多头/空头、乘数、手续费 |
| `app/services/option_pricing.py` | Black-Scholes 定价 + Greeks 计算 |
| `app/services/risk_service.py` | 组合风险评估（保证金风险、到期风险、Gamma 风险、波动率风险等） |
| `app/repositories/database.py` | SQLAlchemy ORM 模型定义 |
| `app/repositories/snapshot_repository.py` | 异步快照持久化 + 自动清理 |
| `app/api/` | 5 个路由模块：dashboard, positions, strategies, orders, export |
| `app/templates/` | Jinja2 模板（base.html + 4 页面），Bootstrap 5 |
| `app/static/` | CSS + JS（Plotly 图表、数据刷新逻辑） |

### 重要约定

- **账户号脱敏**：`U12345678` → `U12****78`，通过 `AccountMaskFilter` 在日志层自动处理
- **配置单例**：`get_settings()` 使用 `@lru_cache` 确保全局唯一实例
- **`app_host` 强制绑定 localhost**：配置校验会拒绝任何非 `127.0.0.1` / `localhost` 的值
- **`read_only=false` 时程序拒绝启动**（`sys.exit(1)`）
- **测试中的只读守卫**：`test_read_only_guards.py` 使用 AST 扫描确保代码中不调用 `placeOrder` 等方法

### 期权成本注意

IBKR 的 `avgCost` 是**每股/每单位成本**，不包含 multiplier。期权总成本 = `avgCost × position × multiplier`。`payoff_engine.py` 和 `strategy_detector.py` 都已正确处理此逻辑。

## 测试

46 个测试覆盖：
- 只读保护（AST 静态扫描 + 运行时检查）
- 账户脱敏（确保完整账户号不出现在日志/导出中）
- 组合识别（14 种策略 + 边界情况：数量不匹配、到期日不匹配、乘数不匹配）
- 收益计算（multiplier + 数量 + 手续费）
- 期权成本（per-share vs per-contract）

测试使用纯 Python，不需要数据库或 IBKR 连接。
