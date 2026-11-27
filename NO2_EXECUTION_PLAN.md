# No.2 美股中小资金策略（IBKR TWS）可行性核查与分阶段执行手册

**一句话结论：可以执行，且非常适合沿用“先只读证伪、零资金试错”的工程路线推进。**

`/Users/shingoxy/Documents/ibkr-portfolio-monitor` 已经具备完整的 IBKR TWS 只读连接层、历史日线拉取、风控闸门、模拟委托单生成与审计页面底座，无需推倒重来；但在策略未通过样本外证伪与实时前向观察之前，**严禁破坏仓库现有的三层只读守卫（Read-Only Guards）**，必须按 **Phase 0（账户与行情权限实测） → Phase 1（离线只读回测证伪） → Phase 2（2–4 周 TWS 实时只读前向观察） → Phase 3（独立隔离的 Paper/实盘执行）** 推进。

---

## 一、仓库现状核查摘要（2026-11-26 实测）

### 1. 现有可复用资产

| 维度 | 现状（已核实） | 对应路径 / 命令 |
| :--- | :--- | :--- |
| **运行环境与测试** | `conda run -n mlx312` 环境可用，全量 **90 项单元测试全部通过**（约 1.1s） | `conda run -n mlx312 python -m pytest tests/ -q` |
| **TWS 连接与只读客户端** | `IBKRClient(EWrapper, EClient)` 已支持 `accountSummary`（含 `AccountType`、`SettledCash`、`AvailableFunds`、`DayTradesRemaining`）、`position`、`openOrder`、`execDetails`、`tickPrice`、`tickSize`、`historicalData` / `historicalDataEnd`、`pnl` | `app/ibkr/client.py`、`app/ibkr/connection_manager.py`、`app/ibkr/contracts.py`、`scripts/check_connection.py` |
| **历史行情适配边界** | `IBKRHistoricalBarProvider.get_daily_bars` 已跑通日线拉取；`scripts/run_backtest.py` 默认使用 `what_to_show="ADJUSTED_LAST"` 并将 CSV 缓存在 `data/history/` | `app/trading/historical_provider.py`、`scripts/run_backtest.py` |
| **信号 / 风控 / 模拟单管道** | `TradingService` → `RiskGate` → `OrderPlanner`（0.20% 限价缓冲）→ `AuditRepository`（SQLite）→ `/trading` 审核页，已形成完整只读信号闭环 | `app/trading/service.py`、`app/trading/risk_gate.py`、`app/trading/order_planner.py`、`app/api/routes_trading.py`、`app/templates/trading.html` |
| **回测与指数基准对照** | 已完成 10 标的 50/150 日均线轮动回测及指数核心方案对照 | `app/trading/backtest.py`、`app/trading/index_backtest.py`、`scripts/run_index_study.py`、`data/backtests/index-study-latest.md` |

### 2. 新会话开始前必须注意的 3 个现状

1. **三层只读守卫处于严格生效状态（Phase 0–2 期间必须保持）**：
   - **配置层**（`app/config.py`）：`read_only: bool = True`，`validate_read_only` 会在 `READ_ONLY=false` 时直接 `sys.exit(1)`；`app_host` 强制仅允许 `127.0.0.1` / `localhost`；`trading_enabled` 默认 `False`、`trading_mode` 仅允许 `signal_only` 或 `paper`。
   - **运行时**（`app/ibkr/client.py`）：`IBKRClient.__init__` 调用 `validate_no_forbidden_methods(self.__class__)`。
   - **静态测试**（`tests/test_read_only_guards.py`）：对 `app/` 目录下所有 Python 文件做 AST 扫描，禁止任何对 `placeOrder`、`cancelOrder`、`reqGlobalCancel`、`exerciseOptions` 的调用或属性引用。
2. **根目录尚无 `.env` 文件**：当前仅存在 `.env.example`，启动连接 TWS 前需要先复制生成 `.env`。
3. **当前 Git 工作区存在未提交改动**：
   - 已修改：`.gitignore`、`README.md`、`app/config.py`、`app/trading/historical_provider.py`、`app/trading/risk_gate.py`、`app/trading/service.py`、`tests/test_historical_provider.py`、`tests/test_risk_gate.py`、`tests/test_trading_service.py`
   - 未跟踪：`AGENTS.md`、`CLAUDE.md`、`NO2_EXECUTION_PLAN.md`、`app/trading/backtest.py`、`app/trading/index_backtest.py`、`docs/`、`scripts/run_backtest.py`、`scripts/run_index_study.py`、`tests/test_backtest.py`、`tests/test_index_backtest.py`
   - **警告**：新会话切勿执行 `git reset` / `git checkout --` 清理工作区；建议先确认是否将这些现有研究基线提交为一个单独 commit，再开始新分支或新模块开发。

### 3. 已有回测基线对照（必须跨过的门槛）

根据 `data/backtests/index-study-latest.md`（初始资金 `11,000 美元`，区间 `2024-07-09` 至 `2026-09-18`，每笔佣金 `1.00 美元` + 滑点 `5.0 bps`）：

| 对比方案 | 期末权益 | 总收益 | 年化收益 | 最大回撤 | Sharpe | 委托数 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| **SPY 买入持有（默认基准）** | `$15,386.38` | `39.88%` | `16.54%` | `-18.52%` | `1.024` | 1 |
| SPY 70% / QQQ 30% 季度再平衡 | `$15,431.82` | `40.29%` | `16.69%` | `-19.11%` | `0.993` | 8 |
| SPY 70% / QQQ 30% + 200 日趋势过滤 | `$12,957.38` | `17.79%` | `7.75%` | `-10.05%` | `0.637` | 15 |
| 原股票/ETF 50/150 均线轮动 | `$11,488.13` | `4.44%` | `2.00%` | `-7.99%` | `0.263` | - |

**核心启示**：任何新纳入执行候选的策略，都必须在**严格扣费、高滑点压力与样本外 Holdout** 下，证明能在净收益、夏普比率或与 SPY 的低相关性组合增量上显著优于直接持有 SPY。

---

## 二、账户与 API 硬约束核查（决定子策略取舍）

### 1. 账户主体与交收约束（先按最严口径兜底）

- **若账户归属日本 IBKR（IBSJ，Interactive Brokers Securities Japan）**：
  - **豁免 FINRA PDT 规则**：不受美国居民账户 `25,000 美元` 日内交易次数限制。
  - **严格仅限现金账户（Cash Account）**：个人客户不提供 Margin 融资融券，**不可融券做空个股**。
  - **美股 T+1 交收约束**：卖出美股所得资金在 `T+1` 日交收完成前属于未交收资金（Unsettled Cash），**禁止当日反复循环用于新开仓或买卖同一标的**（否则会触发现金违规 / 限制账户）。
- **若账户归属其他主体（如 IB LLC / IB HK 保证金账户且净值 `< $25,000`）**：
  - **受 PDT 规则限制**：连续 5 个交易日内日内交易（Day Trades）上限为 3 次。
- **工程兜底原则**：在 Phase 0 实测确认 `AccountType` 与账户主体规则前，**所有策略设计与回测模拟一律按“纯多头（Long-Only）+ 严格受 `SettledCash` T+1 约束”构建**。

### 2. TWS API 能力与数据边界

| 能力项 | 本仓库现状 | 约束与补齐方式 |
| :--- | :--- | :--- |
| **日线历史行情** | 已接入 `reqHistoricalData(..., "1 day")` | 回测必须注意：`ADJUSTED_LAST` 会随历史分红拆股回溯调整，**必须同时对比 `TRADES` 未复权价格**验证真实跳空与成交价。 |
| **5 分钟历史行情** | **尚未接入** | `reqHistoricalData` 单次最多 50 个并发请求、全局约 50 req/s（`PACEAPI`）；拉取 `"5 mins"` 建议按 `1 W` 或 `1 M` 分段并加 `0.3–0.5s` 限流休眠；且 IBKR 缺乏已退市股完整历史，**不能仅靠 IBKR 做多年度无幸存者偏差回测**。 |
| **市场扫描器** | **尚未接入**（`IBKRClient` 未实现 `scannerData` / `scannerDataEnd`） | `reqScannerSubscription` 上限 **10 路同时激活、每次最多返回 50 个合约**，且仅返回合约描述，**不直接返回实时 RVOL 或价格**，必须二次订阅行情或历史柱线自行计算 RVOL。 |
| **实时美股行情** | 依赖账户市场数据订阅权限（`market_data_type=1`） | 5 分钟 ORB 实盘/前向验证**强制要求付费实时 Level 1 行情**；若仅有 15 分钟延时行情（错误码 `10089` / `354` 或 `market_data_type=3`），Track 3 直接不可做。 |
| **历史财报惊喜数据** | **TWS 原生不提供干净的多年度历史 EPS 预期差与精确发布时间戳** | 必须外接 Point-in-Time 外部数据源（SEC EDGAR 8-K 官方时间戳 + 公开/低成本历史财报预期与实绩数据集），不能仅靠 TWS `reqFundamentalData`。 |

---

## 三、No.2 三条子策略的适配度与执行优先级

### Track 1（最高优先级）：纯多头 PEAD（财报超预期后漂移，持仓 5–20 交易日）

- **为什么排第一**：
  1. **零做空、天然合规 T+1**：只做财报超预期后的多头持有（5–20 个交易日），既不依赖融券做空，也完全不受日内资金循环或 PDT 约束。
  2. **零延迟劣势 & 无需高频订阅**：重点捕捉财报发布后的开盘确认段至数周趋势漂移，甚至可在日线/盘前快照阶段完成决策，公网延迟与机构无实质劣势。
  3. **中小资金容量优势**：`10,000–100,000 美元` 账户聚焦中小盘业绩超预期标的时冲击成本极低，而大型对冲基金难以有效配置。
- **核心数据与工程要求**：
  - 数据源：结合 **SEC EDGAR（8-K Item 2.02 / 财报公告）** 获取精确披露时间，并叠加外部历史财报日历（EPS Actual vs Consensus / Revenue Surprise / 涨跌幅与跳空过滤）。
  - 严防未来函数：**必须严格区分 BMO（盘前发布）与 AMC（盘后发布）**：
    - **BMO**：最早只能按当日开盘价（或开盘后 5–15 分钟均价）入场，严禁用发布前一天收盘价回测。
    - **AMC**：最早只能按次一交易日开盘价（或开盘后确认价）入场。
  - 跳空与成本：使用 `TRADES` 未复权价格核对财报次日真实跳空，按单边 `10 / 20 / 30 bps` 滑点 + 每笔 `1.00 美元` 做敏感性测试，并引入退市股或显式审计幸存者偏差。
  - 复用路径：直接复用 `app/trading/risk_gate.py`、`order_planner.py`、`audit_repository.py` 与 `/trading` 界面。

### Track 2（高性价比补充，半自动）：美股零股要约收购套利（Odd-Lot Tender Offers）

- **为什么值得同步推进**：
  1. **零延迟、零实时行情成本、100% 保持本仓库只读**：脚本无需任何自动化下单接口，完全不触碰只读守卫。
  2. **极典型的小资金容量盲区**：上市公司要约回购（Tender Offer）常规定 **99 股以下（Odd-Lot，`< 100` 股）持有人豁免按比例配售（Odd-Lot Priority）**。单次锁定利润通常为几十到几百美元，大资金做不了，对小账户反而是确定性较高的短周期事件收益。
- **执行方式**：
  - 自动化部分：编写只读扫描脚本抓取 SEC EDGAR `SC TO-I` / `SC TO-T` / `SC 13E3` 公告并解析关键字（`odd lot`、`99 shares`、要约价格区间、截止日期），通过 TWS API 只读读取当前 `Ask` 报价，自动计算扣除手续费后的预期净收益与年化。
  - 人工部分：确认有净利且无撤回条款风险后，在现货买入 99 股并通过 **IBKR Client Portal → Corporate Actions** 人工提交要约申报。
- **前置核查项**：运行前**必须先核实当前 IBKR 账户对美股自愿公司行动（Voluntary Tender Offer Election）是否收取单笔处理费**（部分账户可能免收或收固定费用，若单笔收费过高会直接吃光小额利润）。

### Track 3（条件解锁）：纯多头 5m RVOL-ORB（Stocks in Play，日内平仓）

- **解锁前提（缺少任一项即暂缓）**：
  1. Phase 0 实测确认 TWS 已开通**美股实时 Level 1 行情订阅**（`market_data_type=1` 无 10089/354 报错）。
  2. 操作者/运行环境能在美股开盘时段（**23:30 JST 冬令时 / 22:30 JST 夏令时**）保持本地 TWS/Gateway 稳定在线。
  3. 仅做**纯多头**（符合 IBSJ 无融券限制），且每次建仓资金严格受 `SettledCash` T+1 可用已交收余额池限制（例如将可用资金分成 2 个轮动池交替使用，避免同日卖出后资金锁死）。
- **必须补齐的工程底座**：
  - `app/ibkr/client.py`：增量实现只读回调 `scannerData`、`scannerDataEnd`，配合 `reqScannerSubscription` 筛选高成交额/高涨幅异动候选池（最多 50 只）。
  - `app/trading/historical_provider.py`：新增 `get_intraday_bars(symbol, bar_size="5 mins", ...)`，内置严格限流休眠与队列隔离。
  - 离线回测：必须外接覆盖已退市股的干净多年度 5 分钟美股历史数据（不能仅用现存成分股做回测），重点对开盘前 5 分钟突破价加压测试 **`15 / 30 / 40 bps` 高滑点**。

---

## 四、分阶段执行步骤（给新会话的逐步操作手册）

### Phase 0：环境与账户硬条件实测（1 次只读联调）

在新建研究代码前，先用 1 个会话完成基础检查与真账号/Paper 账号能力摸底：

1. **生成本地 `.env`（不入 Git）**：
   ```bash
   cd /Users/shingoxy/Documents/ibkr-portfolio-monitor
   cp .env.example .env
   ```
   确认 `.env` 中保持 `READ_ONLY=true`、`APP_HOST=127.0.0.1`、`IBKR_PORT=7497`（Paper TWS；若连接只读 Live TWS 需核对对应端口，且保持 TWS Global Configuration 中勾选 `Read-Only API`）。
2. **验证基准测试与 TWS Socket 连通性**：
   ```bash
   conda run -n mlx312 python -m pytest tests/ -q
   conda run -n mlx312 python scripts/check_connection.py --port 7497
   ```
3. **运行一次只读账户与行情权限诊断**（可编写一次性只读脚本 `scripts/check_no2_prerequisites.py`）：
   - 读取 `accountSummary` 中的 `AccountType`（确认是 `CASH` 还是 `MARGIN`）、`NetLiquidation`、`SettledCash`、`AvailableFunds`、`DayTradesRemaining`。
   - 用 `market_data_type=1` 请求 `SPY` 和 1 只美股实时行情（验证 `bid` / `ask` / `last` 是否有付费实时权限，还是触发 `10089` / `354` 延时提示）。
   - 请求 `SPY` 最近 `5 D` 的 `"5 mins"` `TRADES` 历史柱线，验证分钟线接口返回与耗时。
   - **人工核查 1 项费率**：登录 IBKR Client Portal / 费率表确认美股佣金结构（Tiered vs Fixed）及 Voluntary Corporate Action（自愿要约收购申报）手续费。

---

### Phase 1：离线只读回测与严格证伪（不触碰三层只读守卫）

> **铁律**：整个 Phase 1 **不引入任何下单接口**，每增加模块同步补全单元测试，保证 `conda run -n mlx312 python -m pytest tests/ -q`（含 `tests/test_read_only_guards.py`）持续 100% 通过。

#### 1. 建议新增的只读研究模块规划

| 目标策略 | 建议新增模块（保持纯函数 + 只读） | 职责说明 |
| :--- | :--- | :--- |
| **Track 1（PEAD）** | `app/trading/pead_strategy.py`<br>`app/trading/pead_backtest.py`<br>`scripts/run_pead_study.py`<br>`tests/test_pead_backtest.py` | 财报事件筛选（EPS/营收惊喜 + 跳空/成交额过滤 + 大盘 Regime 过滤）、BMO/AMC 对齐成交、T+1 `SettledCash` 约束回测、输出 JSON/MD 报告到 `data/backtests/` |
| **Track 2（零股要约）** | `app/trading/odd_lot_scanner.py`<br>`scripts/scan_odd_lots.py`<br>`tests/test_odd_lot_scanner.py` | SEC EDGAR 要约公告解析（提取股份上限/价格/截止日）、结合 TWS `Ask` 报价测算扣费后净利与年化（仅生成人工复核清单） |
| **Track 3（5m ORB，条件解锁）** | 仅在 Phase 0 确认有实时行情后扩展：<br>`app/ibkr/client.py`（加 `scannerData`/`scannerDataEnd`）<br>`app/trading/historical_provider.py`（加 `get_intraday_bars`）<br>`app/trading/orb_backtest.py`、`scripts/run_orb_study.py` | 日内 5m RVOL Top-N 筛选、突破与 `10% ATR` 止损模拟、`SettledCash` 分桶控制与多档重滑点压测 |

#### 2. 必须全部落地的 6 条防作弊校验红线

1. **严格切分开发集与锁定 Holdout**：例如按时间前 70%（或早期年份）调参筛选规则，后 30%（最近 1–2 年）设为锁定 Holdout，**只在定版后跑一次**，严禁看 Holdout 结果反过来改参数。
2. **零未来函数时间戳对齐**：PEAD 必须按 BMO/AMC 精确切分可交易第一根柱线；ORB 必须仅用前 5 分钟结束时刻已知的 RVOL 排名决定是否挂触发单。
3. **幸存者偏差控制与披露**：不得仅用今日高市值白马股回测；必须尽可能接入含退市股票的历史标的池（至少在报告中显式披露成分股来源及偏差方向）。
4. **未复权价格与拆股分红核对**：使用 `TRADES` 未复权价核验真实开盘价、止损触发价与 `RiskGate` 整数股仓位计算，并与 `ADJUSTED_LAST` 交叉校验，防止前复权失真。
5. **成本、滑点与 T+1 现金全额扣减**：
   - 佣金按 IBKR 真实费率（保底每笔 `1.00 美元` 或对应阶梯费率 + SEC/FINRA 卖出微费）。
   - 滑点做压力梯度矩阵：PEAD 跑 `10 / 20 / 30 bps`；ORB 跑 `15 / 30 / 40 bps`。
   - 若账户为 Cash Account，回测账本必须建模 `settled_cash`（`T+1` 释放），拒绝使用未交收卖出款当日开新仓。
6. **基准与稳健性检验**：输出必须与同期 **SPY 买入持有** 对照，并额外输出 **剔除利润最高的 Top 3 单笔交易后的净收益** 与 **t-statistic**，防止“全靠一两只超级妖股撑起曲线”的伪正期望。

---

### Phase 2：TWS 实时只读前向观察（Signal-Only Forward Check，2–4 周）

Phase 1 通过后，不急于切下单，先复用本仓库已有的 `/trading` 审核链路跑 **2–4 周实时只读前向核验**：

1. 保持 `READ_ONLY=true` 与 `trading_mode="signal_only"`。
2. 每日盘前/盘后由定时脚本只读拉取 TWS 实时快照与历史柱线，调用 `TradingService` / 新策略服务生成模拟信号与 `OrderPlan`，通过 `AuditRepository` 写入 `data/ibkr_portfolio.db` 并记录当时的 **真实盘口 `Bid / Ask / Spread / 估算金额`**。
3. 次日开盘与持有期末自动回填实际市场成交区间，对比 **“实时盘口可执行价 vs 离线回测假设价”** 的真实滑点偏差与遗漏成交率。

---

### Phase 3：Paper 自动下单与实盘隔离架构（仅在 Phase 1+2 通过后开启）

为避免破坏 `ibkr-portfolio-monitor` 作为“严格只读持仓监控与研究底座”的安全性，**不要直接在本仓库里关闭 `read_only` 守卫**。当 Phase 1 与 Phase 2 均明确达标后，采用隔离方案：

1. **独立执行目录 / 仓库**：新建独立执行器（如 `ibkr-paper-executor`），仅以只读方式读取本仓库生成的经过审核的信号或共用纯策略库，本仓库 `tests/test_read_only_guards.py` 永远保持全绿。
2. **Paper 强制安全锁（4–8 周）**：
   - 执行器代码硬校验：端口必须为 TWS Paper `7497`（或 Gateway Paper `4002`），且 `managedAccounts()` 返回账号前缀必须为 `DU`（Paper 账号前缀）才允许发出 `placeOrder`。
   - 必须使用带保护的 **限价单（LMT / Stop-Limit）**，禁止无保护市价单（MKT）裸追开盘或跳空流动性枯竭标。
   - 内置硬熔断：单日最大亏损止损、单月最大回撤熔断、T+1 `SettledCash` 余额校验（低于保留门槛拒绝发单）、开盘前/断线重连后对账幂等检查（防重复下单）。
3. **实盘准入条件**：仅在 Paper 连续稳定运行 4–8 周且实盘模拟执行曲线、滑点分布、对账异常率全部符合预期后，再决定是否切入小资金实盘，并在日本税制下按标准股票申告分离课税（20.315%）做好交易流水归档。

---

## 五、明确止损 / 放弃标准（Kill Criteria）

延续“先证伪、零亏损”的原则，只要命中以下任意一条红线，**立即止损归档该子策略，不投入 1 美元实盘**：

| 子策略 | 触发直接放弃（Kill）的量化红线 |
| :--- | :--- |
| **Track 1（PEAD）** | 1. 在 `20 bps` 滑点 + 真实 `BMO/AMC` 开盘次日成交价下，锁定 Holdout 样本的净 Sharpe 无法达到 `>= 1.0` 或明显跑输同期 SPY 买入持有且不能降低组合回撤；<br>2. 剔除收益最高的 Top 3 交易后策略期望转为零或负值（说明统计不显著、依赖极端运气）；<br>3. Phase 2 实时盘口前向观察发现中小盘财报开盘真实点差/滑点持续超出 `30 bps` 导致收益被吞噬。 |
| **Track 2（零股要约）** | 1. 核实 IBKR 账户收取较高的 Voluntary Corporate Action 单笔处理费，导致 `< 100` 股要约单笔预期净利扣费后不足覆盖时间与换汇成本；<br>2. 近 6 个月美股公开 `SC TO-I` 中符合豁免条件且价差充足的有效机会年化频次过低。 |
| **Track 3（5m ORB）** | 1. Phase 0 确认账户无美股实时 Level 1 行情订阅（且不愿额外付费订阅），或无法在 22:30–23:30 JST 稳定保持服务运行；<br>2. 在 `30 bps` 开盘 5m 突破滑点 + T+1 `SettledCash` 限制下，Holdout 净收益为负或预期 Sharpe 低于 `0.8`；<br>3. 5m 信号严重依赖退市前幸存者偏差样本，补全退市标的后失效。 |

---

## 六、新会话可直接复制的启动提示词

在 `/Users/shingoxy/Documents/ibkr-portfolio-monitor` 打开新会话后，可直接发送以下指令：

> 请先阅读 `AGENTS.md`、`NO2_EXECUTION_PLAN.md` 和 `data/backtests/index-study-latest.md`，保持现有三层只读守卫（`READ_ONLY=true`）与未提交基线文件不被破坏。
>
> 我们按 `NO2_EXECUTION_PLAN.md` 推进 Phase 0：
> 1. 检查当前 `conda run -n mlx312 python -m pytest tests/ -q` 与 `.env` 状态（先仅连 Paper 或只读 TWS，不修改任何下单相关限制）；
> 2. 写一个只读诊断脚本核实我的 IBKR 账户类型（是否 Cash Account / 有无 PDT / `SettledCash`）、美股实时行情权限（`market_data_type=1` 是否报错 10089/354）以及 `5 mins` 历史行情接口返回情况；
> 3. 完成 Phase 0 核查并向我汇报实测结果后，再和我确认 Track 1（PEAD）和 Track 2（Odd-Lot 要约扫描）的历史数据源选型，开始 Phase 1 离线只读证伪。
