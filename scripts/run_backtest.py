"""Download adjusted daily bars from TWS and run the read-only swing backtest."""

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
from app.trading.backtest import BacktestConfig, BacktestResult, run_backtest  # noqa: E402
from app.trading.historical_provider import IBKRHistoricalBarProvider  # noqa: E402
from app.trading.models import Bar  # noqa: E402
from app.trading.swing_strategy import SwingTrendStrategy  # noqa: E402


DEFAULT_SYMBOLS = "SPY,QQQ,VTI,IWM,DIA,NVDA,TSM,TSLA,AAPL,MSFT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a read-only stock and ETF swing backtest")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=77)
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--initial-cash", type=float, default=11_000.0)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "backtests")
    parser.add_argument("--use-cache", action="store_true", help="Use existing CSV files without TWS")
    return parser.parse_args()


async def download_bars(
    client: IBKRClient,
    symbols: list[str],
    lookback_days: int,
) -> dict[str, list[Bar]]:
    provider = IBKRHistoricalBarProvider(
        client,
        timeout_seconds=60.0,
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


def save_history(output_dir: Path, bars_by_symbol: dict[str, list[Bar]]) -> None:
    history_dir = output_dir.parent / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    for symbol, bars in bars_by_symbol.items():
        with (history_dir / f"{symbol}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for bar in bars:
                writer.writerow([bar.date.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume])


def load_history(output_dir: Path, symbols: list[str]) -> dict[str, list[Bar]]:
    history_dir = output_dir.parent / "history"
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


def save_report(
    output_dir: Path,
    result: BacktestResult,
    symbols: list[str],
    config: BacktestConfig,
    strategy: SwingTrendStrategy,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = result.to_dict()
    payload["symbols"] = symbols
    payload["config"] = {
        "initial_cash": config.initial_cash,
        "commission_per_order": config.commission_per_order,
        "slippage_bps": config.slippage_bps,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "max_total_exposure_pct": config.max_total_exposure_pct,
        "max_symbol_exposure_pct": config.max_symbol_exposure_pct,
        "min_cash_reserve_pct": config.min_cash_reserve_pct,
        "max_positions": config.max_positions,
    }
    payload["strategy"] = {
        "fast_window": strategy.fast_window,
        "slow_window": strategy.slow_window,
        "atr_window": strategy.atr_window,
        "trailing_stop_pct": strategy.trailing_stop_pct,
        "atr_stop_multiple": strategy.atr_stop_multiple,
    }

    json_path = output_dir / f"backtest-{timestamp}.json"
    latest_json = output_dir / "latest.json"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(encoded, encoding="utf-8")
    latest_json.write_text(encoded, encoding="utf-8")

    markdown = _markdown_report(result, symbols, config, strategy)
    markdown_path = output_dir / f"backtest-{timestamp}.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    (output_dir / "latest.md").write_text(markdown, encoding="utf-8")
    return json_path, markdown_path


def _markdown_report(
    result: BacktestResult,
    symbols: list[str],
    config: BacktestConfig,
    strategy: SwingTrendStrategy,
) -> str:
    lines = [
        "# 股票与 ETF 趋势策略回测",
        "",
        f"- 区间：{result.start_date} 至 {result.end_date}",
        f"- 标的：{', '.join(symbols)}",
        f"- 初始资金：${config.initial_cash:,.2f}",
        f"- 规则：{strategy.fast_window}/{strategy.slow_window} 日均线趋势过滤，收盘出信号，"
        f"下一交易日开盘成交，{strategy.trailing_stop_pct:.0%} 移动止损",
        f"- 风控：单笔风险 {config.risk_per_trade_pct:.2%}，总敞口 {config.max_total_exposure_pct:.0%}，"
        f"单标的 {config.max_symbol_exposure_pct:.0%}，现金保留 {config.min_cash_reserve_pct:.0%}",
        f"- 成本：每笔委托 ${config.commission_per_order:.2f}，滑点 {config.slippage_bps:.1f} bps",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 期末权益 | ${result.final_equity:,.2f} |",
        f"| 总收益 | {result.total_return_pct:.2f}% |",
        f"| 年化收益 | {result.cagr_pct:.2f}% |",
        f"| 最大回撤 | {result.max_drawdown_pct:.2f}% |",
        f"| 年化波动率 | {result.annualized_volatility_pct:.2f}% |",
        f"| Sharpe | {result.sharpe_ratio:.3f} |",
        f"| SPY 同期买入持有 | {result.benchmark_total_return_pct:.2f}% |",
        f"| SPY 年化收益 | {result.benchmark_cagr_pct:.2f}% |",
        f"| SPY 最大回撤 | {result.benchmark_max_drawdown_pct:.2f}% |",
        f"| 已平仓交易 | {result.closed_trades} |",
        f"| 胜率 | {result.win_rate_pct:.2f}% |",
        "",
        "## 已平仓交易",
        "",
        "| 标的 | 买入日 | 卖出日 | 数量 | 买入价 | 卖出价 | 盈亏 | 收益率 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for trade in result.trades:
        lines.append(
            f"| {trade.symbol} | {trade.entry_date} | {trade.exit_date} | {trade.quantity} | "
            f"${trade.entry_price:.2f} | ${trade.exit_price:.2f} | ${trade.pnl:.2f} | {trade.return_pct:.2f}% |"
        )
    if not result.trades:
        lines.append("| - | - | - | 0 | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "行情使用 IBKR ADJUSTED_LAST 日线。结果包含静态观察池带来的幸存者偏差，不能代表未来收益。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()))
    if not symbols:
        raise SystemExit("at least one symbol is required")
    if args.years <= 0:
        raise SystemExit("years must be positive")

    config = BacktestConfig(
        initial_cash=args.initial_cash,
        risk_per_trade_pct=0.0075,
        max_total_exposure_pct=0.75,
        max_symbol_exposure_pct=0.25,
        min_cash_reserve_pct=0.15,
    )
    strategy = SwingTrendStrategy(
        fast_window=50,
        slow_window=150,
        trailing_stop_pct=0.10,
    )
    warmup_calendar_days = strategy.slow_window * 2

    if args.use_cache:
        bars_by_symbol = load_history(args.output_dir, symbols)
    else:
        client = IBKRClient()
        try:
            client.connect(args.host, args.port, args.client_id)
            threading.Thread(target=client.run, daemon=True).start()
            if not client.connected_event.wait(10):
                raise RuntimeError(f"TWS API handshake failed at {args.host}:{args.port}")
            bars_by_symbol = asyncio.run(
                download_bars(client, symbols, args.years * 365 + warmup_calendar_days)
            )
            errors = client.drain_errors()
            blocking_errors = [
                error for error in errors if error.get("errorCode") not in {2104, 2106, 2107, 2108, 2158}
            ]
            if blocking_errors:
                raise RuntimeError(f"IBKR returned errors: {blocking_errors}")
        finally:
            client.disconnect()

    latest_common_date = min(bars[-1].date for bars in bars_by_symbol.values())
    requested_start = latest_common_date - timedelta(days=round(args.years * 365.25))
    if any(len(bars) < strategy.slow_window for bars in bars_by_symbol.values()):
        raise RuntimeError("cached history is too short for the strategy warmup")
    earliest_tradable = max(bars[strategy.slow_window - 1].date for bars in bars_by_symbol.values())
    evaluation_start = max(requested_start, earliest_tradable)
    result = run_backtest(
        bars_by_symbol,
        config=config,
        strategy=strategy,
        start_date=evaluation_start,
    )
    if not args.use_cache:
        save_history(args.output_dir, bars_by_symbol)
    json_path, markdown_path = save_report(args.output_dir, result, symbols, config, strategy)

    print(f"Final equity: ${result.final_equity:,.2f}")
    print(f"Total return: {result.total_return_pct:.2f}%")
    print(f"Max drawdown: {result.max_drawdown_pct:.2f}%")
    print(f"SPY benchmark: {result.benchmark_total_return_pct:.2f}%")
    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
