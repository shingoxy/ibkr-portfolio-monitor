"""Compare index portfolios (Lump-Sum & Monthly DCA) using cached or synced IBKR daily bars."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.ibkr.client import IBKRClient  # noqa: E402
from app.trading.backtest import BacktestConfig, run_backtest  # noqa: E402
from app.trading.historical_provider import IBKRHistoricalBarProvider  # noqa: E402
from app.trading.index_backtest import (  # noqa: E402
    IndexBacktestConfig,
    PortfolioBacktestResult,
    PortfolioRule,
    run_index_backtest,
)
from app.trading.models import Bar  # noqa: E402
from app.trading.swing_strategy import SwingTrendStrategy  # noqa: E402

ALL_SYMBOLS = ["SPY", "QQQ", "VTI", "IWM", "DIA", "NVDA", "TSM", "TSLA", "AAPL", "MSFT"]
INDEX_SYNC_SYMBOLS = list(dict.fromkeys(["SPY", "QQQ", "BIL", "SHY", *ALL_SYMBOLS]))
INFO_ERROR_CODES = {2104, 2106, 2107, 2108, 2158}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare benchmark-first index strategies")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--initial-cash", type=float, default=11_000.0)
    parser.add_argument("--monthly-contribution", type=float, default=500.0)
    parser.add_argument("--defensive-symbol", default="BIL")
    parser.add_argument("--drift-threshold", type=float, default=0.05)
    parser.add_argument("--trend-buffer", type=float, default=0.02)
    parser.add_argument("--history-dir", type=Path, default=PROJECT_ROOT / "data" / "history")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "backtests")
    parser.add_argument("--sync", action="store_true", help="Sync historical bars from TWS before running")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=78)
    return parser.parse_args()


async def download_bars(
    client: IBKRClient,
    symbols: list[str],
    lookback_days: int,
) -> dict[str, list[Bar]]:
    provider = IBKRHistoricalBarProvider(
        client,
        timeout_seconds=90.0,
        what_to_show="ADJUSTED_LAST",
    )
    bars_by_symbol: dict[str, list[Bar]] = {}
    for symbol in symbols:
        bars = await provider.get_daily_bars(symbol, lookback_days)
        if len(bars) < 50:
            raise RuntimeError(f"insufficient history for {symbol}: {len(bars)} bars")
        bars_by_symbol[symbol] = bars
        print(f"{symbol}: {len(bars)} bars ({bars[0].date} to {bars[-1].date})")
        await asyncio.sleep(0.5)
    return bars_by_symbol


def sync_history_from_tws(
    host: str,
    port: int,
    client_id: int,
    history_dir: Path,
    symbols: list[str],
    lookback_days: int,
) -> dict[str, list[Bar]]:
    """Connect to TWS strictly read-only, fetch daily ADJUSTED_LAST bars, and save CSVs."""
    client = IBKRClient()
    try:
        client.connect(host, port, client_id)
        threading.Thread(target=client.run, daemon=True).start()
        if not client.connected_event.wait(10):
            raise RuntimeError(f"TWS API handshake failed at {host}:{port}")
        bars_by_symbol = asyncio.run(download_bars(client, symbols, lookback_days))
        errors = client.drain_errors()
        blocking_errors = [
            error for error in errors if error.get("errorCode") not in INFO_ERROR_CODES
        ]
        if blocking_errors:
            raise RuntimeError(f"IBKR returned errors: {blocking_errors}")
    finally:
        client.disconnect()

    save_history(history_dir, bars_by_symbol)
    return bars_by_symbol


def save_history(history_dir: Path, bars_by_symbol: dict[str, list[Bar]]) -> None:
    history_dir.mkdir(parents=True, exist_ok=True)
    for symbol, bars in bars_by_symbol.items():
        with (history_dir / f"{symbol}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for bar in bars:
                writer.writerow([bar.date.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume])


def load_history(history_dir: Path, symbols: list[str]) -> dict[str, list[Bar]]:
    bars_by_symbol: dict[str, list[Bar]] = {}
    for symbol in symbols:
        path = history_dir / f"{symbol}.csv"
        if not path.exists():
            raise RuntimeError(f"cached history is missing for {symbol}: {path}")
        with path.open(newline="", encoding="utf-8") as handle:
            bars_by_symbol[symbol] = [
                Bar(
                    symbol=symbol,
                    date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )
                for row in csv.DictReader(handle)
            ]
    return bars_by_symbol


def resolve_defensive_symbol(
    history_dir: Path,
    requested_symbol: str,
) -> tuple[str | None, str | None]:
    """Return active defensive ETF symbol (with SHY fallback) and optional sync notice."""
    requested = requested_symbol.strip().upper()
    if (history_dir / f"{requested}.csv").exists():
        return requested, None
    if requested != "SHY" and (history_dir / "SHY.csv").exists():
        return (
            "SHY",
            f"未找到 {requested} 本地缓存，已自动回退使用 SHY。如需补齐 {requested}，请在 TWS 运行后执行："
            "conda run -n mlx312 python scripts/run_index_study.py --years 10 --sync",
        )
    return (
        None,
        f"未检测到 {requested}（或备选 SHY）本地历史缓存，本次仅运行纯股票/现金避险方案。"
        "请在 TWS 启动后执行 `conda run -n mlx312 python scripts/run_index_study.py --years 10 --sync` 拉取 10 年数据与避险 ETF。",
    )


def save_report(
    output_dir: Path,
    lump_results: list[PortfolioBacktestResult],
    dca_results: list[PortfolioBacktestResult],
    active_result: object,
    args: argparse.Namespace,
    active_defensive_symbol: str | None,
    notes: list[str],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    conclusions = _build_conclusions(
        lump_results=lump_results,
        dca_results=dca_results,
        active_result=active_result,
        active_defensive_symbol=active_defensive_symbol,
        monthly_contribution=args.monthly_contribution,
        notes=notes,
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "initial_cash": args.initial_cash,
        "monthly_contribution": args.monthly_contribution,
        "requested_years": args.years,
        "defensive_symbol": active_defensive_symbol,
        "drift_threshold_pct": args.drift_threshold,
        "trend_buffer_pct": args.trend_buffer,
        "notes": notes,
        "index_strategies": [result.to_dict() for result in lump_results],
        "dca_strategies": [result.to_dict() for result in dca_results],
        "active_stock_rotation": active_result.to_dict(),
        "conclusions": conclusions,
        "methodology": {
            "price_source": "IBKR ADJUSTED_LAST cached daily bars",
            "commission_per_order": 1.0,
            "slippage_bps": 5.0,
            "signal_timing": "prior close signal, next open execution",
            "cash_yield": 0.0,
            "contribution_frequency": "monthly" if args.monthly_contribution > 0 else "none",
        },
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path = output_dir / f"index-study-{timestamp}.json"
    json_path.write_text(json_text, encoding="utf-8")
    (output_dir / "index-study-latest.json").write_text(json_text, encoding="utf-8")

    markdown = _markdown_report(
        lump_results=lump_results,
        dca_results=dca_results,
        active_result=active_result,
        args=args,
        active_defensive_symbol=active_defensive_symbol,
        notes=notes,
        conclusions=conclusions,
    )
    markdown_path = output_dir / f"index-study-{timestamp}.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    (output_dir / "index-study-latest.md").write_text(markdown, encoding="utf-8")
    return json_path, markdown_path


def _build_conclusions(
    lump_results: list[PortfolioBacktestResult],
    dca_results: list[PortfolioBacktestResult],
    active_result: object,
    active_defensive_symbol: str | None,
    monthly_contribution: float,
    notes: list[str],
) -> list[str]:
    spy = lump_results[0]
    best_return = max(lump_results, key=lambda r: r.total_return_pct)
    best_sharpe = max(lump_results, key=lambda r: r.sharpe_ratio)
    lowest_dd = max(lump_results, key=lambda r: r.max_drawdown_pct)  # least negative

    lines = [
        f"SPY 买入持有基准：总收益 {spy.total_return_pct:.2f}%，年化 {spy.cagr_pct:.2f}%，最大回撤 {spy.max_drawdown_pct:.2f}%，Sharpe {spy.sharpe_ratio:.3f}。",
        f"一次性投入最优收益方案为「{best_return.strategy_name}」（总收益 {best_return.total_return_pct:.2f}%，Sharpe {best_return.sharpe_ratio:.3f}）；"
        f"最高风险调整收益方案为「{best_sharpe.strategy_name}」（Sharpe {best_sharpe.sharpe_ratio:.3f}，总收益 {best_sharpe.total_return_pct:.2f}%）。",
        f"最小最大回撤方案为「{lowest_dd.strategy_name}」（最大回撤 {lowest_dd.max_drawdown_pct:.2f}%，平均仓位 {lowest_dd.average_exposure_pct:.2f}%）。",
    ]

    for res in lump_results[1:]:
        diff_ret = res.total_return_pct - spy.total_return_pct
        diff_dd = res.max_drawdown_pct - spy.max_drawdown_pct
        diff_sharpe = res.sharpe_ratio - spy.sharpe_ratio
        rolling_beat = (
            f"，滚动1年胜率 {res.rolling_1y['beat_benchmark_rate_pct']:.1f}%"
            if res.rolling_1y.get("sample_count", 0) > 0 and res.rolling_1y.get("beat_benchmark_rate_pct") is not None
            else ""
        )
        lines.append(
            f"「{res.strategy_name}」相对 SPY：收益差 {diff_ret:+.2f}%，回撤差 {diff_dd:+.2f}%（正值表示回撤更小），"
            f"Sharpe 差 {diff_sharpe:+.3f}，委托 {res.order_count} 笔{rolling_beat}。"
        )

    if dca_results and monthly_contribution > 0:
        spy_dca = dca_results[0]
        best_dca = max(dca_results, key=lambda r: r.final_equity)
        lines.append(
            f"定投模式（每月 +${monthly_contribution:,.0f}）：SPY 累计投入 ${spy_dca.total_contributed:,.2f}、"
            f"期末 ${spy_dca.final_equity:,.2f}（净盈利 ${spy_dca.net_profit:,.2f}，TWR 年化 {spy_dca.twr_cagr_pct:.2f}%，MWR/XIRR {spy_dca.mwr_xirr_pct:.2f}%）；"
            f"期末资产最高为「{best_dca.strategy_name}」（${best_dca.final_equity:,.2f}，XIRR {best_dca.mwr_xirr_pct:.2f}%）。"
        )

    lines.append(
        f"同期 50/150 股票/ETF 轮动（原方案）总收益 {active_result.total_return_pct:.2f}%、最大回撤 {active_result.max_drawdown_pct:.2f}%、"
        f"Sharpe {active_result.sharpe_ratio:.3f}（相对 SPY 收益差 {active_result.total_return_pct - spy.total_return_pct:+.2f}%）。"
    )
    if active_defensive_symbol is not None:
        lines.append(f"已启用避险资产 {active_defensive_symbol} 接收趋势转弱时的退出权重。")
    for note in notes:
        lines.append(note)
    return lines


def _markdown_report(
    lump_results: list[PortfolioBacktestResult],
    dca_results: list[PortfolioBacktestResult],
    active_result: object,
    args: argparse.Namespace,
    active_defensive_symbol: str | None,
    notes: list[str],
    conclusions: list[str],
) -> str:
    first = lump_results[0]
    lines = [
        "# 指数核心方案对照回测",
        "",
        f"- 区间：{first.start_date} 至 {first.end_date}",
        f"- 初始资金：${args.initial_cash:,.2f}（定投方案每月追加 ${args.monthly_contribution:,.2f}）",
        f"- 参数：目标年数 {args.years} 年 | 漂移阈值 {args.drift_threshold:.1%} | 趋势双缓冲 {args.trend_buffer:.1%} | 避险标的 {active_defensive_symbol or '无（纯现金）'}",
        "- 行情：IBKR ADJUSTED_LAST 日线；200 个交易日仅用于预热，不计入正式收益",
        "- 成交：前一交易日收盘形成信号，下一交易日开盘成交；整数股",
        "- 成本：每笔委托 $1.00，滑点 5.0 bps；现金收益按 0 计算",
    ]
    if notes:
        lines.append("")
        for note in notes:
            lines.append(f"> 注意：{note}")

    lines.extend(
        [
            "",
            "## 1. 一次性投入对照（Lump-Sum）",
            "",
            "| 方案 | 期末权益 | 总收益 | 年化 | 最大回撤 | 年化波动 | Sharpe | 平均仓位 | 委托数 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for result in lump_results:
        lines.append(
            f"| {result.strategy_name} | ${result.final_equity:,.2f} | {result.total_return_pct:.2f}% | "
            f"{result.cagr_pct:.2f}% | {result.max_drawdown_pct:.2f}% | "
            f"{result.annualized_volatility_pct:.2f}% | {result.sharpe_ratio:.3f} | "
            f"{result.average_exposure_pct:.2f}% | {result.order_count} |"
        )
    active_exposure = _average_exposure(active_result.equity_curve)
    lines.append(
        f"| 股票/ETF 50/150 轮动（原方案） | ${active_result.final_equity:,.2f} | "
        f"{active_result.total_return_pct:.2f}% | {active_result.cagr_pct:.2f}% | "
        f"{active_result.max_drawdown_pct:.2f}% | {active_result.annualized_volatility_pct:.2f}% | "
        f"{active_result.sharpe_ratio:.3f} | {active_exposure:.2f}% | - |"
    )

    lines.extend(
        [
            "",
            f"## 2. 每月定投对照（Monthly DCA +${args.monthly_contribution:,.0f}/月）",
            "",
            "| 方案 | 累计投入 | 期末权益 | 净盈利 | TWR总收益 | TWR年化 | MWR/XIRR | TWR最大回撤 | Sharpe | 委托数 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for result in dca_results:
        lines.append(
            f"| {result.strategy_name} | ${result.total_contributed:,.2f} | ${result.final_equity:,.2f} | "
            f"${result.net_profit:,.2f} | {result.twr_total_return_pct:.2f}% | {result.twr_cagr_pct:.2f}% | "
            f"{result.mwr_xirr_pct:.2f}% | {result.max_drawdown_pct:.2f}% | "
            f"{result.sharpe_ratio:.3f} | {result.order_count} |"
        )

    lines.extend(
        [
            "",
            "## 3. 分年度收益分解（Lump-Sum）",
            "",
            "| 年度 | 区间 | 方案 | 年度收益 | 最大回撤 | 平均仓位 |",
            "|---:|---|---|---:|---:|---:|",
        ]
    )
    years = sorted({row["year"] for result in lump_results for row in result.annual_breakdown})
    for year in years:
        for result in lump_results:
            match = next((row for row in result.annual_breakdown if row["year"] == year), None)
            if match is None:
                continue
            lines.append(
                f"| {match['year']} | {match['start_date']} ~ {match['end_date']} | {result.strategy_name} | "
                f"{match['return_pct']:.2f}% | {match['max_drawdown_pct']:.2f}% | {match['avg_exposure_pct']:.2f}% |"
            )

    lines.extend(
        [
            "",
            "## 4. 历史压力测试区间（覆盖 >= 5 交易日）",
            "",
        ]
    )
    covered_windows = [
        (row["name"], row["start_date"], row["end_date"], row["trading_days"])
        for row in lump_results[0].stress_periods
    ]
    if not covered_windows:
        lines.append("当前样本区间未覆盖任何预定义压力窗口（需同步更长历史数据，例如 10 年）。")
    else:
        lines.extend(
            [
                "| 压力窗口 | 区间 | 交易日 | 方案 | 区间收益 | 最大回撤 | 平均仓位 |",
                "|---|---|---:|---|---:|---:|---:|",
            ]
        )
        for window_name, _, _, _ in covered_windows:
            for result in lump_results:
                match = next((row for row in result.stress_periods if row["name"] == window_name), None)
                if match is None:
                    continue
                lines.append(
                    f"| {match['name']} | {match['start_date']} ~ {match['end_date']} | {match['trading_days']} | "
                    f"{result.strategy_name} | {match['return_pct']:.2f}% | {match['max_drawdown_pct']:.2f}% | {match['avg_exposure_pct']:.2f}% |"
                )

    lines.extend(
        [
            "",
            "## 5. 滚动窗口胜率汇总（Rolling 1Y / 3Y）",
            "",
            "| 窗口 | 方案 | 样本数 | 年化中位数 | 最低年化 | 最高年化 | 正收益占比 | 跑赢SPY占比 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for label, attr in (("1Y (252日)", "rolling_1y"), ("3Y (756日)", "rolling_3y")):
        for result in lump_results:
            roll = getattr(result, attr)
            count = roll.get("sample_count", 0)
            beat = roll.get("beat_benchmark_rate_pct")
            beat_text = f"{beat:.2f}%" if count > 0 and beat is not None else "-"
            if count == 0:
                lines.append(f"| {label} | {result.strategy_name} | 0 | - | - | - | - | - |")
            else:
                lines.append(
                    f"| {label} | {result.strategy_name} | {count} | {roll['median_cagr_pct']:.2f}% | "
                    f"{roll['min_cagr_pct']:.2f}% | {roll['max_cagr_pct']:.2f}% | "
                    f"{roll['positive_rate_pct']:.2f}% | {beat_text} |"
                )

    lines.extend(
        [
            "",
            "## 6. 规则说明",
            "",
            "- SPY 买入持有：首次买入后不再交易（定投模式下按目标权重只买不卖）。",
            "- SPY 70% / QQQ 30% 季度再平衡：每季度首个交易日恢复目标权重。",
            f"- SPY 70% / QQQ 30% 阈值再平衡：任意交易日开盘最大权重偏离达到 {args.drift_threshold:.1%} 时触发再平衡。",
            "- 200 日趋势过滤（现金）：每月首个交易日检查前一日收盘；低于自身 200 日均线时，对应权重留作现金。",
            f"- 200 日缓冲趋势（{args.trend_buffer:.1%}）：引入滞回带宽，仅在跌破均线下方 {args.trend_buffer:.1%} 时转弱，站回均线上方 {args.trend_buffer:.1%} 时恢复，降低假突破换手。",
        ]
    )
    if active_defensive_symbol:
        lines.append(
            f"- 200 日缓冲趋势（避险 {active_defensive_symbol}）：趋势转弱时将退出权重轮换至 {active_defensive_symbol}，不再空仓吃 0 利息。"
        )
    lines.append(
        "- 原轮动方案：10 个股票/ETF 的 50/150 均线、移动止损和仓位风控，使用相同正式起点重新计算。"
    )

    lines.extend(["", "## 7. 动态结论", ""])
    for conclusion in conclusions:
        lines.append(f"- {conclusion}")

    lines.extend(
        [
            "",
            "## 8. 限制",
            "",
            "未计税费、汇率、未配置避险 ETF 时的现金利息及分数股；ETF 历史成分与产品存续带来偏差。",
            "",
        ]
    )
    return "\n".join(lines)


def _average_exposure(equity_curve: list[dict]) -> float:
    values = [
        point["gross_exposure"] / point["equity"]
        for point in equity_curve
        if point["equity"]
    ]
    return sum(values) / len(values) * 100 if values else 0.0


def build_study_rules(
    trend_window: int,
    drift_threshold: float,
    trend_buffer: float,
    defensive_symbol: str | None,
) -> list[PortfolioRule]:
    rules = [
        PortfolioRule("SPY 买入持有", {"SPY": 1.0}, "buy_and_hold"),
        PortfolioRule("SPY 70% / QQQ 30% 季度再平衡", {"SPY": 0.70, "QQQ": 0.30}, "quarterly"),
        PortfolioRule(
            f"SPY 70% / QQQ 30% 阈值再平衡({drift_threshold:.0%})",
            {"SPY": 0.70, "QQQ": 0.30},
            "drift",
            drift_threshold_pct=drift_threshold,
        ),
        PortfolioRule(
            "SPY 70% / QQQ 30% + 200日趋势过滤(现金)",
            {"SPY": 0.70, "QQQ": 0.30},
            "monthly",
            trend_window=trend_window,
        ),
        PortfolioRule(
            f"SPY 70% / QQQ 30% + 200日缓冲趋势({trend_buffer:.0%}, 现金)",
            {"SPY": 0.70, "QQQ": 0.30},
            "monthly",
            trend_window=trend_window,
            trend_buffer_pct=trend_buffer,
        ),
    ]
    if defensive_symbol is not None:
        rules.append(
            PortfolioRule(
                f"SPY 70% / QQQ 30% + 200日缓冲趋势(避险 {defensive_symbol})",
                {"SPY": 0.70, "QQQ": 0.30},
                "monthly",
                trend_window=trend_window,
                defensive_symbol=defensive_symbol,
                trend_buffer_pct=trend_buffer,
            )
        )
    return rules


def main() -> int:
    args = parse_args()
    if args.years <= 0:
        raise SystemExit("years must be positive")
    if args.initial_cash <= 0:
        raise SystemExit("initial_cash must be positive")
    if args.monthly_contribution < 0:
        raise SystemExit("monthly_contribution must be non-negative")
    if not (0.0 < args.drift_threshold < 1.0):
        raise SystemExit("drift_threshold must be between 0 and 1")
    if not (0.0 <= args.trend_buffer < 0.5):
        raise SystemExit("trend_buffer must be in [0.0, 0.5)")

    trend_window = 200
    if args.sync:
        lookback_days = args.years * 365 + 400
        sync_history_from_tws(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            history_dir=args.history_dir,
            symbols=INDEX_SYNC_SYMBOLS,
            lookback_days=lookback_days,
        )

    notes: list[str] = []
    active_defensive_symbol, defensive_note = resolve_defensive_symbol(
        args.history_dir, args.defensive_symbol
    )
    if defensive_note:
        notes.append(defensive_note)

    symbols_to_load = list(
        dict.fromkeys([*ALL_SYMBOLS, *([active_defensive_symbol] if active_defensive_symbol else [])])
    )
    bars_by_symbol = load_history(args.history_dir, symbols_to_load)

    latest_common_date = min(bars[-1].date for bars in bars_by_symbol.values())
    requested_start = latest_common_date - timedelta(days=round(args.years * 365.25))
    if any(len(bars) <= trend_window for bars in bars_by_symbol.values()):
        raise SystemExit("cached history is too short for 200-day trend warmup")
    earliest_tradable = max(bars[trend_window].date for bars in bars_by_symbol.values())
    evaluation_start = max(requested_start, earliest_tradable)

    actual_years = (latest_common_date - evaluation_start).days / 365.25
    if actual_years < args.years - 1.0:
        notes.append(
            f"当前本地缓存仅覆盖 {evaluation_start} 至 {latest_common_date}（约 {actual_years:.1f} 年，短于请求的 {args.years} 年）。"
            "请在 TWS 启动后执行 `conda run -n mlx312 python scripts/run_index_study.py --years 10 --sync` 获取完整历史。"
        )

    rules = build_study_rules(
        trend_window=trend_window,
        drift_threshold=args.drift_threshold,
        trend_buffer=args.trend_buffer,
        defensive_symbol=active_defensive_symbol,
    )

    lump_config = IndexBacktestConfig(initial_cash=args.initial_cash)
    spy_lump = run_index_backtest(
        bars_by_symbol,
        rule=rules[0],
        config=lump_config,
        start_date=evaluation_start,
    )
    lump_results = [
        run_index_backtest(
            bars_by_symbol,
            rule=rule,
            config=lump_config,
            start_date=evaluation_start,
            benchmark_curve=spy_lump.equity_curve,
        )
        for rule in rules
    ]

    dca_config = IndexBacktestConfig(
        initial_cash=args.initial_cash,
        periodic_contribution=args.monthly_contribution,
        contribution_frequency="monthly" if args.monthly_contribution > 0 else "none",
    )
    spy_dca = run_index_backtest(
        bars_by_symbol,
        rule=rules[0],
        config=dca_config,
        start_date=evaluation_start,
    )
    dca_results = [
        run_index_backtest(
            bars_by_symbol,
            rule=rule,
            config=dca_config,
            start_date=evaluation_start,
            benchmark_curve=spy_dca.equity_curve,
        )
        for rule in rules
    ]

    active_result = run_backtest(
        {symbol: bars_by_symbol[symbol] for symbol in ALL_SYMBOLS},
        config=BacktestConfig(
            initial_cash=args.initial_cash,
            risk_per_trade_pct=0.0075,
            max_total_exposure_pct=0.75,
            max_symbol_exposure_pct=0.25,
            min_cash_reserve_pct=0.15,
        ),
        strategy=SwingTrendStrategy(fast_window=50, slow_window=150, trailing_stop_pct=0.10),
        start_date=evaluation_start,
    )
    json_path, markdown_path = save_report(
        output_dir=args.output_dir,
        lump_results=lump_results,
        dca_results=dca_results,
        active_result=active_result,
        args=args,
        active_defensive_symbol=active_defensive_symbol,
        notes=notes,
    )

    for note in notes:
        print(f"NOTE: {note}")
    print("=== Lump-Sum ===")
    for result in lump_results:
        print(
            f"{result.strategy_name}: return {result.total_return_pct:.2f}%, "
            f"max drawdown {result.max_drawdown_pct:.2f}%, Sharpe {result.sharpe_ratio:.3f}"
        )
    print(
        f"原轮动方案: return {active_result.total_return_pct:.2f}%, "
        f"max drawdown {active_result.max_drawdown_pct:.2f}%, Sharpe {active_result.sharpe_ratio:.3f}"
    )
    print(f"=== Monthly DCA (+${args.monthly_contribution:,.0f}/mo) ===")
    for result in dca_results:
        print(
            f"{result.strategy_name}: contributed ${result.total_contributed:,.2f}, "
            f"final ${result.final_equity:,.2f}, TWR {result.twr_total_return_pct:.2f}%, "
            f"XIRR {result.mwr_xirr_pct:.2f}%, maxDD {result.max_drawdown_pct:.2f}%"
        )
    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
