"""Read-only SEC EDGAR Point-in-Time PEAD earnings event loader (zero lookahead)."""

from __future__ import annotations

import csv
import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

import httpx

from app.trading.odd_lot_scanner import validate_sec_user_agent
from app.trading.pead_strategy import EarningsEvent, EarningsSession

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

EPS_CONCEPTS: tuple[str, ...] = (
    "EarningsPerShareDiluted",
    "EarningsPerShareBasic",
)
REVENUE_CONCEPTS: tuple[str, ...] = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueGoodsNet",
)


def classify_ny_session(acceptance_dt_utc: str) -> tuple[date, EarningsSession]:
    """Convert SEC UTC acceptance timestamp to (NY announcement date, BMO/AMC).

    Filings accepted strictly before 09:30 America/New_York are classified BMO;
    filings accepted at or after 09:30 (intraday or after-hours) are classified AMC
    so entry never uses a same-day open prior to the filing timestamp.
    """
    raw = str(acceptance_dt_utc or "").strip()
    if not raw:
        raise ValueError("acceptance_dt_utc must not be empty")
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_ny = dt.astimezone(NY_TZ)
    if (dt_ny.hour, dt_ny.minute) < (9, 30):
        return dt_ny.date(), EarningsSession.BMO
    return dt_ny.date(), EarningsSession.AMC


def extract_8k_earnings_releases(submissions_json: dict[str, Any]) -> list[tuple[date, EarningsSession, str]]:
    """Extract (announcement_date, session, accession_number) for 8-K Item 2.02 filings."""
    recent = submissions_json.get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    items_list = recent.get("items") or []
    acceptance_times = recent.get("acceptanceDateTime") or []
    filing_dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []

    seen: set[tuple[date, EarningsSession]] = set()
    releases: list[tuple[date, EarningsSession, str]] = []

    count = len(forms)
    for idx in range(count):
        form = str(forms[idx] or "").strip().upper()
        if form != "8-K":
            continue

        raw_items = items_list[idx] if idx < len(items_list) else ""
        if isinstance(raw_items, list):
            tokens = {str(x).strip() for x in raw_items if str(x).strip()}
        else:
            tokens = {x.strip() for x in str(raw_items or "").split(",") if x.strip()}
        if "2.02" not in tokens:
            continue

        accn = str(accessions[idx] if idx < len(accessions) else "").strip()
        acc_dt_raw = str(acceptance_times[idx] if idx < len(acceptance_times) else "").strip()
        filing_date_raw = str(filing_dates[idx] if idx < len(filing_dates) else "").strip()

        ann_date: date | None = None
        session = EarningsSession.AMC
        if acc_dt_raw:
            try:
                ann_date, session = classify_ny_session(acc_dt_raw)
            except ValueError:
                ann_date = None
        if ann_date is None and filing_date_raw:
            ann_date = date.fromisoformat(filing_date_raw[:10])
            session = EarningsSession.AMC
        if ann_date is None:
            continue

        key = (ann_date, session)
        if key in seen:
            continue
        seen.add(key)
        releases.append((ann_date, session, accn))

    releases.sort(key=lambda row: (row[0], row[1].value, row[2]))
    return releases


def _collect_same_accn_yoy_pairs(
    facts: list[dict[str, Any]],
    *,
    allowed_forms: set[str],
    min_duration_days: int,
    max_duration_days: int,
) -> list[dict[str, Any]]:
    by_accn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in facts:
        form = str(item.get("form") or "").strip().upper()
        if form not in allowed_forms:
            continue
        start_s = item.get("start")
        end_s = item.get("end")
        filed_s = item.get("filed")
        accn = str(item.get("accn") or "").strip()
        val = item.get("val")
        if not (start_s and end_s and filed_s and accn) or not isinstance(val, (int, float)):
            continue
        try:
            start_dt = date.fromisoformat(str(start_s)[:10])
            end_dt = date.fromisoformat(str(end_s)[:10])
            filed_dt = date.fromisoformat(str(filed_s)[:10])
        except ValueError:
            continue
        duration = (end_dt - start_dt).days
        if not (min_duration_days <= duration <= max_duration_days):
            continue
        by_accn[accn].append(
            {
                "end": end_dt,
                "start": start_dt,
                "filed": filed_dt,
                "val": float(val),
                "form": form,
                "accn": accn,
            }
        )

    pairs: list[dict[str, Any]] = []
    for accn, items in by_accn.items():
        by_end: dict[date, dict[str, Any]] = {}
        for item in items:
            by_end.setdefault(item["end"], item)
        if len(by_end) < 2:
            continue
        ends_desc = sorted(by_end.keys(), reverse=True)
        curr = by_end[ends_desc[0]]
        prior_match: dict[str, Any] | None = None
        for prev_end in ends_desc[1:]:
            gap_days = (curr["end"] - prev_end).days
            if 330 <= gap_days <= 400:
                prior_match = by_end[prev_end]
                break
        if prior_match is None:
            continue
        pairs.append(
            {
                "period_end": curr["end"],
                "filed": curr["filed"],
                "actual": curr["val"],
                "estimate": prior_match["val"],
                "form": curr["form"],
                "accn": accn,
            }
        )
    return pairs


def extract_xbrl_yoy_quarterly_facts(
    companyfacts_json: dict[str, Any],
    concept_names: Sequence[str],
    unit_key: str,
    *,
    value_decimals: int = 4,
) -> dict[date, dict[str, Any]]:
    """Extract split-consistent point-in-time YoY quarterly facts (10-Q + derived 10-K Q4)."""
    us_gaap = companyfacts_json.get("facts", {}).get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        return {}

    merged_by_end: dict[date, dict[str, Any]] = {}

    for priority_idx, concept in enumerate(concept_names):
        concept_obj = us_gaap.get(concept)
        if not isinstance(concept_obj, dict):
            continue
        raw_facts = concept_obj.get("units", {}).get(unit_key, [])
        if not isinstance(raw_facts, list) or not raw_facts:
            continue

        direct_q_pairs = _collect_same_accn_yoy_pairs(
            raw_facts,
            allowed_forms={"10-Q", "10-K"},
            min_duration_days=65,
            max_duration_days=115,
        )
        ytd9_pairs = _collect_same_accn_yoy_pairs(
            raw_facts,
            allowed_forms={"10-Q"},
            min_duration_days=240,
            max_duration_days=295,
        )
        annual_pairs = _collect_same_accn_yoy_pairs(
            raw_facts,
            allowed_forms={"10-K"},
            min_duration_days=340,
            max_duration_days=385,
        )

        # Earliest point-in-time 9-month YTD per period_end for Q4 subtraction.
        ytd9_by_end: dict[date, dict[str, Any]] = {}
        for row in sorted(ytd9_pairs, key=lambda r: (r["period_end"], r["filed"])):
            ytd9_by_end.setdefault(row["period_end"], row)

        candidate_rows = list(direct_q_pairs)
        for fy_row in annual_pairs:
            fy_end = fy_row["period_end"]
            matching_ytd: dict[str, Any] | None = None
            for ytd_end, ytd_row in sorted(ytd9_by_end.items(), reverse=True):
                diff_days = (fy_end - ytd_end).days
                if 70 <= diff_days <= 115:
                    matching_ytd = ytd_row
                    break
            if matching_ytd is None:
                continue

            # Guard against split discontinuities between Q3 10-Q and FY 10-K filings.
            fy_prev = fy_row["estimate"]
            fy_curr = fy_row["actual"]
            ytd_prev = matching_ytd["estimate"]
            ytd_curr = matching_ytd["actual"]
            if abs(ytd_prev) > max(abs(fy_prev), 0.25) * 1.5:
                continue
            if abs(ytd_curr) > max(abs(fy_curr), 0.25) * 1.5:
                continue

            candidate_rows.append(
                {
                    "period_end": fy_end,
                    "filed": fy_row["filed"],
                    "actual": round(fy_curr - ytd_curr, value_decimals),
                    "estimate": round(fy_prev - ytd_prev, value_decimals),
                    "form": "10-K_Q4",
                    "accn": fy_row["accn"],
                }
            )

        # Keep earliest filed per period_end; higher-priority concept fills earlier if available.
        for row in sorted(candidate_rows, key=lambda r: (r["period_end"], r["filed"])):
            end_dt = row["period_end"]
            existing = merged_by_end.get(end_dt)
            if existing is None:
                merged_by_end[end_dt] = {**row, "concept": concept, "priority": priority_idx}
            elif priority_idx == existing["priority"] and row["filed"] < existing["filed"]:
                merged_by_end[end_dt] = {**row, "concept": concept, "priority": priority_idx}

    return merged_by_end


def _match_revenue_for_period(
    period_end: date,
    revenue_by_end: dict[date, dict[str, Any]],
) -> dict[str, Any] | None:
    if period_end in revenue_by_end:
        return revenue_by_end[period_end]
    for rev_end, row in revenue_by_end.items():
        if abs((rev_end - period_end).days) <= 5:
            return row
    return None


def build_pead_events_from_sec(
    symbol: str,
    submissions_json: dict[str, Any],
    companyfacts_json: dict[str, Any],
    *,
    start_date: date | None = None,
) -> list[EarningsEvent]:
    """Combine SEC 8-K Item 2.02 release timing with XBRL YoY Seasonal Random Walk EPS/revenue."""
    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        raise ValueError("symbol must not be empty")

    eps_by_end = extract_xbrl_yoy_quarterly_facts(
        companyfacts_json,
        EPS_CONCEPTS,
        "USD/shares",
        value_decimals=4,
    )
    revenue_by_end = extract_xbrl_yoy_quarterly_facts(
        companyfacts_json,
        REVENUE_CONCEPTS,
        "USD",
        value_decimals=2,
    )
    releases = extract_8k_earnings_releases(submissions_json)

    events_by_date: dict[date, EarningsEvent] = {}
    for period_end in sorted(eps_by_end):
        eps_row = eps_by_end[period_end]
        eps_estimate = float(eps_row["estimate"])
        eps_actual = float(eps_row["actual"])
        if abs(eps_estimate) <= 1e-9:
            continue

        filed_dt: date = eps_row["filed"]
        candidates = [
            rel
            for rel in releases
            if period_end < rel[0] <= filed_dt + timedelta(days=5)
            and (rel[0] - period_end).days <= 75
        ]
        # Prefer the latest 8-K Item 2.02 on/before 10-Q/10-K (+1d UTC) so early
        # post-quarter deliveries updates (e.g. TSLA Day+2) are skipped cleanly.
        on_or_before = [rel for rel in candidates if rel[0] <= filed_dt + timedelta(days=1)]
        matched_release = on_or_before[-1] if on_or_before else (candidates[0] if candidates else None)

        if matched_release is not None:
            ann_date, session, _ = matched_release
            source = "SEC_EDGAR_8K_202+XBRL_SRW"
        else:
            ann_date = filed_dt
            session = EarningsSession.AMC
            source = "SEC_EDGAR_10QK+XBRL_SRW"

        if start_date is not None and ann_date < start_date:
            continue
        if ann_date in events_by_date:
            continue

        rev_row = _match_revenue_for_period(period_end, revenue_by_end)
        rev_actual = float(rev_row["actual"]) if rev_row is not None else None
        rev_estimate = (
            float(rev_row["estimate"])
            if rev_row is not None and abs(float(rev_row["estimate"])) > 1e-9
            else None
        )
        if rev_estimate is None:
            rev_actual = None

        events_by_date[ann_date] = EarningsEvent(
            symbol=clean_symbol,
            announcement_date=ann_date,
            session=session,
            eps_actual=eps_actual,
            eps_estimate=eps_estimate,
            revenue_actual=rev_actual,
            revenue_estimate=rev_estimate,
            source=source,
        )

    return [events_by_date[d] for d in sorted(events_by_date)]


def fetch_sec_ticker_cik_map(
    user_agent: str,
    *,
    cache_path: Path | None = None,
    timeout_seconds: float = 20.0,
    http_client: httpx.Client | None = None,
) -> dict[str, str]:
    """Fetch or load SEC ticker -> 10-digit zero-padded CIK mapping."""
    ua = validate_sec_user_agent(user_agent)
    if cache_path is not None and cache_path.exists():
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        return _parse_company_tickers_payload(raw)

    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout_seconds, headers=headers, follow_redirects=True)
    try:
        resp = client.get(SEC_TICKERS_URL, headers=headers)
        resp.raise_for_status()
        raw = resp.json()
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(raw), encoding="utf-8")
        return _parse_company_tickers_payload(raw)
    finally:
        if owns_client:
            client.close()


def _parse_company_tickers_payload(payload: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if isinstance(payload, dict):
        items = payload.values()
    elif isinstance(payload, list):
        items = payload
    else:
        return mapping

    for entry in items:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        cik_val = entry.get("cik_str")
        if not ticker or cik_val is None:
            continue
        cik_digits = str(cik_val).strip()
        if cik_digits.isdigit():
            mapping[ticker] = cik_digits.zfill(10)
    return mapping


def fetch_pead_events_for_symbols(
    symbols: Sequence[str],
    user_agent: str,
    *,
    start_date: date | None = None,
    cache_dir: Path | None = None,
    rate_limit_seconds: float = 0.25,
    timeout_seconds: float = 25.0,
    http_client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[list[EarningsEvent], list[str]]:
    """Fetch SEC EDGAR 8-K + XBRL PEAD events for symbols; return (events, skipped_symbols)."""
    ua = validate_sec_user_agent(user_agent)
    if rate_limit_seconds < 0.2:
        raise ValueError("rate_limit_seconds must be >= 0.2 for SEC EDGAR compliance")

    clean_symbols = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    if not clean_symbols:
        raise ValueError("at least one symbol is required")

    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout_seconds, headers=headers, follow_redirects=True)
    events: list[EarningsEvent] = []
    skipped: list[str] = []

    try:
        tickers_cache = (cache_dir / "sec_company_tickers.json") if cache_dir is not None else None
        ticker_to_cik = fetch_sec_ticker_cik_map(
            ua,
            cache_path=tickers_cache,
            timeout_seconds=timeout_seconds,
            http_client=client,
        )

        for symbol in clean_symbols:
            cik10 = ticker_to_cik.get(symbol)
            if not cik10:
                logger.warning("Skipping %s: no SEC CIK mapping found", symbol)
                skipped.append(symbol)
                continue

            sub_cache = (cache_dir / f"{symbol}_submissions.json") if cache_dir is not None else None
            if sub_cache is not None and sub_cache.exists():
                submissions_json = json.loads(sub_cache.read_text(encoding="utf-8"))
            else:
                sleep_fn(rate_limit_seconds)
                sub_resp = client.get(SEC_SUBMISSIONS_URL.format(cik10=cik10), headers=headers)
                if sub_resp.status_code != 200:
                    logger.warning("Skipping %s: submissions HTTP %s", symbol, sub_resp.status_code)
                    skipped.append(symbol)
                    continue
                submissions_json = sub_resp.json()
                if sub_cache is not None:
                    sub_cache.parent.mkdir(parents=True, exist_ok=True)
                    sub_cache.write_text(json.dumps(submissions_json), encoding="utf-8")

            cf_cache = (cache_dir / f"{symbol}_companyfacts.json") if cache_dir is not None else None
            if cf_cache is not None and cf_cache.exists():
                companyfacts_json = json.loads(cf_cache.read_text(encoding="utf-8"))
            else:
                sleep_fn(rate_limit_seconds)
                cf_resp = client.get(SEC_COMPANYFACTS_URL.format(cik10=cik10), headers=headers)
                if cf_resp.status_code != 200:
                    logger.warning("Skipping %s: companyfacts HTTP %s", symbol, cf_resp.status_code)
                    skipped.append(symbol)
                    continue
                companyfacts_json = cf_resp.json()
                if cf_cache is not None:
                    cf_cache.parent.mkdir(parents=True, exist_ok=True)
                    cf_cache.write_text(json.dumps(companyfacts_json), encoding="utf-8")

            sym_events = build_pead_events_from_sec(
                symbol,
                submissions_json,
                companyfacts_json,
                start_date=start_date,
            )
            if not sym_events:
                logger.warning("Skipping %s: no us-gaap 10-Q/8-K earnings events extracted", symbol)
                skipped.append(symbol)
                continue
            events.extend(sym_events)
    finally:
        if owns_client:
            client.close()

    events.sort(key=lambda ev: (ev.announcement_date, ev.symbol))
    return events, skipped


def write_pead_events_csv(events: Sequence[EarningsEvent], output_path: Path) -> Path:
    """Write PEAD events in the exact schema expected by scripts/run_pead_study.py."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "symbol",
                "announcement_date",
                "session",
                "eps_actual",
                "eps_estimate",
                "revenue_actual",
                "revenue_estimate",
                "source",
            ]
        )
        for ev in events:
            writer.writerow(
                [
                    ev.symbol,
                    ev.announcement_date.isoformat(),
                    ev.session.value,
                    ev.eps_actual,
                    ev.eps_estimate,
                    "" if ev.revenue_actual is None else ev.revenue_actual,
                    "" if ev.revenue_estimate is None else ev.revenue_estimate,
                    ev.source,
                ]
            )
    return output_path
