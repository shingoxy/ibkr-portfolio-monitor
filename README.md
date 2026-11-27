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

## 只读策略回测

TWS 登录并启用 API 后，可同步分红拆股调整后的日线并运行三年回测：

```bash
conda run -n mlx312 python scripts/run_backtest.py --years 3
```

历史数据场临时不可用时，可以使用最近一次成功同步的本地 CSV：

```bash
conda run -n mlx312 python scripts/run_backtest.py --years 3 --use-cache
```

默认观察池为 `SPY,QQQ,VTI,IWM,DIA,NVDA,TSM,TSLA,AAPL,MSFT`。行情 CSV 和回测报告保存在本地 `data/history/` 与 `data/backtests/`，不会发出任何交易指令。

研究策略采用 50/150 日均线趋势过滤、10% 移动止损、下一交易日开盘成交，并计入手续费与滑点。风控参数为单笔风险 0.75%、总敞口 75%、单标的 25%、至少保留 15% 现金、最多 4 个持仓。

指数核心方案对照研究（默认 10 年目标窗口、每月 $500 定投对照、5% 漂移阈值再平衡、2% 趋势双缓冲与 `BIL`/`SHY` 避险轮动）：

```bash
# 离线使用本地 data/history/ 缓存运行（默认 --years 10）
conda run -n mlx312 python scripts/run_index_study.py

# TWS 启动并启用 API 后，同步 10 年（years*365+400 日）ADJUSTED_LAST 数据（含 SPY, QQQ, BIL, SHY 及观察池）
conda run -n mlx312 python scripts/run_index_study.py --years 10 --sync --host 127.0.0.1 --port 7497 --client-id 78
```

主要参数：
- `--years`（默认 `10`）、`--initial-cash`（默认 `11000.0`）、`--monthly-contribution`（默认 `500.0`）
- `--defensive-symbol`（默认 `BIL`）：200 日趋势转弱时将退出权重轮换至避险 ETF；若本地未缓存 `BIL` 则自动回退 `SHY`，两者均未缓存时仅运行纯股票/现金方案并在报告中提示先执行 `--years 10 --sync`
- `--drift-threshold`（默认 `0.05`）、`--trend-buffer`（默认 `0.02` 滞回带宽，降低均线假突破换手）
- `--sync`（`--host`、`--port` 默认 `7497`、`--client-id` 默认 `78`）：严格只读调用 `IBKRHistoricalBarProvider` 拉取日线至 `data/history/`

报告写入 `data/backtests/index-study-latest.{json,md}`（及时间戳归档），包含一次性投入（Lump-Sum）与每月定投（Monthly DCA，计算现金流调整后 TWR、MWR/XIRR 与 TWR 最大回撤）对照表、分年度收益分解（Annual Breakdown）、历史压力窗口（`2018 Q4` / `2020 COVID` / `2022 Bear` / `2025 Tariff`）、Rolling 1Y/3Y 胜率汇总及动态结论。

## No.2 美股只读研究与前置诊断（Phase 0 & Track 1-2）

> **只读守卫声明**：以下脚本严格维持 `READ_ONLY=true` 与本地 AST/运行时只读校验，绝不调用任何下单或撤单接口；Track 3（5 分钟 RVOL-ORB）受 Phase 0 实时行情与分钟线核查门控，Phase 3 自动执行不在本仓库范围内。

### Phase 0：环境与 TWS 权限只读诊断 (`scripts/check_no2_prerequisites.py`)

```bash
# 离线检查 .env 的 READ_ONLY=true / localhost / 端口守卫（不连接 TWS）
conda run -n mlx312 python scripts/check_no2_prerequisites.py --env-only --env-file .env

# 连接本地 TWS 只读核查账户类型（Cash T+1 / PDT）、market_data_type=1 实时行情及 SPY 5m 历史柱线
conda run -n mlx312 python scripts/check_no2_prerequisites.py --host 127.0.0.1 --port 7497 --client-id 79 --symbols SPY,AAPL --timeout 15.0
```

实时诊断成功后会写入脱敏报告 `data/backtests/no2-phase0-latest.{json,md}`（可通过 `--output-dir` 指定目录），并提示在 IBKR Client Portal 人工核查美股 Voluntary Corporate Action 手续费与 Tiered/Fixed 佣金结构。

### Track 1：PEAD 财报超预期漂移离线回测 (`scripts/fetch_pead_events.py`, `scripts/run_pead_study.py`)

```bash
# 从 SEC EDGAR 拉取 Point-in-Time 8-K Item 2.02 (BMO/AMC) 与 XBRL 同比 EPS/营收事件 CSV（可选 --sync-trades 拉取未复权 TRADES 日线）
conda run -n mlx312 python scripts/fetch_pead_events.py --symbols AAPL,MSFT,NVDA,TSLA --user-agent "IBKRMonitor user@example.com" --start-date 2016-01-01 --output-csv data/events/pead_events.csv

# 查看完整参数与所需事件 CSV 表头格式
conda run -n mlx312 python scripts/run_pead_study.py --help

# 运行零未来函数（BMO/AMC 对齐）与 T+1 SettledCash 约束回测（10/20/30 bps 滑点压力矩阵）
conda run -n mlx312 python scripts/run_pead_study.py --events-csv data/events/pead_events.csv --history-dir data/history --initial-cash 11000.0 --output-dir data/backtests
```

- 必需事件 CSV 表头：`symbol,announcement_date,session,eps_actual,eps_estimate,revenue_actual,revenue_estimate,source`
- 可选关键参数：`--trades-dir`（未复权 `TRADES` 价格交叉核对）、`--holdout-start YYYY-MM-DD`（锁定 Holdout 切分）、`--slippage-grid`（默认 `10,20,30`）、`--universe-note`、`--min-eps-surprise`、`--min-revenue-surprise`、`--min-gap`、`--max-gap`、`--min-avg-dollar-volume`、`--min-price`、`--spy-regime-window`、`--holding-days`、`--stop-loss`、`--volume-multiplier`、`--no-t-plus-1`（仅用于敏感性对比）
- 输出：写入 `data/backtests/pead-study-latest.{json,md}`（及时间戳文件），包含 SPY 基准对比、剔除 Top-3 交易净收益、t-statistic 与 Kill Criteria 判定。

### Track 2：美股零股要约收购扫描 (`scripts/scan_odd_lots.py`)

```bash
# 离线扫描本地 .txt/.html/.htm/.json 要约公告并结合手工 Ask 报价测算净利
conda run -n mlx312 python scripts/scan_odd_lots.py --input-dir <filings_dir> --quotes "SYM=12.34" --voluntary-ca-fee 0.0 --commission 1.0 --slippage-bps 10.0 --max-shares 99

# 从 SEC EDGAR 拉取近 30 天含 odd-lot 条款的上市标的 SC TO-I / SC TO-T / SC 13E3 公告，并可选通过 TWS 延迟冻结快照读取参考报价
conda run -n mlx312 python scripts/scan_odd_lots.py --edgar --user-agent "IBKRMonitor user@example.com" --days 30 --limit 20 --listed-only --use-tws --host 127.0.0.1 --port 7497 --client-id 79 --market-data-type 4
```

- 其他参数：`--query`（默认 `'"odd lot" OR "odd-lot"'`）、`--listed-only`、`--forms`、`--as-of-date YYYY-MM-DD`、`--output-dir`（默认 `data/backtests`）
- 输出：写入 `data/backtests/odd-lot-scan-latest.{json,md}`（及时间戳文件），仅生成人工复核清单，需在 **IBKR Client Portal → Corporate Actions** 手工申报。

## 测试覆盖

166 个测试覆盖（`conda run -n mlx312 python -m pytest tests/ -q`）：
- 只读保护（AST 静态扫描禁止方法调用 + 配置/运行时校验）
- 账户脱敏（完整账户号不会出现在日志/导出/Phase 0 报告中）
- 组合识别（14种期权策略 + 边界情况：数量、到期日、乘数不匹配）
- 收益计算与期权成本（per-share vs per-contract、multiplier、手续费）
- 指数研究与定投回测（`BIL`/`SHY` 避险切换、滞回趋势过滤、漂移阈值再平衡、DCA TWR/MWR-XIRR、分年度/压力窗口/滚动胜率）
- No.2 Phase 0 与 Track 1-2 研究（TWS 前置诊断与 Track 3 门控、PEAD BMO/AMC 零未来函数与 T+1 `SettledCash`、零股要约解析与 Voluntary CA 费用测算）

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
├── trading/                    # 只读信号/风控/模拟单、指数回测、PEAD 回测与零股要约扫描
├── repositories/               # SQLite 异步存储
├── api/                        # 5 个路由模块（含筛选排序）
├── templates/                  # 4 个 HTML 页面
└── static/                     # CSS + JS
tests/                          # 20 个测试文件，166 个测试
scripts/                        # check_connection.py, check_no2_prerequisites.py, fetch_pead_events.py, run_backtest.py, run_index_study.py, run_pead_study.py, scan_odd_lots.py, run.ps1, install.ps1
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
