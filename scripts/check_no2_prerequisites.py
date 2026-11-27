"""Phase 0 read-only diagnostic for No.2 US equity prerequisites.

Strictly read-only: checks `.env` safety guards, queries TWS account summary,
tests `market_data_type=1` real-time quotes vs delayed/unsubscribed notices,
and validates 5-minute historical intraday bar retrieval.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ibkr.client import ACCOUNT_TAGS, IBKRClient  # noqa: E402
from app.ibkr.contracts import make_stock_contract  # noqa: E402
from app.ibkr.errors import INFO_CODES  # noqa: E402
from app.ibkr.request_ids import RequestIdManager  # noqa: E402
from app.logging_config import mask_account, setup_logging  # noqa: E402

MARKET_DATA_ISSUE_CODES = frozenset({354, 10089, 10167, 10168, 10189})
IGNORED_INFO_CODES = frozenset(INFO_CODES.keys()) | {2107, 2108, -1}
ALLOWED_APP_HOSTS = frozenset({"127.0.0.1", "localhost"})
PAPER_PORTS = frozenset({7497, 4002})
LIVE_PORTS = frozenset({7496, 4001})

_UNMASKED_ACCOUNT_RE = re.compile(r"\b([UD][U]?)(\d{2,})\b(?!\*)")


def _sanitize_text(text: str, extra_raw_accounts: Sequence[str] = ()) -> str:
    """Mask any IBKR account IDs that might appear in free-form strings (idempotent)."""
    cleaned = str(text)
    for acct in extra_raw_accounts:
        raw = str(acct or "").strip()
        if raw:
            cleaned = cleaned.replace(raw, mask_account(raw))
    return _UNMASKED_ACCOUNT_RE.sub(lambda m: mask_account(m.group(0)), cleaned)


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    num = _parse_float(value)
    if num is None:
        return None
    return int(round(num))


def _parse_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        values[key] = val
    return values


def check_env_read_only(env_path: Path) -> dict[str, Any]:
    """Validate `.env` presence, read-only guard, localhost binding, and port."""
    path = Path(env_path)
    issues: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        issues.append(f".env file not found at {path}; run `cp .env.example .env` before live TWS probing.")
        return {
            "env_path": str(path),
            "exists": False,
            "ok": False,
            "read_only": False,
            "read_only_ok": False,
            "read_only_raw": None,
            "app_host": None,
            "app_host_ok": False,
            "ibkr_host": None,
            "ibkr_port": None,
            "ibkr_port_ok": False,
            "port_mode": "Unknown",
            "ibkr_account_masked": "",
            "market_data_type": None,
            "issues": issues,
            "warnings": warnings,
        }

    raw = _parse_env_file(path)

    read_only_raw = raw.get("READ_ONLY")
    read_only_ok = read_only_raw is not None and read_only_raw.strip().lower() == "true"
    if not read_only_ok:
        issues.append("READ_ONLY must be explicitly set to `true` in .env.")

    app_host = raw.get("APP_HOST")
    app_host_ok = app_host in ALLOWED_APP_HOSTS
    if not app_host_ok:
        issues.append(f"APP_HOST must be `127.0.0.1` or `localhost` (got {app_host!r}).")

    ibkr_host = raw.get("IBKR_HOST", "").strip() or None
    if ibkr_host and ibkr_host not in ALLOWED_APP_HOSTS:
        warnings.append(f"IBKR_HOST={ibkr_host!r} is non-local; verify network boundary.")

    port_raw = raw.get("IBKR_PORT", "").strip()
    ibkr_port: int | None = None
    ibkr_port_ok = False
    if port_raw:
        try:
            parsed_port = int(port_raw)
            if 1 <= parsed_port <= 65535:
                ibkr_port = parsed_port
                ibkr_port_ok = True
            else:
                issues.append(f"IBKR_PORT out of valid TCP range 1..65535: {parsed_port}.")
        except ValueError:
            issues.append(f"IBKR_PORT is not a valid integer: {port_raw!r}.")
    else:
        issues.append("IBKR_PORT is missing or empty in .env.")

    if ibkr_port in PAPER_PORTS:
        port_mode = "Paper"
    elif ibkr_port in LIVE_PORTS:
        port_mode = "Live"
        warnings.append(
            f"IBKR_PORT={ibkr_port} targets a Live port; keep TWS Global Configuration -> API -> Read-Only API enabled."
        )
    elif ibkr_port is not None:
        port_mode = "Custom"
    else:
        port_mode = "Unknown"

    raw_account = raw.get("IBKR_ACCOUNT", "").strip()
    masked_account = mask_account(raw_account) if raw_account else ""

    mdt_raw = raw.get("MARKET_DATA_TYPE", "").strip()
    mdt_val = _parse_int(mdt_raw) if mdt_raw else None

    return {
        "env_path": str(path),
        "exists": True,
        "ok": read_only_ok and app_host_ok and ibkr_port_ok,
        "read_only": read_only_ok,
        "read_only_ok": read_only_ok,
        "read_only_raw": read_only_raw,
        "app_host": app_host,
        "app_host_ok": app_host_ok,
        "ibkr_host": ibkr_host,
        "ibkr_port": ibkr_port,
        "ibkr_port_ok": ibkr_port_ok,
        "port_mode": port_mode,
        "ibkr_account_masked": masked_account,
        "market_data_type": mdt_val,
        "issues": issues,
        "warnings": warnings,
    }


def _pick_summary_item(
    summary_items: Sequence[dict[str, Any]],
    tag: str,
    preferred_account: str = "",
) -> dict[str, Any] | None:
    matches = [item for item in summary_items if item.get("tag") == tag]
    if not matches:
        return None
    if preferred_account:
        acct_matches = [item for item in matches if item.get("account") == preferred_account]
        if acct_matches:
            matches = acct_matches
    for preferred_ccy in ("USD", "BASE", ""):
        for item in matches:
            if str(item.get("currency") or "").upper() == preferred_ccy:
                return item
    return matches[0]


def summarize_account_constraints(
    summary_items: Sequence[dict[str, Any]],
    accounts: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Derive Cash/Margin, T+1 settlement, PDT, and Paper flags with masked account IDs."""
    raw_accounts: list[str] = []
    for acct in accounts or ():
        clean = str(acct or "").strip()
        if clean and clean.upper() != "ALL" and clean not in raw_accounts:
            raw_accounts.append(clean)
    for item in summary_items:
        clean = str(item.get("account") or "").strip()
        if clean and clean.upper() != "ALL" and clean not in raw_accounts:
            raw_accounts.append(clean)

    accounts_masked = [mask_account(acct) for acct in raw_accounts]
    primary_raw = raw_accounts[0] if raw_accounts else ""
    primary_masked = accounts_masked[0] if accounts_masked else "****"
    is_paper = any(acct.upper().startswith("DU") for acct in raw_accounts)

    acct_type_item = _pick_summary_item(summary_items, "AccountType", primary_raw)
    net_liq_item = _pick_summary_item(summary_items, "NetLiquidation", primary_raw)
    settled_item = _pick_summary_item(summary_items, "SettledCash", primary_raw)
    avail_item = _pick_summary_item(summary_items, "AvailableFunds", primary_raw)
    bp_item = _pick_summary_item(summary_items, "BuyingPower", primary_raw)
    dtr_item = _pick_summary_item(summary_items, "DayTradesRemaining", primary_raw)

    account_type = str(acct_type_item.get("value") or "").strip() if acct_type_item else ""
    account_type_upper = account_type.upper()
    net_liquidation = _parse_float(net_liq_item.get("value")) if net_liq_item else None
    settled_cash = _parse_float(settled_item.get("value")) if settled_item else None
    available_funds = _parse_float(avail_item.get("value")) if avail_item else None
    buying_power = _parse_float(bp_item.get("value")) if bp_item else None
    day_trades_remaining = _parse_int(dtr_item.get("value")) if dtr_item else None

    currency = ""
    for item in (net_liq_item, settled_item, avail_item):
        if item and item.get("currency"):
            currency = str(item["currency"]).strip().upper()
            break

    notes: list[str] = []
    if "CASH" in account_type_upper:
        is_cash_account = True
        t_plus_1_cash_only = True
        pdt_restricted = False
        notes.append("AccountType indicates Cash account: long-only, no margin shorting, T+1 SettledCash enforced.")
    elif "MARGIN" in account_type_upper:
        is_cash_account = False
        t_plus_1_cash_only = False
        if day_trades_remaining is not None and day_trades_remaining >= 0:
            pdt_restricted = True
        elif net_liquidation is not None and net_liquidation < 25_000.0:
            pdt_restricted = True
        else:
            pdt_restricted = False
        if pdt_restricted:
            notes.append(
                f"Margin account under PDT constraint (NetLiquidation={net_liquidation}, DayTradesRemaining={day_trades_remaining})."
            )
        else:
            notes.append("Margin account without active PDT restriction.")
    else:
        if day_trades_remaining is not None and day_trades_remaining >= 0:
            is_cash_account = False
            t_plus_1_cash_only = False
            pdt_restricted = True
            notes.append(
                f"Non-explicit AccountType ({account_type or 'UNKNOWN'}) with DayTradesRemaining={day_trades_remaining}: classified as PDT-restricted Margin."
            )
        elif (
            buying_power is not None
            and net_liquidation is not None
            and net_liquidation > 0
            and buying_power > net_liquidation * 1.15
        ):
            is_cash_account = False
            t_plus_1_cash_only = False
            pdt_restricted = net_liquidation < 25_000.0
            notes.append(
                f"Non-explicit AccountType ({account_type or 'UNKNOWN'}) with leveraged BuyingPower: classified as Margin."
            )
        else:
            is_cash_account = True if account_type_upper else False
            t_plus_1_cash_only = True
            pdt_restricted = False
            notes.append(
                "AccountType did not confirm Margin leverage; defaulting conservatively to long-only T+1 SettledCash constraints."
            )

    return {
        "accounts_masked": accounts_masked,
        "primary_account_masked": primary_masked,
        "account_type": account_type,
        "currency": currency or "USD",
        "net_liquidation": net_liquidation,
        "settled_cash": settled_cash,
        "available_funds": available_funds,
        "buying_power": buying_power,
        "day_trades_remaining": day_trades_remaining,
        "is_paper": is_paper,
        "is_cash_account": is_cash_account,
        "t_plus_1_cash_only": t_plus_1_cash_only,
        "pdt_restricted": pdt_restricted,
        "has_account_summary": bool(summary_items),
        "notes": [_sanitize_text(note, raw_accounts) for note in notes],
    }


def summarize_market_data_probe(
    market_data_items: Sequence[dict[str, Any]] | dict[Any, dict[str, Any]],
    notices: Sequence[dict[str, Any]],
    errors: Sequence[dict[str, Any]],
    symbols: Sequence[str],
) -> dict[str, Any]:
    """Classify `market_data_type=1` probe results against IBKR entitlement notices/errors."""
    requested_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]

    items_list: list[dict[str, Any]]
    if isinstance(market_data_items, dict):
        items_list = [dict(v) for v in market_data_items.values() if isinstance(v, dict)]
    else:
        items_list = [dict(v) for v in market_data_items]

    req_id_to_symbol: dict[int, str] = {}
    items_by_symbol: dict[str, dict[str, Any]] = {}
    for item in items_list:
        symbol = str(item.get("symbol") or "").strip().upper()
        req_id = item.get("ticker_id") or item.get("reqId") or item.get("req_id")
        if symbol and req_id is not None:
            try:
                req_id_to_symbol[int(req_id)] = symbol
            except (TypeError, ValueError):
                pass
        if symbol:
            items_by_symbol[symbol] = item
            if symbol not in requested_symbols:
                requested_symbols.append(symbol)

    dedup_notices: list[dict[str, Any]] = []
    seen_notice_keys: set[tuple[int, int, str]] = set()
    blocking_errors: list[dict[str, Any]] = []

    for entry in list(notices) + list(errors):
        code = int(entry.get("errorCode", 0) or 0)
        req_id = int(entry.get("reqId", -1) or -1)
        msg = _sanitize_text(entry.get("errorString", ""))
        if code in IGNORED_INFO_CODES:
            continue
        normalized = {
            "reqId": req_id,
            "symbol": str(entry.get("symbol") or req_id_to_symbol.get(req_id, "")).upper(),
            "errorCode": code,
            "errorString": msg,
        }
        if code in MARKET_DATA_ISSUE_CODES:
            key = (req_id, code, msg)
            if key not in seen_notice_keys:
                seen_notice_keys.add(key)
                dedup_notices.append(normalized)
        else:
            blocking_errors.append(normalized)

    issue_codes = sorted({n["errorCode"] for n in dedup_notices})
    unsubscribed = bool(issue_codes)
    delayed_available_only = any(code in (354, 10167) for code in issue_codes) or any(
        code == 10089 and "delayed" in str(n.get("errorString", "")).lower()
        for n in dedup_notices
        for code in [n["errorCode"]]
    )

    per_symbol: dict[str, dict[str, Any]] = {}
    symbols_with_quotes: list[str] = []
    missing_symbols: list[str] = []

    for symbol in requested_symbols:
        item = items_by_symbol.get(symbol, {})
        bid = _parse_float(item.get("bid"))
        ask = _parse_float(item.get("ask"))
        last = _parse_float(item.get("last"))
        close = _parse_float(item.get("close"))

        has_bid_ask = (bid is not None and bid > 0) and (ask is not None and ask > 0)
        has_quote = any(val is not None and val > 0 for val in (bid, ask, last))
        sym_codes = sorted({
            n["errorCode"] for n in dedup_notices if n.get("symbol") == symbol
        })
        sym_errors = [e for e in blocking_errors if e.get("symbol") == symbol]

        if has_quote and not sym_codes and not sym_errors:
            symbols_with_quotes.append(symbol)
        else:
            missing_symbols.append(symbol)

        per_symbol[symbol] = {
            "bid": bid if (bid is not None and bid > 0) else None,
            "ask": ask if (ask is not None and ask > 0) else None,
            "last": last if (last is not None and last > 0) else None,
            "close": close if (close is not None and close > 0) else None,
            "has_quote": has_quote,
            "has_bid_ask": has_bid_ask,
            "issue_codes": sym_codes,
            "errors": sym_errors,
        }

    realtime_ok = (
        bool(requested_symbols)
        and len(symbols_with_quotes) == len(requested_symbols)
        and not issue_codes
        and not blocking_errors
    )

    if realtime_ok:
        status = "realtime_ok"
    elif 10168 in issue_codes and not delayed_available_only:
        status = "delayed_not_enabled"
    elif issue_codes:
        status = "delayed_only_or_unsubscribed"
    elif blocking_errors:
        status = "error"
    else:
        status = "no_realtime_ticks"

    return {
        "market_data_type": 1,
        "realtime_ok": realtime_ok,
        "status": status,
        "unsubscribed": unsubscribed,
        "delayed_available_only": delayed_available_only,
        "issue_codes": issue_codes,
        "symbols_requested": requested_symbols,
        "symbols_with_quotes": symbols_with_quotes,
        "missing_symbols": missing_symbols,
        "per_symbol": per_symbol,
        "notices": dedup_notices,
        "errors": blocking_errors,
    }


def _parse_bar_sort_key(timestamp: str) -> tuple[int, str]:
    raw = " ".join(timestamp.strip().split())
    try:
        return (0, datetime.fromisoformat(raw).isoformat())
    except ValueError:
        pass
    parts = raw.split()
    candidate = f"{parts[0]} {parts[1]}" if len(parts) >= 2 else raw
    for fmt in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d", "%Y-%m-%d"):
        try:
            return (0, datetime.strptime(candidate, fmt).isoformat())
        except ValueError:
            continue
    return (1, raw)


def summarize_intraday_probe(
    bars: Sequence[dict[str, Any]],
    elapsed_seconds: float,
    errors: Sequence[dict[str, Any]],
    symbol: str = "SPY",
) -> dict[str, Any]:
    """Validate `5 D` `"5 mins"` `TRADES` historical bar response and latency."""
    blocking_errors: list[dict[str, Any]] = []
    for err in errors:
        code = int(err.get("errorCode", 0) or 0)
        if code in IGNORED_INFO_CODES:
            continue
        blocking_errors.append({
            "reqId": int(err.get("reqId", -1) or -1),
            "errorCode": code,
            "errorString": _sanitize_text(err.get("errorString", "")),
        })

    timestamps = [str(bar.get("date") or bar.get("timestamp") or "").strip() for bar in bars]
    non_empty_ts = [ts for ts in timestamps if ts]
    if non_empty_ts:
        ordered = sorted(non_empty_ts, key=_parse_bar_sort_key)
        first_timestamp = ordered[0]
        last_timestamp = ordered[-1]
    else:
        first_timestamp = ""
        last_timestamp = ""

    bar_count = len(bars)
    elapsed = round(max(0.0, float(elapsed_seconds)), 3)
    intraday_ok = bar_count > 0 and not blocking_errors

    warnings: list[str] = []
    if bar_count == 0 and not blocking_errors:
        warnings.append("No 5-min bars returned for `5 D` TRADES query.")
    elif 0 < bar_count < 78:
        warnings.append(f"Only {bar_count} bars returned for 5 D / 5 mins query (expected ~390 for 5 full RTH sessions).")

    return {
        "intraday_ok": intraday_ok,
        "symbol": symbol,
        "duration": "5 D",
        "bar_size": "5 mins",
        "what_to_show": "TRADES",
        "bar_count": bar_count,
        "elapsed_seconds": elapsed,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "errors": blocking_errors,
        "warnings": warnings,
    }


def evaluate_track_readiness(
    account_info: dict[str, Any],
    market_data_info: dict[str, Any],
    intraday_info: dict[str, Any],
) -> dict[str, Any]:
    """Map Phase 0 diagnostics onto No.2 Track 1/2/3 readiness and hard gates."""
    t_plus_1 = bool(account_info.get("t_plus_1_cash_only", True))
    is_cash = bool(account_info.get("is_cash_account", False))
    pdt_restricted = bool(account_info.get("pdt_restricted", False))
    has_acct = bool(account_info.get("has_account_summary", False))

    t1_constraints: list[str] = ["Long-only execution; respect BMO/AMC announcement bar alignment."]
    if t_plus_1:
        t1_constraints.append("Enforce T+1 SettledCash ledger (no recycling unsettled sale proceeds same day).")
    if not has_acct:
        t1_constraints.append("Live account summary not verified yet; defaulting to conservative Cash T+1 rules.")

    track1 = {
        "name": "Track 1: Long-Only PEAD (5-20d hold)",
        "ready": True,
        "status": "READY",
        "reasons": [
            "Multi-day hold (5-20 trading days) avoids PDT limits and T+1 intraday cash lockups.",
            "Does not require paid real-time Level 1 streaming or margin shorting.",
        ],
        "constraints": t1_constraints,
    }

    track2 = {
        "name": "Track 2: Odd-Lot Tender Offers (<100 shares)",
        "ready": True,
        "manual_verification_required": True,
        "status": "READY_MANUAL_FEE_CHECK",
        "reasons": [
            "Zero real-time latency dependency; fits small accounts via <100 share odd-lot proration priority.",
        ],
        "manual_reminders": [
            "Manually verify US Voluntary Corporate Action (tender election) fee and commission tier in IBKR Client Portal before placing any odd-lot trade.",
            "Submit tender offer elections manually via IBKR Client Portal -> Corporate Actions (this repository stays strictly read-only).",
        ],
    }

    realtime_ok = bool(market_data_info.get("realtime_ok", False))
    intraday_ok = bool(intraday_info.get("intraday_ok", False))
    track3_unlocked = realtime_ok and intraday_ok

    t3_reasons: list[str] = []
    if not realtime_ok:
        codes = market_data_info.get("issue_codes") or []
        code_hint = f" (codes: {', '.join(str(c) for c in codes)})" if codes else ""
        t3_reasons.append(f"Real-time Level 1 market data (`market_data_type=1`) unavailable{code_hint}.")
    else:
        t3_reasons.append("Real-time Level 1 market data (`market_data_type=1`) confirmed.")

    if not intraday_ok:
        t3_reasons.append("5-min `5 D` `TRADES` historical bar probe failed or returned 0 bars.")
    else:
        t3_reasons.append(
            f"5-min `5 D` `TRADES` probe returned {intraday_info.get('bar_count', 0)} bars in {intraday_info.get('elapsed_seconds', 0)}s."
        )

    t3_constraints: list[str] = []
    if is_cash or t_plus_1:
        t3_constraints.append("Cash Account / T+1 SettledCash: long-only, split capital into settled buckets to prevent good-faith violations.")
    if pdt_restricted:
        dtr = account_info.get("day_trades_remaining")
        t3_constraints.append(f"PDT restricted (<$25k Margin, DayTradesRemaining={dtr}): max 3 day trades per 5 business days.")
    t3_constraints.append("Requires TWS/Gateway stability at US open (22:30/23:30 JST) and survivor-bias-free 5m validation.")

    track3 = {
        "name": "Track 3: Long-Only 5m RVOL-ORB",
        "ready": track3_unlocked,
        "unlocked": track3_unlocked,
        "status": "UNLOCKED" if track3_unlocked else "BLOCKED",
        "gated_by": ["realtime_ok", "intraday_ok"],
        "realtime_ok": realtime_ok,
        "intraday_ok": intraday_ok,
        "reasons": t3_reasons,
        "constraints": t3_constraints,
    }

    recommended_order = ["track1_pead", "track2_odd_lot"]
    if track3_unlocked:
        recommended_order.append("track3_orb_5m")

    return {
        "track1_pead": track1,
        "track2_odd_lot": track2,
        "track3_orb_5m": track3,
        "recommended_order": recommended_order,
    }


def _wait_for_market_data_snapshots(
    client: Any,
    ticker_ids: dict[str, int],
    timeout: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    deadline = time.monotonic() + max(0.5, float(timeout))
    notices: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    noticed_req_ids: set[int] = set()
    first_quote_time: dict[str, float] = {}
    target_req_ids = set(ticker_ids.values())

    while time.monotonic() <= deadline:
        for item in client.drain_market_data_notices():
            notices.append(item)
            noticed_req_ids.add(int(item.get("reqId", -1) or -1))
        for item in client.drain_errors():
            errors.append(item)
            noticed_req_ids.add(int(item.get("reqId", -1) or -1))

        now = time.monotonic()
        all_settled = True
        for symbol, tid in ticker_ids.items():
            if tid in noticed_req_ids:
                continue
            info = client.get_ticker_info(tid)
            bid = _parse_float(info.get("bid"))
            ask = _parse_float(info.get("ask"))
            last = _parse_float(info.get("last"))
            has_bid_ask = (bid is not None and bid > 0) and (ask is not None and ask > 0)
            has_any = any(v is not None and v > 0 for v in (bid, ask, last))
            if has_bid_ask:
                continue
            if has_any:
                first_quote_time.setdefault(symbol, now)
                if now - first_quote_time[symbol] >= 1.5:
                    continue
            all_settled = False

        if all_settled and target_req_ids:
            break
        time.sleep(0.1)

    for item in client.drain_market_data_notices():
        notices.append(item)
    for item in client.drain_errors():
        errors.append(item)

    items: list[dict[str, Any]] = []
    for symbol, tid in ticker_ids.items():
        info = dict(client.get_ticker_info(tid))
        info.setdefault("symbol", symbol)
        info["ticker_id"] = tid
        items.append(info)
        if tid not in noticed_req_ids:
            try:
                client.cancelMktData(tid)
            except Exception:
                pass
        client.clear_ticker(tid)

    # Discard harmless cancelMktData ack noise (e.g. error 300) after snapshot already ended
    client.drain_errors()
    client.drain_market_data_notices()
    return items, notices, errors


def run_live_probe(
    host: str,
    port: int,
    client_id: int,
    symbols: Sequence[str],
    timeout: float = 15.0,
    env_info: dict[str, Any] | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Connect read-only to TWS/IB Gateway and execute Phase 0 diagnostic probes."""
    clean_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()] or ["SPY", "AAPL"]
    intraday_symbol = "SPY" if "SPY" in clean_symbols else clean_symbols[0]

    client = client_factory() if client_factory is not None else IBKRClient()
    req_ids = RequestIdManager()
    raw_accounts: list[str] = []

    try:
        client.connect(host, port, client_id)
        api_thread = threading.Thread(target=client.run, daemon=True, name="no2-phase0-api")
        api_thread.start()

        if not client.connected_event.wait(timeout=timeout):
            raise RuntimeError(f"TWS API handshake timed out at {host}:{port} (clientId={client_id})")

        # Drain initial handshake messages (2104/2106/etc.)
        time.sleep(0.2)
        client.drain_errors()
        client.drain_market_data_notices()
        raw_accounts = list(getattr(client, "accounts", []) or [])

        # 1. Account summary probe
        acct_req_id = req_ids.allocate("account_summary")
        client.account_summary_end.clear()
        client.reqAccountSummary(acct_req_id, "All", ACCOUNT_TAGS)
        client.account_summary_end.wait(timeout=timeout)
        try:
            client.cancelAccountSummary(acct_req_id)
        except Exception:
            pass
        req_ids.release(acct_req_id)
        summary_items = client.drain_account_summary()
        acct_errors = client.drain_errors()
        account_info = summarize_account_constraints(summary_items, raw_accounts)
        for err in acct_errors:
            if int(err.get("errorCode", 0) or 0) not in IGNORED_INFO_CODES:
                account_info["notes"].append(
                    _sanitize_text(f"Account summary error {err.get('errorCode')}: {err.get('errorString', '')}", raw_accounts)
                )

        # 2. Real-time market data probe (market_data_type=1)
        client.drain_market_data_notices()
        client.drain_errors()
        client.reqMarketDataType(1)
        ticker_ids: dict[str, int] = {}
        for symbol in clean_symbols:
            tid = client.allocate_ticker_id({
                "symbol": symbol,
                "sec_type": "STK",
                "market_data_type": 1,
            })
            ticker_ids[symbol] = tid
            client.reqMktData(tid, make_stock_contract(symbol), "", True, False, [])

        md_items, md_notices, md_errors = _wait_for_market_data_snapshots(client, ticker_ids, timeout)
        market_data_info = summarize_market_data_probe(md_items, md_notices, md_errors, clean_symbols)

        # 3. 5-minute intraday historical data probe
        client.drain_historical_data()
        client.drain_historical_data_end()
        client.drain_errors()

        used_req_ids = {acct_req_id} | set(ticker_ids.values())
        skipped_hist_ids: list[int] = []
        hist_req_id = req_ids.allocate("historical")
        while hist_req_id in used_req_ids:
            skipped_hist_ids.append(hist_req_id)
            hist_req_id = req_ids.allocate("historical")
        for skipped_id in skipped_hist_ids:
            req_ids.release(skipped_id)
        start_ts = time.monotonic()
        client.reqHistoricalData(
            hist_req_id,
            make_stock_contract(intraday_symbol),
            "",
            "5 D",
            "5 mins",
            "TRADES",
            1,
            1,
            False,
            [],
        )

        hist_bars: list[dict[str, Any]] = []
        hist_errors: list[dict[str, Any]] = []
        hist_done = False

        def _drain_hist_step() -> None:
            nonlocal hist_done
            for end_evt in client.drain_historical_data_end():
                if int(end_evt.get("req_id", -1) or -1) == hist_req_id:
                    hist_done = True
            for bar in client.drain_historical_data():
                if int(bar.get("req_id", -1) or -1) == hist_req_id:
                    hist_bars.append(bar)
            for err in client.drain_errors():
                code = int(err.get("errorCode", 0) or 0)
                if code in IGNORED_INFO_CODES or code == 300:
                    continue
                if int(err.get("reqId", -1) or -1) == hist_req_id:
                    hist_errors.append(err)
                    hist_done = True

        hist_deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() <= hist_deadline and not hist_done:
            _drain_hist_step()
            if not hist_done:
                time.sleep(0.1)

        # Final post-loop drain so bars pushed right before/with historicalDataEnd are never missed.
        _drain_hist_step()

        elapsed_seconds = time.monotonic() - start_ts
        if not hist_done:
            try:
                client.cancelHistoricalData(hist_req_id)
            except Exception:
                pass
        req_ids.release(hist_req_id)

        intraday_info = summarize_intraday_probe(
            hist_bars,
            elapsed_seconds,
            hist_errors,
            symbol=intraday_symbol,
        )
        tracks = evaluate_track_readiness(account_info, market_data_info, intraday_info)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "live_probe",
            "connection": {
                "host": host,
                "port": port,
                "client_id": client_id,
                "connected": True,
                "symbols": clean_symbols,
                "intraday_symbol": intraday_symbol,
            },
            "env": env_info or {},
            "account": account_info,
            "market_data": market_data_info,
            "intraday": intraday_info,
            "tracks": tracks,
        }
        return _mask_report_recursive(report, raw_accounts)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _mask_report_recursive(obj: Any, extra_raw_accounts: Sequence[str] = ()) -> Any:
    if isinstance(obj, str):
        return _sanitize_text(obj, extra_raw_accounts)
    if isinstance(obj, list):
        return [_mask_report_recursive(item, extra_raw_accounts) for item in obj]
    if isinstance(obj, dict):
        return {k: _mask_report_recursive(v, extra_raw_accounts) for k, v in obj.items()}
    return obj


def render_markdown_report(report: dict[str, Any]) -> str:
    """Render masked Phase 0 diagnostic JSON into a readable Markdown report."""
    env = report.get("env", {})
    acct = report.get("account", {})
    md = report.get("market_data", {})
    intra = report.get("intraday", {})
    tracks = report.get("tracks", {})
    conn = report.get("connection", {})

    lines = [
        "# No.2 Phase 0 Diagnostic Report (Read-Only)",
        "",
        f"- Generated At (UTC): `{report.get('generated_at', '')}`",
        f"- Endpoint: `{conn.get('host', '127.0.0.1')}:{conn.get('port', '')}` (ClientId `{conn.get('client_id', '')}`)",
        f"- Env Check OK: `{env.get('ok', False)}` (`READ_ONLY={env.get('read_only_raw', 'N/A')}`, `APP_HOST={env.get('app_host', 'N/A')}`, `IBKR_PORT={env.get('ibkr_port', 'N/A')}`)",
        "",
        "## 1. Account Constraints (Masked)",
        "",
        f"- Accounts: `{', '.join(acct.get('accounts_masked', [])) or 'N/A'}`",
        f"- AccountType: `{acct.get('account_type') or 'N/A'}`",
        f"- Paper Account (`DU*`): `{acct.get('is_paper', False)}`",
        f"- Cash Account: `{acct.get('is_cash_account', False)}`",
        f"- T+1 SettledCash Only: `{acct.get('t_plus_1_cash_only', True)}`",
        f"- PDT Restricted: `{acct.get('pdt_restricted', False)}`",
        f"- NetLiquidation: `{acct.get('net_liquidation')}` {acct.get('currency', 'USD')}",
        f"- SettledCash: `{acct.get('settled_cash')}` {acct.get('currency', 'USD')}",
        f"- AvailableFunds: `{acct.get('available_funds')}` {acct.get('currency', 'USD')}",
        f"- DayTradesRemaining: `{acct.get('day_trades_remaining')}`",
        "",
        "## 2. Market Data Probe (`market_data_type=1`)",
        "",
        f"- `realtime_ok`: `{md.get('realtime_ok', False)}` (status: `{md.get('status', 'unknown')}`)",
        f"- Issue Codes: `{md.get('issue_codes', [])}`",
        "",
        "| Symbol | Bid | Ask | Last | Quote OK | Issue Codes |",
        "|---|---:|---:|---:|:---:|---|",
    ]

    for symbol, info in (md.get("per_symbol") or {}).items():
        lines.append(
            f"| {symbol} | {info.get('bid')} | {info.get('ask')} | {info.get('last')} | "
            f"{info.get('has_quote', False)} | {info.get('issue_codes', [])} |"
        )
    if not md.get("per_symbol"):
        lines.append("| - | - | - | - | False | [] |")

    lines.extend([
        "",
        "## 3. Intraday 5-Min Historical Probe (`5 D` / `5 mins` / `TRADES`)",
        "",
        f"- `intraday_ok`: `{intra.get('intraday_ok', False)}` ({intra.get('symbol', 'SPY')})",
        f"- Bar Count: `{intra.get('bar_count', 0)}`",
        f"- Latency: `{intra.get('elapsed_seconds', 0.0)}s`",
        f"- Range: `{intra.get('first_timestamp', '')}` -> `{intra.get('last_timestamp', '')}`",
        "",
        "## 4. Track Readiness & Gates",
        "",
    ])

    for key in ("track1_pead", "track2_odd_lot", "track3_orb_5m"):
        t = tracks.get(key, {})
        lines.append(f"### {t.get('name', key)} — `{t.get('status', 'UNKNOWN')}`")
        for reason in t.get("reasons", []):
            lines.append(f"- {reason}")
        for note in t.get("constraints", []):
            lines.append(f"- Constraint: {note}")
        for reminder in t.get("manual_reminders", []):
            lines.append(f"- Manual Action: {reminder}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_phase0_report(output_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """Write masked `no2-phase0-latest.{json,md}` to `output_dir`."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_report = _mask_report_recursive(report)
    json_path = out_dir / "no2-phase0-latest.json"
    md_path = out_dir / "no2-phase0-latest.md"
    json_path.write_text(json.dumps(safe_report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_sanitize_text(render_markdown_report(safe_report)), encoding="utf-8")
    return json_path, md_path


def _print_env_summary(env_info: dict[str, Any]) -> None:
    print(f"[Phase 0 Env Check] path={env_info['env_path']}")
    print(f"  exists: {env_info['exists']}")
    print(f"  READ_ONLY=true: {env_info['read_only_ok']} (raw={env_info['read_only_raw']!r})")
    print(f"  APP_HOST: {env_info['app_host']!r} (ok={env_info['app_host_ok']})")
    print(f"  IBKR_PORT: {env_info['ibkr_port']} (ok={env_info['ibkr_port_ok']}, mode={env_info['port_mode']})")
    if env_info.get("ibkr_account_masked"):
        print(f"  IBKR_ACCOUNT: {env_info['ibkr_account_masked']}")
    for warn in env_info.get("warnings", []):
        print(f"  [WARN] {warn}")
    for issue in env_info.get("issues", []):
        print(f"  [ISSUE] {issue}")
    print(f"  env_ok: {env_info['ok']}")
    print("  Manual Reminder: Verify US Voluntary Corporate Action fee & Tiered/Fixed commissions in IBKR Client Portal -> Corporate Actions.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 0 read-only diagnostic for No.2 US equity tracks")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=79)
    parser.add_argument("--symbols", default="SPY,AAPL")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "backtests")
    parser.add_argument("--env-only", action="store_true", help="Offline check of .env read-only guards without connecting to TWS")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env", help="Path to .env file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_info = check_env_read_only(args.env_file)

    if args.env_only:
        _print_env_summary(env_info)
        # Fail fast if an existing .env explicitly disables read-only or exposes non-local host
        if env_info["exists"] and (not env_info["read_only_ok"] or not env_info["app_host_ok"]):
            return 1
        return 0

    if env_info["exists"] and not env_info["read_only_ok"]:
        _print_env_summary(env_info)
        print("[ABORT] Refusing live probe when READ_ONLY is not true in .env.", file=sys.stderr)
        return 1

    setup_logging("WARNING")
    logging.getLogger("ibapi").setLevel(logging.WARNING)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("at least one symbol is required")

    try:
        report = run_live_probe(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            symbols=symbols,
            timeout=args.timeout,
            env_info=env_info,
        )
    except Exception as exc:
        print(f"[FAIL] Live Phase 0 probe failed: {_sanitize_text(str(exc))}", file=sys.stderr)
        return 1

    json_path, md_path = save_phase0_report(args.output_dir, report)
    acct = report["account"]
    md = report["market_data"]
    intra = report["intraday"]
    tracks = report["tracks"]

    print(f"Account: {acct['primary_account_masked']} | type={acct['account_type'] or 'N/A'} | "
          f"paper={acct['is_paper']} | cash_t1={acct['t_plus_1_cash_only']} | pdt={acct['pdt_restricted']}")
    print(f"Market Data (type=1): realtime_ok={md['realtime_ok']} | status={md['status']} | codes={md['issue_codes']}")
    print(f"Intraday 5m ({intra['symbol']}): intraday_ok={intra['intraday_ok']} | bars={intra['bar_count']} | elapsed={intra['elapsed_seconds']}s")
    print(f"Track 1 PEAD: {tracks['track1_pead']['status']}")
    print(f"Track 2 Odd-Lot: {tracks['track2_odd_lot']['status']} (check Voluntary CA fee in IBKR Client Portal)")
    print(f"Track 3 5m ORB: {tracks['track3_orb_5m']['status']}")
    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
