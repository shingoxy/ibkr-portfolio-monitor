"""Run an offline, zero-lookahead PEAD backtest study with 10/20/30 bps slippage stress testing."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.trading.models import Bar  # noqa: E402
from app.trading.pead_backtest import (  # noqa: E402
    DEFAULT_UNIVERSE_NOTE,
    PEADBacktestConfig,
    PEADBacktestResult,
    PEADSlippageMatrixResult,
    run_pead_slippage_matrix,
)
from app.trading.pead_strategy import EarningsEvent, EarningsSession, PEADConfig  # noqa: E402

REQUIRED_EVENT_COLUMNS = (
    "symbol",
    "announcement_date",
    "session",
    "eps_actual",
    "eps_estimate",
)
EVENT_CSV_SCHEMA = (
    "symbol,announcement_date,session,eps_actual,eps_estimate,"
    "revenue_actual,revenue_estimate,source"
)
EVENT_CSV_EXAMPLE = (
    "AAPL,2024-05-02,AMC,1.53,1.50,90753000000,90010000000,SEC_EDGAR_8K"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a strictly read-only, T+1 SettledCash-constrained PEAD backtest study "
            f"across 10/20/30 bps slippage. Required events CSV schema: {EVENT_CSV_SCHEMA}"
        )
    )
    parser.add_argument(
        "--events-csv",
        type=Path,
        default=None,
        help=(
            "Path to point-in-time earnings events CSV with header: "
            f"{EVENT_CSV_SCHEMA}"
        ),
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "history",
        help="Directory of adjusted daily CSV files ({SYMBOL}.csv).",
    )
    parser.add_argument(
        "--trades-dir",
        type=Path,
        default=None,
        help="Optional directory of unadjusted TRADES daily CSV files ({SYMBOL}.csv).",
    )
    parser.add_argument(
        "--holdout-start",
        type=str,
        default=None,
        help="Optional locked Holdout start date (YYYY-MM-DD) by trade entry date.",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=11_000.0,
        help="Starting account equity in USD (default: 11000.0).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "backtests",
        help="Directory for pead-study-latest.{json,md} and timestamped reports.",
    )
    parser.add_argument(
        "--universe-note",
        type=str,
        default=DEFAULT_UNIVERSE_NOTE,
        help="Explicit universe provenance and survivorship bias disclosure note.",
    )
    parser.add_argument(
        "--slippage-grid",
        type=str,
        default="10,20,30",
        help="Comma-separated one-way slippage levels in bps (default: 10,20,30).",
    )
    parser.add_argument("--min-eps-surprise", type=float, default=0.05)
    parser.add_argument("--min-revenue-surprise", type=float, default=0.0)
    parser.add_argument("--min-gap", type=float, default=0.02)
    parser.add_argument("--max-gap", type=float, default=0.25)
    parser.add_argument("--min-avg-dollar-volume", type=float, default=10_000_000.0)
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--spy-regime-window", type=int, default=200)
    parser.add_argument("--holding-days", type=int, default=10)
    parser.add_argument("--stop-loss", type=float, default=0.08)
    parser.add_argument("--volume-multiplier", type=float, default=1.0)
    parser.add_argument(
        "--no-t-plus-1",
        action="store_true",
        help="Disable T+1 SettledCash constraint (for sensitivity comparison only).",
    )
    return parser.parse_args(argv)


def _events_csv_help_message(reason: str) -> str:
    return (
        f"Error: {reason}\n\n"
        "Please provide --events-csv pointing to a UTF-8 CSV file with schema:\n"
        f"  {EVENT_CSV_SCHEMA}\n\n"
        "Column details:\n"
        "  - symbol: Equity ticker (e.g., AAPL)\n"
        "  - announcement_date: Official release date in YYYY-MM-DD\n"
        "  - session: BMO (before market open) or AMC (after market close)\n"
        "  - eps_actual: Reported EPS (float)\n"
        "  - eps_estimate: Consensus EPS estimate prior to release (float)\n"
        "  - revenue_actual: Optional reported revenue (float or blank)\n"
        "  - revenue_estimate: Optional consensus revenue (float or blank)\n"
        "  - source: Provenance label (e.g., SEC_EDGAR_8K)\n\n"
        "Example:\n"
        f"  {EVENT_CSV_SCHEMA}\n"
        f"  {EVENT_CSV_EXAMPLE}\n"
    )


def _parse_optional_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return float(cleaned)


def load_events_csv(path: Path) -> list[EarningsEvent]:
    if not path.exists():
        raise FileNotFoundError(_events_csv_help_message(f"events CSV file not found: {path}"))
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing_cols = [col for col in REQUIRED_EVENT_COLUMNS if col not in fieldnames]
        if missing_cols:
            raise ValueError(
                _events_csv_help_message(
                    f"missing required CSV columns {missing_cols} in {path}; found {fieldnames}"
                )
            )
        events: list[EarningsEvent] = []
        for row_num, row in enumerate(reader, start=2):
            try:
                events.append(
                    EarningsEvent(
                        symbol=row["symbol"],
                        announcement_date=datetime.strptime(
                            row["announcement_date"].strip(), "%Y-%m-%d"
                        ).date(),
                        session=EarningsSession(row["session"].strip().upper()),
                        eps_actual=float(row["eps_actual"]),
                        eps_estimate=float(row["eps_estimate"]),
                        revenue_actual=_parse_optional_float(row.get("revenue_actual")),
                        revenue_estimate=_parse_optional_float(row.get("revenue_estimate")),
                        source=(row.get("source") or "").strip(),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    _events_csv_help_message(f"invalid row {row_num} in {path}: {exc}")
                ) from exc
    if not events:
        raise ValueError(_events_csv_help_message(f"no earnings events found in {path}"))
    return events


def load_bars_csv(path: Path, symbol: str) -> list[Bar]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            Bar(
                symbol=symbol,
                date=datetime.strptime(row["date"].strip(), "%Y-%m-%d").date(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(float(row["volume"])),
            )
            for row in csv.DictReader(handle)
        ]


def load_history_directory(
    directory: Path, symbols: Sequence[str], required: bool = True
) -> tuple[dict[str, list[Bar]], list[str]]:
    if not directory.exists():
        if required:
            raise RuntimeError(f"history directory does not exist: {directory}")
        return {}, list(symbols)
    bars_by_symbol: dict[str, list[Bar]] = {}
    missing_symbols: list[str] = []
    target_symbols = list(dict.fromkeys([*symbols, "SPY"]))
    for symbol in target_symbols:
        path = directory / f"{symbol}.csv"
        if path.exists():
            bars_by_symbol[symbol] = load_bars_csv(path, symbol)
        elif symbol in symbols:
            missing_symbols.append(symbol)
    if required and not any(symbol in bars_by_symbol for symbol in symbols):
        raise RuntimeError(
            f"no cached history CSV files found in {directory} for event symbols: "
            f"{', '.join(symbols)}"
        )
    return bars_by_symbol, missing_symbols


def save_pead_report(
    output_dir: Path,
    matrix: PEADSlippageMatrixResult,
    strategy_config: PEADConfig,
    backtest_config: PEADBacktestConfig,
    events_csv: Path,
    missing_history_symbols: list[str],
    holdout_start: date | None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "events_csv": str(events_csv),
        "holdout_start_date": holdout_start.isoformat() if holdout_start else None,
        "missing_history_symbols": missing_history_symbols,
        "strategy_config": {
            "min_eps_surprise_pct": strategy_config.min_eps_surprise_pct,
            "min_revenue_surprise_pct": strategy_config.min_revenue_surprise_pct,
            "min_gap_pct": strategy_config.min_gap_pct,
            "max_gap_pct": strategy_config.max_gap_pct,
            "min_avg_dollar_volume": strategy_config.min_avg_dollar_volume,
            "min_price": strategy_config.min_price,
            "spy_regime_window": strategy_config.spy_regime_window,
            "holding_days": strategy_config.holding_days,
            "stop_loss_pct": strategy_config.stop_loss_pct,
            "dollar_volume_window": strategy_config.dollar_volume_window,
            "volume_multiplier": strategy_config.volume_multiplier,
        },
        "backtest_config": {
            "initial_cash": backtest_config.initial_cash,
            "commission_per_order": backtest_config.commission_per_order,
            "sec_fee_rate": backtest_config.sec_fee_rate,
            "finra_taf_per_share": backtest_config.finra_taf_per_share,
            "finra_taf_max_per_order": backtest_config.finra_taf_max_per_order,
            "max_positions": backtest_config.max_positions,
            "max_symbol_exposure_pct": backtest_config.max_symbol_exposure_pct,
            "max_total_exposure_pct": backtest_config.max_total_exposure_pct,
            "min_cash_reserve_pct": backtest_config.min_cash_reserve_pct,
            "enforce_t_plus_1_settled_cash": backtest_config.enforce_t_plus_1_settled_cash,
            "universe_note": backtest_config.universe_note,
        },
        "slippage_matrix": matrix.to_dict(),
    }

    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path = output_dir / f"pead-study-{timestamp}.json"
    json_path.write_text(encoded, encoding="utf-8")
    (output_dir / "pead-study-latest.json").write_text(encoded, encoding="utf-8")

    markdown = _markdown_report(
        matrix=matrix,
        strategy_config=strategy_config,
        backtest_config=backtest_config,
        events_csv=events_csv,
        missing_history_symbols=missing_history_symbols,
        holdout_start=holdout_start,
    )
    markdown_path = output_dir / f"pead-study-{timestamp}.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    (output_dir / "pead-study-latest.md").write_text(markdown, encoding="utf-8")
    return json_path, markdown_path


def _markdown_report(
    matrix: PEADSlippageMatrixResult,
    strategy_config: PEADConfig,
    backtest_config: PEADBacktestConfig,
    events_csv: Path,
    missing_history_symbols: list[str],
    holdout_start: date | None,
) -> str:
    baseline_run = matrix.runs[1] if len(matrix.runs) > 1 else matrix.runs[0]
    for run in matrix.runs:
        if abs(run.slippage_bps - 20.0) < 1e-9:
            baseline_run = run
            break
    base: PEADBacktestResult = baseline_run.result

    lines = [
        "# Track 1: 纯多头 PEAD 财报漂移回测研究报告",
        "",
        f"- 区间：{base.start_date} 至 {base.end_date}",
        f"- 事件文件：`{events_csv}`（评估事件 {base.total_events_evaluated} 条，通过初筛 {base.accepted_candidates_count} 条，拒绝 {base.rejected_candidates_count} 条）",
        f"- Holdout 切分：{holdout_start.isoformat() if holdout_start else '未启用（全样本）'}",
        f"- 初始资金：${backtest_config.initial_cash:,.2f}",
        f"- 规则：EPS 惊喜 >= {strategy_config.min_eps_surprise_pct:.1%}，跳空 [{strategy_config.min_gap_pct:.1%}, {strategy_config.max_gap_pct:.1%}]，价格 >= ${strategy_config.min_price:.2f}，20 日均成交额 >= ${strategy_config.min_avg_dollar_volume:,.0f}，SPY {strategy_config.spy_regime_window} 日 Regime 过滤，持有 {strategy_config.holding_days} 个交易日，止损 {strategy_config.stop_loss_pct:.1%}",
        f"- 现金账户 T+1 交收约束：{'启用（仅可用 SettledCash 开新仓）' if backtest_config.enforce_t_plus_1_settled_cash else '关闭'}（因未交收资金拦截开仓 {base.blocked_by_unsettled_cash_count} 次）",
        f"- 费率模型：佣金 ${backtest_config.commission_per_order:.2f}/笔 + 卖出 SEC {backtest_config.sec_fee_rate:.7f} + 卖出 FINRA TAF ${backtest_config.finra_taf_per_share:.6f}/股（封顶 ${backtest_config.finra_taf_max_per_order:.2f}）",
        "",
        "## 滑点压力梯度矩阵（10 / 20 / 30 bps）",
        "",
        "| 单边滑点 (bps) | 期末权益 | 总收益 | 年化 | 最大回撤 | Sharpe | SPY 总收益 | SPY 最大回撤 | 交易数 | 胜率 | 剔除Top3净盈亏 | 剔除Top3收益 | t-stat | 淘汰判定 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for run in matrix.runs:
        res = run.result
        kc = run.kill_criteria
        lines.append(
            f"| {run.slippage_bps:.1f} | ${res.final_equity:,.2f} | {res.total_return_pct:.2f}% | "
            f"{res.cagr_pct:.2f}% | {res.max_drawdown_pct:.2f}% | {res.sharpe_ratio:.3f} | "
            f"{res.benchmark_total_return_pct:.2f}% | {res.benchmark_max_drawdown_pct:.2f}% | "
            f"{res.closed_trades} | {res.win_rate_pct:.2f}% | ${res.pnl_excluding_top3:,.2f} | "
            f"{res.return_pct_excluding_top3:.2f}% | {res.trade_t_statistic:.3f} | {kc.status} |"
        )

    if base.dev_metrics is not None and base.holdout_metrics is not None:
        lines.extend(
            [
                "",
                f"## 开发集 vs 锁定 Holdout（基准滑点 {baseline_run.slippage_bps:.1f} bps，按入场日切分）",
                "",
                "| 样本切分 | 区间 | 总收益 | 最大回撤 | Sharpe | SPY 总收益 | SPY 最大回撤 | 交易数 | 胜率 | 剔除Top3净盈亏 | t-stat |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for label, m in [("Dev", base.dev_metrics), ("Holdout", base.holdout_metrics)]:
            span = f"{m.start_date}..{m.end_date}" if m.start_date and m.end_date else "-"
            lines.append(
                f"| {label} | {span} | {m.total_return_pct:.2f}% | {m.max_drawdown_pct:.2f}% | "
                f"{m.sharpe_ratio:.3f} | {m.benchmark_total_return_pct:.2f}% | "
                f"{m.benchmark_max_drawdown_pct:.2f}% | {m.closed_trades} | {m.win_rate_pct:.2f}% | "
                f"${m.pnl_excluding_top3:,.2f} | {m.trade_t_statistic:.3f} |"
            )

    kc_base = baseline_run.kill_criteria
    lines.extend(
        [
            "",
            f"## Kill Criteria 判定（{baseline_run.slippage_bps:.1f} bps / 评估范围：{kc_base.evaluated_scope}）",
            "",
            f"- 结论：**{kc_base.status}**",
        ]
    )
    if kc_base.reasons:
        for reason in kc_base.reasons:
            lines.append(f"- 触发原因：{reason}")
    else:
        lines.append("- 未触发 Section 5 淘汰红线。")

    audit = base.price_discrepancy_audit
    lines.extend(
        [
            "",
            "## 未复权 TRADES vs ADJUSTED_LAST 价格交叉核验",
            "",
            f"- 是否提供未复权 TRADES 数据：{audit['trades_bars_provided']}",
            f"- 核验标的数：{len(audit['compared_symbols'])}，核验柱线数：{audit['compared_bar_count']}",
            f"- 开盘/收盘最大偏差：{audit['max_open_diff_pct']:.4f}% / {audit['max_close_diff_pct']:.4f}%，决策变化事件数：{audit['decision_changed_count']}",
        ]
    )
    for warning in audit.get("warnings", []):
        lines.append(f"- 注意：{warning}")

    if base.rejection_reason_counts:
        lines.extend(["", "## 候选拦截原因分布", ""])
        for reason, count in base.rejection_reason_counts.items():
            lines.append(f"- `{reason}`: {count}")

    if missing_history_symbols:
        lines.extend(
            [
                "",
                "## 缺失日线历史文件标的",
                "",
                f"- 以下事件标的无对应历史 CSV：{', '.join(missing_history_symbols)}",
            ]
        )

    lines.extend(
        [
            "",
            "## 幸存者偏差与方法论披露",
            "",
            f"- {base.survivorship_bias_disclosure}",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.events_csv is None:
        print(_events_csv_help_message("missing required argument --events-csv"), file=sys.stderr)
        return 2

    try:
        events = load_events_csv(args.events_csv)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    holdout_start: date | None = None
    if args.holdout_start:
        try:
            holdout_start = datetime.strptime(args.holdout_start.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            print(f"Error: invalid --holdout-start '{args.holdout_start}' (expected YYYY-MM-DD)", file=sys.stderr)
            return 2

    try:
        slippage_grid = [float(item.strip()) for item in args.slippage_grid.split(",") if item.strip()]
    except ValueError:
        print(f"Error: invalid --slippage-grid '{args.slippage_grid}'", file=sys.stderr)
        return 2

    event_symbols = list(dict.fromkeys(event.symbol for event in events))
    bars_by_symbol, missing_history_symbols = load_history_directory(
        args.history_dir, event_symbols, required=True
    )
    trades_bars_by_symbol: dict[str, list[Bar]] | None = None
    if args.trades_dir is not None:
        if not args.trades_dir.exists():
            print(f"Error: trades directory not found: {args.trades_dir}", file=sys.stderr)
            return 2
        loaded_trades, _ = load_history_directory(args.trades_dir, event_symbols, required=False)
        trades_bars_by_symbol = loaded_trades or None

    strategy_config = PEADConfig(
        min_eps_surprise_pct=args.min_eps_surprise,
        min_revenue_surprise_pct=args.min_revenue_surprise,
        min_gap_pct=args.min_gap,
        max_gap_pct=args.max_gap,
        min_avg_dollar_volume=args.min_avg_dollar_volume,
        min_price=args.min_price,
        spy_regime_window=args.spy_regime_window,
        holding_days=args.holding_days,
        stop_loss_pct=args.stop_loss,
        volume_multiplier=args.volume_multiplier,
    )
    backtest_config = PEADBacktestConfig(
        initial_cash=args.initial_cash,
        enforce_t_plus_1_settled_cash=not args.no_t_plus_1,
        universe_note=args.universe_note,
    )

    matrix = run_pead_slippage_matrix(
        events=events,
        bars_by_symbol=bars_by_symbol,
        strategy_config=strategy_config,
        backtest_config=backtest_config,
        trades_bars_by_symbol=trades_bars_by_symbol,
        holdout_start_date=holdout_start,
        slippage_grid=slippage_grid,
    )
    json_path, markdown_path = save_pead_report(
        output_dir=args.output_dir,
        matrix=matrix,
        strategy_config=strategy_config,
        backtest_config=backtest_config,
        events_csv=args.events_csv,
        missing_history_symbols=missing_history_symbols,
        holdout_start=holdout_start,
    )

    for run in matrix.runs:
        res = run.result
        print(
            f"[{run.slippage_bps:.1f} bps] final=${res.final_equity:,.2f} "
            f"ret={res.total_return_pct:.2f}% sharpe={res.sharpe_ratio:.3f} "
            f"trades={res.closed_trades} top3_ex_pnl=${res.pnl_excluding_top3:,.2f} "
            f"t={res.trade_t_statistic:.3f} status={run.kill_criteria.status}"
        )
    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
