"""Fetch Point-in-Time PEAD earnings events from SEC EDGAR (and optional unadjusted TRADES bars from TWS)."""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.ibkr.client import IBKRClient  # noqa: E402
from app.trading.historical_provider import IBKRHistoricalBarProvider  # noqa: E402
from app.trading.models import Bar  # noqa: E402
from app.trading.pead_edgar_loader import (  # noqa: E402
    fetch_pead_events_for_symbols,
    write_pead_events_csv,
)

DEFAULT_SYMBOLS = "AAPL,MSFT,NVDA,TSLA"
IGNORED_TWS_INFO_CODES = {2104, 2106, 2107, 2108, 2158}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly read-only SEC EDGAR Point-in-Time PEAD event collector "
            "(8-K Item 2.02 BMO/AMC timing + split-consistent XBRL YoY EPS/revenue)."
        )
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f"Comma-separated US stock tickers (default: {DEFAULT_SYMBOLS}).",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("SEC_EDGAR_USER_AGENT", ""),
        help="SEC EDGAR User-Agent (e.g. 'IBKRMonitor user@example.com').",
    )
    parser.add_argument(
        "--start-date",
        default="2016-01-01",
        help="Minimum announcement_date YYYY-MM-DD (default: 2016-01-01).",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=PROJECT_ROOT / "data" / "events" / "pead_events.csv",
        help="Output CSV path (default: data/events/pead_events.csv).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "events",
        help="Directory to cache SEC submissions/companyfacts JSON (default: data/events).",
    )
    parser.add_argument(
        "--sync-trades",
        action="store_true",
        help="Also fetch unadjusted TRADES daily bars (plus SPY) from read-only TWS.",
    )
    parser.add_argument(
        "--trades-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "history_trades",
        help="Directory to save unadjusted TRADES CSVs (default: data/history_trades).",
    )
    parser.add_argument("--years", type=int, default=10, help="Lookback years for --sync-trades (default: 10).")
    parser.add_argument("--host", default="127.0.0.1", help="TWS host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=7497, help="TWS port (default: 7497).")
    parser.add_argument("--client-id", type=int, default=80, help="TWS client ID (default: 80).")
    return parser.parse_args(argv)


async def _download_unadjusted_trades(
    client: IBKRClient,
    symbols: list[str],
    lookback_days: int,
) -> dict[str, list[Bar]]:
    provider = IBKRHistoricalBarProvider(
        client,
        timeout_seconds=90.0,
        what_to_show="TRADES",
    )
    bars_by_symbol: dict[str, list[Bar]] = {}
    for sym in symbols:
        bars = await provider.get_daily_bars(sym, lookback_days)
        if not bars:
            raise RuntimeError(f"no TRADES bars returned for {sym}")
        bars_by_symbol[sym] = bars
        print(f"TRADES {sym}: {len(bars)} bars ({bars[0].date} to {bars[-1].date})")
        await asyncio.sleep(0.5)
    return bars_by_symbol


def save_trades_history(trades_dir: Path, bars_by_symbol: dict[str, list[Bar]]) -> None:
    trades_dir.mkdir(parents=True, exist_ok=True)
    for sym, bars in bars_by_symbol.items():
        with (trades_dir / f"{sym}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for bar in bars:
                writer.writerow([bar.date.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume])


def sync_unadjusted_trades_from_tws(
    symbols: list[str],
    *,
    trades_dir: Path,
    years: int,
    host: str,
    port: int,
    client_id: int,
) -> None:
    sync_symbols = list(dict.fromkeys(["SPY", *symbols]))
    lookback_days = max(1, years) * 365 + 400
    client = IBKRClient()
    try:
        client.connect(host, port, client_id)
        threading.Thread(target=client.run, daemon=True).start()
        if not client.connected_event.wait(10.0):
            raise RuntimeError(f"TWS API handshake failed at {host}:{port}")
        bars_by_symbol = asyncio.run(_download_unadjusted_trades(client, sync_symbols, lookback_days))
        errors = client.drain_errors()
        blocking = [err for err in errors if err.get("errorCode") not in IGNORED_TWS_INFO_CODES]
        if blocking:
            raise RuntimeError(f"IBKR returned errors during TRADES sync: {blocking}")
    finally:
        client.disconnect()
    save_trades_history(trades_dir, bars_by_symbol)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(",") if s.strip()))
    if not symbols:
        raise SystemExit("At least one symbol is required.")
    start_dt = datetime.strptime(args.start_date.strip(), "%Y-%m-%d").date() if args.start_date.strip() else None

    events, skipped = fetch_pead_events_for_symbols(
        symbols,
        args.user_agent,
        start_date=start_dt,
        cache_dir=args.cache_dir,
    )
    if not events:
        raise SystemExit(f"No PEAD events extracted for {symbols} (skipped={skipped}).")

    csv_path = write_pead_events_csv(events, args.output_csv)
    print(f"Extracted {len(events)} Point-in-Time PEAD event(s) -> {csv_path}")
    if skipped:
        print(f"Skipped symbols (missing CIK or non-US-GAAP 10-Q/8-K): {', '.join(skipped)}")

    if args.sync_trades:
        sync_unadjusted_trades_from_tws(
            symbols,
            trades_dir=args.trades_dir,
            years=args.years,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
        )
        print(f"Saved unadjusted TRADES CSVs -> {args.trades_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
