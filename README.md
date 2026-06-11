# IBKR 本地只读持仓分析工具

通过 Interactive Brokers TWS API 读取美股账户数据，在本地网页中展示持仓、盈亏、期权组合和风险分析。

## 安全声明

- **严格只读**：代码中不包含 `placeOrder`、`cancelOrder`、`reqGlobalCancel`、`exerciseOptions` 调用
- **默认 READ_ONLY=true**：配置为 false 时程序拒绝启动
- **仅监听 127.0.0.1**：不允许暴露到公网
- **不保存密码**：登录由 TWS/IB Gateway 手动完成
- **账户号脱敏**：日志和导出数据中自动脱敏（U12345678 → U12****78）

## 快速开始

### 1. 安装依赖

```powershell
cd D:\Projects\TradingWorkstation
pip install -r requirements.txt
```

### 2. 配置 .env

复制 `.env.example` 为 `.env`，按需修改：

```env
IBKR_HOST=127.0.0.1
IBKR_PORT=7497          # TWS Paper=7497, Live=7496, Gateway Paper=4002, Live=4001
IBKR_CLIENT_ID=17
APP_HOST=127.0.0.1
APP_PORT=8765
READ_ONLY=true
```

### 3. 配置 TWS

在 Trader Workstation 中：

1. 进入 **File → Global Configuration → API → Settings**
2. 勾选 **Enable ActiveX and Socket Clients**
3. 勾选 **Read-Only API**（即使程序没有下单代码，也应开启）
4. 设置 **Socket port** 与 `.env` 中的 `IBKR_PORT` 一致
5. **Trusted IPs** 添加 `127.0.0.1`
6. 可选：勾选 **Download open orders on connection**

### 4. 检查连接

```powershell
python scripts/check_connection.py --port 7497
```

### 5. 启动应用

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

或使用脚本：

```powershell
powershell -File scripts/run.ps1
```

访问 http://127.0.0.1:8765

## 功能

| 页面 | 功能 |
|------|------|
| 仪表盘 | 账户概要、净资产、保证金、持仓分布饼图、数据过期提示 |
| 持仓 | 股票/期权持仓表格，按标的/类型筛选，按盈亏/到期日等排序 |
| 期权策略 | 自动识别组合（14种策略），收益图、情景分析（IV变化维度）、风险提示 |
| 挂单 | 只读显示当前挂单、已完成订单、成交记录 |
| 导出 | 脱敏 Markdown/JSON + 完整本地 JSON |

## 自动识别的期权组合

- 单腿期权（Long/Short Call/Put）
- 垂直价差（Bull Call/Bear Put/Bull Put/Bear Call Spread）
- Straddle / Strangle
- Iron Condor / Iron Butterfly
- Butterfly Spread
- Calendar / Diagonal Spread
- Covered Call / Protective Put
- 自定义多腿组合

## 测试

```powershell
python -m pytest tests/ -v
```

46 个测试覆盖：
- 只读保护（AST 静态扫描禁止方法调用）
- 账户脱敏（完整账户号不会出现在日志/导出中）
- 组合识别（14种策略 + 边界情况）
- 收益计算（各种 multiplier + 数量 + 手续费）
- 期权成本（per-share vs per-contract 验证）
- 边界情况（数量不匹配、到期日不匹配、乘数不匹配）

## 项目结构

```
app/
├── main.py                     # FastAPI 入口 + 后台快照任务
├── config.py                   # Pydantic Settings
├── logging_config.py           # 日志 + 账户脱敏 + 只读守卫
├── ibkr/
│   ├── client.py               # EWrapper + EClient（修正 ACCOUNT_TAGS）
│   ├── connection_manager.py   # 指数退避重连 + DataCache
│   ├── errors.py               # 错误码中文翻译
│   ├── request_ids.py          # reqId 管理
│   └── contracts.py            # 合约构建
├── models/schemas.py           # Pydantic v2 数据模型
├── services/
│   ├── strategy_detector.py    # 组合识别（14种策略 + 风险评估）
│   ├── payoff_engine.py        # 到期收益计算
│   ├── option_pricing.py       # Black-Scholes + Greeks + IV
│   ├── risk_service.py         # 风险评估
│   ├── export_service.py       # 脱敏导出 + 完整本地导出
│   └── snapshot_service.py     # 快照编排 + 自动清理
├── repositories/               # SQLite 异步存储
├── api/                        # 5 个路由模块（含筛选排序）
├── templates/                  # 4 个 HTML 页面
└── static/                     # CSS + JS
tests/                          # 5 个测试文件，46 个测试
scripts/                        # run.ps1, install.ps1, check_connection.py
```

## 技术栈

- Python 3.14 / FastAPI / Uvicorn / Pydantic v2
- ibapi（官方 TWS API）
- numpy / scipy / plotly
- Jinja2 / Bootstrap 5
- SQLite / SQLAlchemy / aiosqlite
- pytest

## 启动命令

```powershell
# 确认 TWS 已启动且 API 已启用
python scripts/check_connection.py --port 7497

# 启动应用
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```
