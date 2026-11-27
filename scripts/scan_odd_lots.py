"""Read-only US odd-lot tender offer scanner CLI (EDGAR / local fixtures + optional TWS Ask)."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.trading.odd_lot_scanner import (  # noqa: E402
    DEFAULT_EDGAR_FORMS,
    MANUAL_EXECUTION_REMINDER,
    OddLotOpportunity,
    OpportunityStatus,
    TenderOfferFiling,
    evaluate_odd_lot_opportunity,
    fetch_recent_edgar_tender_filings,
    fetch_tws_ask_quotes,
    parse_tender_offer_text,
)

_STATUS_SORT_ORDER = {
    OpportunityStatus.ACTIONABLE_REVIEW: 0,
    OpportunityStatus.LOW_OR_NEGATIVE_NET: 1,
    OpportunityStatus.NEEDS_QUOTE: 2,
    OpportunityStatus.NO_ODD_LOT_PRIORITY: 3,
    OpportunityStatus.EXPIRED: 4,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly read-only odd-lot tender offer scanner. "
            "Generates a human-review checklist for manual execution via IBKR Client Portal -> Corporate Actions."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="Offline directory containing local .txt/.html/.htm/.json tender offer filings.",
    )
    parser.add_argument(
        "--edgar",
        action="store_true",
        help="Fetch recent SC TO-I / SC TO-T / SC 13E3 filings from SEC EDGAR.",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("SEC_EDGAR_USER_AGENT", ""),
        help="SEC EDGAR User-Agent (required with --edgar, e.g. 'IBKRMonitor user@example.com').",
    )
    parser.add_argument("--days", type=int, default=30, help="Lookback calendar days for EDGAR search (default: 30).")
    parser.add_argument("--limit", type=int, default=20, help="Maximum EDGAR filings to inspect (default: 20).")
    parser.add_argument(
        "--forms",
        default=",".join(DEFAULT_EDGAR_FORMS),
        help="Comma-separated SEC forms to query (default: 'SC TO-I,SC TO-T,SC 13E3').",
    )
    parser.add_argument(
        "--query",
        default='"odd lot" OR "odd-lot"',
        help="EDGAR EFTS search query (default: '\"odd lot\" OR \"odd-lot\"'; pass empty string for unfiltered).",
    )
    parser.add_argument(
        "--listed-only",
        action="store_true",
        help="Skip EDGAR filings without a resolved exchange ticker symbol.",
    )
    parser.add_argument(
        "--quotes",
        default="",
        help="Manual Ask quotes as 'SYM=12.34,OTHER=45.60'. Overrides fixture quotes and skips TWS for those symbols.",
    )
    parser.add_argument(
        "--use-tws",
        action="store_true",
        help="Read-only fetch missing Ask quotes from IBKR TWS via reqMktData(..., snapshot=True).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="TWS host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=7497, help="TWS port (default: 7497).")
    parser.add_argument("--client-id", type=int, default=79, help="TWS client ID (default: 79).")
    parser.add_argument(
        "--market-data-type",
        type=int,
        default=4,
        help="TWS reqMarketDataType (1=live, 2=frozen, 3=delayed, 4=delayed-frozen; default: 4).",
    )
    parser.add_argument(
        "--voluntary-ca-fee",
        type=float,
        default=0.0,
        help="IBKR Voluntary Corporate Action handling fee in USD (default: 0.0).",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=1.0,
        help="Estimated buy-side commission in USD (default: 1.0).",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=10.0,
        help="Entry slippage assumption above Ask in bps (default: 10.0).",
    )
    parser.add_argument(
        "--max-shares",
        type=int,
        default=99,
        help="Maximum odd-lot share cap (default: 99).",
    )
    parser.add_argument(
        "--as-of-date",
        default="",
        help="Evaluation date YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "backtests",
        help="Directory for odd-lot-scan-latest.{json,md} and timestamped reports.",
    )
    return parser.parse_args(argv)


def parse_quotes_arg(raw: str) -> dict[str, float]:
    quotes: dict[str, float] = {}
    if not raw.strip():
        return quotes
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"invalid quote entry {item!r}; expected SYM=PRICE")
        sym_raw, price_raw = item.split("=", 1)
        sym = sym_raw.strip().upper()
        if not sym:
            raise ValueError(f"missing symbol in quote entry {item!r}")
        price = float(price_raw.strip())
        if price <= 0:
            raise ValueError(f"quote price must be positive for {sym}: {price}")
        quotes[sym] = price
    return quotes


def _infer_symbol_from_filename(path: Path) -> str:
    match = re.match(r"^([A-Z]{1,5})(?:[_-].*)?$", path.stem)
    return match.group(1) if match else ""


def _load_json_item(item: dict[str, Any], path: Path) -> tuple[TenderOfferFiling, float | None]:
    sym = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
    raw_text = item.get("text") or item.get("html") or item.get("content") or item.get("body")
    ask_val = item.get("ask_price")
    ask_price = float(ask_val) if isinstance(ask_val, (int, float)) and float(ask_val) > 0 else None

    if isinstance(raw_text, str) and raw_text.strip():
        filing = parse_tender_offer_text(
            raw_text,
            symbol=sym,
            company_name=str(item.get("company_name") or ""),
            cik=str(item.get("cik") or ""),
            form_type=str(item.get("form_type") or ""),
            accession_number=str(item.get("accession_number") or ""),
            filing_date=item.get("filing_date"),
            filing_url=str(item.get("filing_url") or ""),
            source_path=str(path),
            title=str(item.get("title") or ""),
        )
        # Overlay explicit JSON overrides if supplied alongside raw text.
        if item.get("offer_type") is not None:
            filing.offer_type = str(item["offer_type"])
        if item.get("min_offer_price") is not None:
            filing.min_offer_price = float(item["min_offer_price"])
        if item.get("max_offer_price") is not None:
            filing.max_offer_price = float(item["max_offer_price"])
        if item.get("odd_lot_priority") is not None:
            filing.odd_lot_priority = bool(item["odd_lot_priority"])
        if item.get("expiration_date"):
            filing.expiration_date = date.fromisoformat(str(item["expiration_date"])[:10])
        return filing, ask_price

    filing = TenderOfferFiling(
        symbol=sym,
        company_name=str(item.get("company_name") or ""),
        cik=str(item.get("cik") or ""),
        form_type=str(item.get("form_type") or ""),
        accession_number=str(item.get("accession_number") or ""),
        filing_date=date.fromisoformat(str(item["filing_date"])[:10]) if item.get("filing_date") else None,
        filing_url=str(item.get("filing_url") or ""),
        source_path=str(path),
        title=str(item.get("title") or ""),
        offer_type=str(item.get("offer_type") or "unknown"),
        min_offer_price=float(item["min_offer_price"]) if item.get("min_offer_price") is not None else None,
        max_offer_price=float(item["max_offer_price"]) if item.get("max_offer_price") is not None else None,
        odd_lot_priority=bool(item.get("odd_lot_priority", False)),
        odd_lot_max_shares=int(item.get("odd_lot_max_shares", 99)),
        expiration_date=date.fromisoformat(str(item["expiration_date"])[:10]) if item.get("expiration_date") else None,
        expiration_text=str(item.get("expiration_text") or ""),
        proration_risk=bool(item.get("proration_risk", False)),
        withdrawal_risk=bool(item.get("withdrawal_risk", False)),
        financing_risk=bool(item.get("financing_risk", False)),
        risk_terms=list(item.get("risk_terms") or []),
        evidence_snippets=list(item.get("evidence_snippets") or []),
    )
    return filing, ask_price


def load_local_filings(input_dir: Path) -> tuple[list[TenderOfferFiling], dict[str, float]]:
    if not input_dir.exists() or not input_dir.is_dir():
        raise FileNotFoundError(f"input directory does not exist: {input_dir}")

    filings: list[TenderOfferFiling] = []
    embedded_quotes: dict[str, float] = {}
    supported_exts = {".txt", ".html", ".htm", ".json"}
    files = sorted(p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in supported_exts)

    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload if isinstance(payload, list) else payload.get("filings", [payload]) if isinstance(payload, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                filing, ask_price = _load_json_item(item, path)
                filings.append(filing)
                if filing.symbol and ask_price is not None:
                    embedded_quotes[filing.symbol] = ask_price
        else:
            raw = path.read_text(encoding="utf-8", errors="replace")
            inferred_sym = _infer_symbol_from_filename(path)
            filing = parse_tender_offer_text(
                raw,
                symbol=inferred_sym,
                title=path.stem,
                source_path=str(path),
            )
            filings.append(filing)

    return filings, embedded_quotes


def _sort_opportunities(opportunities: list[OddLotOpportunity]) -> list[OddLotOpportunity]:
    return sorted(
        opportunities,
        key=lambda opp: (
            _STATUS_SORT_ORDER.get(opp.status, 99),
            -(opp.worst_case_net_profit if opp.worst_case_net_profit is not None else -1e12),
            opp.symbol,
            opp.source_path,
        ),
    )


def _format_money(val: float | None) -> str:
    return f"${val:,.2f}" if val is not None else "-"


def _format_pct(val: float | None) -> str:
    return f"{val:.2f}%" if val is not None else "-"


def _markdown_report(
    opportunities: list[OddLotOpportunity],
    *,
    as_of_date: date,
    source_label: str,
    voluntary_ca_fee: float,
    buy_commission: float,
    slippage_bps: float,
    max_shares: int,
    quote_sources: dict[str, str] | None = None,
) -> str:
    counts = {status.value: sum(1 for o in opportunities if o.status == status) for status in OpportunityStatus}
    lines = [
        "# US Odd-Lot Tender Offer Human-Review Checklist",
        "",
        f"> **{MANUAL_EXECUTION_REMINDER}**",
        "",
        f"- **Evaluation Date**: `{as_of_date.isoformat()}`",
        f"- **Source**: `{source_label}`",
        f"- **Read-Only Mode**: `True` (zero automated execution)",
        f"- **Assumptions**: max_shares=`{max_shares}`, buy_commission=`${buy_commission:.2f}`, "
        f"slippage=`{slippage_bps:.1f} bps`, voluntary_ca_fee=`${voluntary_ca_fee:.2f}`",
        (
            f"- **Summary**: total=`{len(opportunities)}`, "
            f"ACTIONABLE_REVIEW=`{counts['ACTIONABLE_REVIEW']}`, "
            f"LOW_OR_NEGATIVE_NET=`{counts['LOW_OR_NEGATIVE_NET']}`, "
            f"NEEDS_QUOTE=`{counts['NEEDS_QUOTE']}`, "
            f"NO_ODD_LOT_PRIORITY=`{counts['NO_ODD_LOT_PRIORITY']}`, "
            f"EXPIRED=`{counts['EXPIRED']}`"
        ),
    ]
    if quote_sources:
        formatted = ", ".join(f"{sym}={src}" for sym, src in sorted(quote_sources.items()))
        lines.append(f"- **TWS Quote Sources**: `{formatted}`")
    lines.extend([
        "",
        "## Manual Verification Steps Before Any Trade",
        "",
        "1. Open the SEC filing / Exhibit (a)(1)(A) and verify odd-lot priority is uncapped and exempt from proration.",
        "2. Check for withdrawal, termination, financing, or minimum-condition clauses that could cancel the offer.",
        "3. Confirm your IBKR account's Voluntary Corporate Action election fee (a high fee can erase odd-lot profit).",
        "4. Check broker Corporate Actions deadline (typically 1-2 business days earlier than SEC expiration date).",
        "5. Execute manually ONLY via **IBKR Client Portal -> Corporate Actions** after buying <= 99 shares.",
        "",
        "## Evaluated Filings",
        "",
        "| Symbol | Form | Status | Offer Range | Ask | Max Shares | Worst Net | Best Net | Worst Ret | Worst Ann | Expiration | Risk Flags |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ])

    if not opportunities:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | No filings found |")
    else:
        for opp in opportunities:
            if opp.min_offer_price is not None and opp.max_offer_price is not None:
                if abs(opp.max_offer_price - opp.min_offer_price) < 1e-9:
                    offer_str = f"${opp.min_offer_price:,.2f}"
                else:
                    offer_str = f"${opp.min_offer_price:,.2f} - ${opp.max_offer_price:,.2f}"
            else:
                offer_str = "-"
            exp_str = opp.expiration_date.isoformat() if opp.expiration_date else "-"
            flags_str = ", ".join(opp.risk_flags) if opp.risk_flags else "-"
            lines.append(
                f"| {opp.symbol or '-'} | {opp.form_type or '-'} | {opp.status.value} | {offer_str} | "
                f"{_format_money(opp.ask_price)} | {opp.max_shares} | "
                f"{_format_money(opp.worst_case_net_profit)} | {_format_money(opp.best_case_net_profit)} | "
                f"{_format_pct(opp.worst_case_return_pct)} | {_format_pct(opp.worst_case_annualized_pct)} | "
                f"{exp_str} | {flags_str} |"
            )

    lines.extend(["", "## Per-Opportunity Human-Review Checklist", ""])
    if not opportunities:
        lines.append("_No tender offer filings matched the selected filters._")
    else:
        for idx, opp in enumerate(opportunities, start=1):
            label = opp.symbol or opp.company_name or Path(opp.source_path).name or f"Filing #{idx}"
            lines.append(f"### {idx}. {label} (`{opp.status.value}`)")
            lines.append("")
            lines.append(f"- [ ] Confirm odd-lot priority (`odd_lot_priority={opp.odd_lot_priority}`) and cap (`<= {opp.max_shares}` shares).")
            lines.append(f"- [ ] Verify worst-case net profit (`{_format_money(opp.worst_case_net_profit)}`) against IBKR Voluntary CA fee (`${opp.voluntary_ca_fee:.2f}`).")
            lines.append("- [ ] If proceeding, submit election manually in **IBKR Client Portal -> Corporate Actions**.")
            if opp.filing_url:
                lines.append(f"- Filing URL: {opp.filing_url}")
            if opp.source_path:
                lines.append(f"- Source Path: `{opp.source_path}`")
            if opp.risk_flags:
                lines.append(f"- Risk Flags: `{', '.join(opp.risk_flags)}`")
            if opp.evidence_snippets:
                lines.append("- Evidence Snippets:")
                for snippet in opp.evidence_snippets:
                    lines.append(f"  - {snippet}")
            lines.append("")

    return "\n".join(lines)


def save_scan_report(
    output_dir: Path,
    opportunities: list[OddLotOpportunity],
    *,
    as_of_date: date,
    source_label: str,
    voluntary_ca_fee: float,
    buy_commission: float,
    slippage_bps: float,
    max_shares: int,
    quote_sources: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    counts = {status.value: sum(1 for o in opportunities if o.status == status) for status in OpportunityStatus}

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of_date.isoformat(),
        "source": source_label,
        "read_only": True,
        "manual_execution_reminder": MANUAL_EXECUTION_REMINDER,
        "parameters": {
            "voluntary_ca_fee": voluntary_ca_fee,
            "buy_commission": buy_commission,
            "slippage_bps": slippage_bps,
            "max_shares": max_shares,
        },
        "quote_sources": quote_sources or {},
        "summary": {
            "total_filings": len(opportunities),
            **counts,
        },
        "opportunities": [opp.to_dict() for opp in opportunities],
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path = output_dir / f"odd-lot-scan-{timestamp}.json"
    json_path.write_text(json_text, encoding="utf-8")
    (output_dir / "odd-lot-scan-latest.json").write_text(json_text, encoding="utf-8")

    md_text = _markdown_report(
        opportunities,
        as_of_date=as_of_date,
        source_label=source_label,
        voluntary_ca_fee=voluntary_ca_fee,
        buy_commission=buy_commission,
        slippage_bps=slippage_bps,
        max_shares=max_shares,
        quote_sources=quote_sources,
    )
    md_path = output_dir / f"odd-lot-scan-{timestamp}.md"
    md_path.write_text(md_text, encoding="utf-8")
    (output_dir / "odd-lot-scan-latest.md").write_text(md_text, encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.input_dir is None and not args.edgar:
        raise SystemExit("Please specify --input-dir and/or --edgar.")

    eval_date = date.fromisoformat(args.as_of_date) if args.as_of_date else date.today()
    manual_quotes = parse_quotes_arg(args.quotes)

    filings: list[TenderOfferFiling] = []
    quotes: dict[str, float] = {}
    sources: list[str] = []

    if args.input_dir is not None:
        local_filings, embedded_quotes = load_local_filings(args.input_dir)
        filings.extend(local_filings)
        quotes.update(embedded_quotes)
        sources.append(f"input_dir:{args.input_dir}")

    if args.edgar:
        forms = tuple(f.strip() for f in args.forms.split(",") if f.strip())
        edgar_filings = fetch_recent_edgar_tender_filings(
            args.user_agent,
            days_back=args.days,
            forms=forms,
            max_results=args.limit,
            query=args.query.strip() or None,
            listed_only=args.listed_only,
        )
        filings.extend(edgar_filings)
        sources.append("edgar")

    quotes.update(manual_quotes)
    quote_sources: dict[str, str] = {}

    if args.use_tws:
        missing_symbols = [
            f.symbol
            for f in filings
            if f.symbol and f.symbol not in quotes
        ]
        if missing_symbols:
            tws_quotes = fetch_tws_ask_quotes(
                missing_symbols,
                host=args.host,
                port=args.port,
                client_id=args.client_id,
                market_data_type=args.market_data_type,
                quote_sources=quote_sources,
            )
            for sym, ask in tws_quotes.items():
                if ask is not None:
                    quotes[sym] = ask

    opportunities = [
        evaluate_odd_lot_opportunity(
            filing,
            quotes.get(filing.symbol) if filing.symbol else None,
            as_of_date=eval_date,
            buy_commission=args.commission,
            slippage_bps=args.slippage_bps,
            voluntary_ca_fee=args.voluntary_ca_fee,
            max_shares=args.max_shares,
        )
        for filing in filings
    ]
    sorted_opps = _sort_opportunities(opportunities)

    json_path, md_path = save_scan_report(
        args.output_dir,
        sorted_opps,
        as_of_date=eval_date,
        source_label="+".join(sources),
        voluntary_ca_fee=args.voluntary_ca_fee,
        buy_commission=args.commission,
        slippage_bps=args.slippage_bps,
        max_shares=args.max_shares,
        quote_sources=quote_sources,
    )

    print(f"Scanned {len(sorted_opps)} tender offer filing(s).")
    for opp in sorted_opps:
        print(
            f"  {opp.symbol or '-':6s} | {opp.status.value:20s} | "
            f"worst_net={_format_money(opp.worst_case_net_profit)} | "
            f"flags={','.join(opp.risk_flags) or '-'}"
        )
    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {md_path}")
    print(f"Reminder: {MANUAL_EXECUTION_REMINDER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
