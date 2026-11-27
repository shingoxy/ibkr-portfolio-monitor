"""Read-only US odd-lot tender offer parser, evaluator, and SEC EDGAR scanner."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, timedelta
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

import httpx
from pydantic import BaseModel, Field

from app.ibkr.client import IBKRClient
from app.ibkr.contracts import make_stock_contract

logger = logging.getLogger(__name__)

SETTLEMENT_DAYS = 3
DEFAULT_EDGAR_FORMS: tuple[str, ...] = ("SC TO-I", "SC TO-T", "SC 13E3")
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
EDGAR_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{adsh_nodash}/{filename}"
EDGAR_INDEX_JSON_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{adsh_nodash}/index.json"

MANUAL_EXECUTION_REMINDER = (
    "READ-ONLY SCANNER: Verify terms in the full SEC filing and confirm your account's "
    "Voluntary Corporate Action fee schedule; if net spread remains attractive, buy <= 99 "
    "shares manually and submit your tender election in IBKR Client Portal -> Corporate Actions."
)

_BLOCK_TAGS = frozenset({
    "p", "div", "br", "hr", "tr", "td", "th", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "header", "footer", "blockquote", "pre", "table", "thead", "tbody",
})
_SKIP_TAGS = frozenset({"script", "style", "noscript"})

_MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_NAMES_RE = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
_TEXT_DATE_RE = re.compile(
    rf"\b({_MONTH_NAMES_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")

_EXPIRATION_BEFORE_RE = re.compile(
    r"(?:expire[sd]?|expiring|expiration\s+(?:date|time)|deadline|valid\s+(?:until|through)|"
    r"ends?\s+(?:at|on)|tender\s+deadline|5:00\s*p\.?m\.?|12:00\s*midnight|midnight)"
    r"(?:[^.\n]|\.(?=\S)|p\.m\.|a\.m\.|E\.T\.|N\.Y\.){0,120}?"
    rf"((?:{_MONTH_NAMES_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}|20\d{{2}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/20\d{{2}})",
    re.IGNORECASE,
)
_EXPIRATION_AFTER_RE = re.compile(
    rf"((?:{_MONTH_NAMES_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}|20\d{{2}}-\d{{2}}-\d{{2}})"
    r"\s*(?:\([^)]*expiration[^)]*\)|[^.\n]{0,30}\bexpiration\s+date\b)",
    re.IGNORECASE,
)

_PRICE_NUM = r"(?:US\s*)?\$(\d{1,3}(?:,\d{3})*(?:\.\d{1,4})?|\d+(?:\.\d{1,4})?)"
_PER_SHARE_OPT = r"(?:\s+(?:net\s+)?(?:in\s+cash\s+)?per\s+(?:common\s+)?share)?"
_SCALE_SUFFIX_RE = re.compile(
    r"^\s*(?:million|billion|thousand|m\b|b\b|shares\b|in\s+value\b|aggregate\b|of\s+(?:shares|common|our))",
    re.IGNORECASE,
)
_BAD_PRICE_CONTEXT_BEFORE_RE = re.compile(
    r"(?:par\s+value(?:\s+of)?|closing(?:\s+sale)?\s+prices?(?:\s+of)?|"
    r"last\s+reported\s+sale\s+price(?:\s+of)?|market\s+prices?(?:\s+of)?|"
    r"52-week\s+(?:high|low)(?:\s+of)?|traded(?:\s+at)?|ranged(?:\s+from)?|fluctuated(?:\s+from)?|"
    r"exercise\s+price(?:\s+of)?|conversion\s+price(?:\s+of)?|"
    r"purchased\s+[\d,]+\s+shares\s+at\s+prices?(?:\s+between)?|"
    r"open[\s-]market\s+(?:purchases?|transactions?)(?:\s+at\s+prices?)?|"
    r"historical\s+(?:purchase|repurchase)\s+prices?(?:\s+of)?|at\s+prices\s+between|"
    r"aggregate\s+(?:purchase\s+)?price(?:\s+of)?|up\s+to\s+an\s+aggregate(?:\s+of)?)\s*$",
    re.IGNORECASE,
)

_DUTCH_NOT_GREATER_RE = re.compile(
    rf"(?:not\s+(?:greater|more)\s+than|no\s+more\s+than)\s+{_PRICE_NUM}{_PER_SHARE_OPT}\s*,?\s*"
    rf"(?:(?:and\s+)?not\s+less\s+than|nor\s+less\s+than|or\s+less\s+than)\s+{_PRICE_NUM}",
    re.IGNORECASE,
)
_DUTCH_NOT_LESS_RE = re.compile(
    rf"(?:not\s+less\s+than|at\s+least)\s+{_PRICE_NUM}{_PER_SHARE_OPT}\s*,?\s*"
    rf"(?:(?:and|nor|or)\s+not\s+(?:greater|more)\s+than|(?:nor|or)\s+(?:greater|more)\s+than)\s+{_PRICE_NUM}",
    re.IGNORECASE,
)
_DUTCH_BETWEEN_RE = re.compile(
    rf"\bbetween\s+{_PRICE_NUM}{_PER_SHARE_OPT}\s+and\s+{_PRICE_NUM}(?:\s+per\s+share)?",
    re.IGNORECASE,
)
_DUTCH_TO_PER_SHARE_RE = re.compile(
    rf"(?:from\s+)?{_PRICE_NUM}{_PER_SHARE_OPT}\s+(?:to|[-–—])\s+{_PRICE_NUM}\s+(?:net\s+)?(?:in\s+cash\s+)?per\s+(?:common\s+)?share",
    re.IGNORECASE,
)
_DUTCH_PRICE_TO_RE = re.compile(
    rf"(?:price(?:\s+range)?|purchase\s+price|offer\s+price|tender\s+price|at)\s+(?:of\s+|from\s+)?"
    rf"{_PRICE_NUM}{_PER_SHARE_OPT}\s+(?:to|[-–—])\s+{_PRICE_NUM}",
    re.IGNORECASE,
)
_DUTCH_GENERIC_TO_RE = re.compile(
    rf"{_PRICE_NUM}\s+to\s+{_PRICE_NUM}",
    re.IGNORECASE,
)

_FIXED_OFFER_PATTERNS = (
    re.compile(
        rf"(?:purchase|offer|tender|cash|merger)\s+price\s+(?:of|is|equal\s+to|:)?\s*{_PRICE_NUM}"
        rf"(?:\s+(?:net\s+to\s+(?:the\s+)?seller\s+)?(?:in\s+cash\s+)?per\s+(?:common\s+)?share)?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:at|for)\s+(?:a\s+price\s+of\s+)?{_PRICE_NUM}\s+"
        rf"(?:net\s+(?:to\s+(?:the\s+)?seller\s+)?)?(?:in\s+cash\s+)?per\s+(?:common\s+)?share",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_PRICE_NUM}\s+(?:net\s+)?(?:in\s+cash\s+)?per\s+(?:common\s+)?share",
        re.IGNORECASE,
    ),
)

_ODD_LOT_PATTERNS = (
    re.compile(r"\bodd[\s-]?lot\s+(?:priority|preference|exemption|provision|holder[s]?|tender[s]?)\b", re.IGNORECASE),
    re.compile(r"\bodd[\s-]?lots?\b", re.IGNORECASE),
    re.compile(r"\b(?:fewer|less)\s+than\s+(?:one\s+hundred\s*\(\s*100\s*\)|100)\s+shares\b", re.IGNORECASE),
    re.compile(
        r"\b(?:up\s+to\s+(?:and\s+including\s+)?)?99\s+(?:or\s+(?:fewer|less)\s+)?shares\b|"
        r"\b99\s+shares\s+or\s+(?:fewer|less)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnot\s+(?:be\s+)?subject\s+to\s+proration\b|\bexempt\s+from\s+proration\b|\bwithout\s+proration\b", re.IGNORECASE),
)

_ODD_LOT_DOC_DENIAL_RE = re.compile(
    r"(?:"
    r"\b(?:no|without|never|neither)\b[^.;?\n]{0,40}\bodd[\s-]?lot\s+(?:priority|preference|exemption)\b|"
    r"\bdoes\s+not\s+(?:provide|offer|include|grant|contain)\b[^.;?\n]{0,50}\bodd[\s-]?lot|"
    r"\b(?:will|does|do)\s+not\s+(?:have|contain|include|provide)\s+(?:any\s+)?(?:special\s+)?pro[\s-]?ration\s+(?:provision|preference|priority)\s+for\s+odd[\s-]?lot|"
    r"\bodd[\s-]?lot(?:\s+(?:priority|preference|exemption))?\s+(?:is\s+|will\s+)?(?:not\s+available|does\s+not\s+apply|will\s+not\s+apply|not\s+applicable|excluded)\b|"
    r"\ball\s+odd[\s-]?lot\s+tenders\b[^.?\n]{0,90}\bare\s+(?:also\s+)?subject\s+to\s+proration\b|"
    r"\bodd[\s-]?lot\s+tenders\s+to\s+be\s+subject\s+to\s+(?:the\s+same\s+)?proration\b"
    r")",
    re.IGNORECASE,
)

_ODD_LOT_NEGATION_RE = re.compile(
    r"(?:"
    r"\b(?:no|not|without|never|neither)\b[^.;?\n]{0,40}\b(?:odd[\s-]?lot\s+(?:priority|preference|exemption)|odd[\s-]?lot\s+preference)\b|"
    r"\bdoes\s+not\s+(?:provide|offer|include|grant|contain)\b[^.;?\n]{0,50}\bodd[\s-]?lot|"
    r"\b(?:will|does|do)\s+not\s+(?:have|contain|include|provide)\s+(?:any\s+)?(?:special\s+)?pro[\s-]?ration\s+(?:provision|preference|priority)\s+for\s+odd[\s-]?lot|"
    r"\bodd[\s-]?lot(?:\s+(?:priority|preference|exemption))?\s+(?:is\s+|will\s+)?(?:not\s+available|does\s+not\s+apply|will\s+not\s+apply|not\s+applicable|excluded)\b|"
    r"\b(?:odd[\s-]?lots?(?:\s+tenders?)?|fewer\s+than\s+100\s+shares|less\s+than\s+100\s+shares|99\s+(?:or\s+fewer\s+)?shares)\b[^.;?\n]{0,90}"
    r"\b(?:to\s+be\s+subject\s+to\s+(?:the\s+same\s+)?proration|(?:will|shall|may|are)\s+(?:also\s+)?(?:be\s+)?(?:subject\s+to\s+(?:the\s+same\s+)?proration|prorated))\b|"
    r"\bnot\s+exempt\s+from\s+proration\b"
    r")",
    re.IGNORECASE,
)

_MAX_SHARES_LT_RE = re.compile(r"\b(?:fewer|less)\s+than\s+(\d{1,4})\s+shares\b", re.IGNORECASE)
_MAX_SHARES_LE_RE = re.compile(r"\b(?:up\s+to\s+)?(\d{1,4})\s+(?:or\s+fewer\s+)?shares(?:\s+or\s+(?:fewer|less))?\b", re.IGNORECASE)

_TICKER_IN_TEXT_RE = re.compile(
    r"(?:NYSE(?:\s+American)?|NASDAQ|Nasdaq|AMEX|Ticker(?:\s+Symbol)?|Trading\s+Symbol)\s*[:\-]\s*([A-Z]{1,5}(?:\.[A-Z])?)\b"
)

_FINANCING_RISK_RE = re.compile(
    r"\b(?:subject\s+to\s+(?:the\s+receipt\s+of\s+)?financing|"
    r"conditioned\s+(?:on|upon)\s+(?:obtaining\s+|receipt\s+of\s+)?financing|"
    r"financing\s+condition|financing\s+contingency|financing\s+risk|"
    r"receipt\s+of\s+(?:sufficient\s+)?financing|obtaining\s+(?:sufficient\s+)?financing)\b",
    re.IGNORECASE,
)
_FINANCING_NEGATED_RE = re.compile(
    r"\b(?:not\s+(?:conditioned|contingent)\s+(?:on|upon)\s+(?:any\s+)?financing|"
    r"not\s+subject\s+to\s+(?:any\s+)?financing(?:\s+condition)?|"
    r"no\s+financing\s+condition|without\s+(?:any\s+)?financing\s+condition)\b",
    re.IGNORECASE,
)

_WITHDRAWAL_RISK_RE = re.compile(
    r"\b(?:(?:we|company|offeror|purchaser|issuer)\s+may\s+(?:withdraw|terminate|cancel)\s+the\s+offer|"
    r"right\s+to\s+(?:withdraw|terminate|cancel)(?:\s+the\s+offer)?|"
    r"reserve[sd]?\s+the\s+right\s+to\s+(?:withdraw|terminate|cancel)|"
    r"withdraw(?:al)?\s+(?:of\s+the\s+offer|risk|clause|condition)|"
    r"terminate\s+the\s+offer|termination\s+(?:condition|right|of\s+the\s+offer)|"
    r"material\s+adverse\s+(?:change|effect))\b",
    re.IGNORECASE,
)

_PRORATION_TERM_RE = re.compile(r"\b(?:proration|prorate[sd]?|pro\s+rata\s+(?:basis|allocation|purchase))\b", re.IGNORECASE)
_PRORATION_ODD_LOT_EXEMPT_RE = re.compile(
    r"\b(?:not\s+(?:be\s+)?subject\s+to\s+proration|exempt\s+from\s+proration|without\s+proration|before\s+(?:any\s+)?proration)\b",
    re.IGNORECASE,
)
_ODD_LOT_PRORATED_RE = re.compile(
    r"\b(?:odd[\s-]?lots?|fewer\s+than\s+100\s+shares|less\s+than\s+100\s+shares|99\s+(?:or\s+fewer\s+)?shares)\b[^.;\n]{0,70}"
    r"\b(?:subject\s+to\s+proration|may\s+be\s+prorated|will\s+be\s+prorated|prorated)\b",
    re.IGNORECASE,
)


class OpportunityStatus(str, Enum):
    ACTIONABLE_REVIEW = "ACTIONABLE_REVIEW"
    LOW_OR_NEGATIVE_NET = "LOW_OR_NEGATIVE_NET"
    NEEDS_QUOTE = "NEEDS_QUOTE"
    NO_ODD_LOT_PRIORITY = "NO_ODD_LOT_PRIORITY"
    EXPIRED = "EXPIRED"


class TenderOfferFiling(BaseModel):
    """Structured representation of a tender offer filing."""

    symbol: str = ""
    company_name: str = ""
    cik: str = ""
    form_type: str = ""
    accession_number: str = ""
    filing_date: date | None = None
    filing_url: str = ""
    source_path: str = ""
    title: str = ""
    offer_type: str = "unknown"  # "dutch_auction" | "fixed_price" | "unknown"
    min_offer_price: float | None = None
    max_offer_price: float | None = None
    odd_lot_priority: bool = False
    odd_lot_max_shares: int = 99
    expiration_date: date | None = None
    expiration_text: str = ""
    proration_risk: bool = False
    withdrawal_risk: bool = False
    financing_risk: bool = False
    risk_terms: list[str] = Field(default_factory=list)
    evidence_snippets: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class OddLotOpportunity(BaseModel):
    """Evaluated read-only odd-lot tender offer economics and review status."""

    symbol: str = ""
    company_name: str = ""
    form_type: str = ""
    filing_url: str = ""
    source_path: str = ""
    offer_type: str = "unknown"
    odd_lot_priority: bool = False
    max_shares: int = 99
    min_offer_price: float | None = None
    max_offer_price: float | None = None
    ask_price: float | None = None
    effective_entry_price: float | None = None
    buy_commission: float = 1.0
    slippage_bps: float = 10.0
    voluntary_ca_fee: float = 0.0
    capital_required: float | None = None
    worst_case_gross_proceeds: float | None = None
    best_case_gross_proceeds: float | None = None
    worst_case_net_profit: float | None = None
    best_case_net_profit: float | None = None
    worst_case_return_pct: float | None = None
    best_case_return_pct: float | None = None
    holding_days: int | None = None
    worst_case_annualized_pct: float | None = None
    best_case_annualized_pct: float | None = None
    expiration_date: date | None = None
    as_of_date: date
    status: OpportunityStatus
    risk_flags: list[str] = Field(default_factory=list)
    evidence_snippets: list[str] = Field(default_factory=list)
    manual_action_reminder: str = MANUAL_EXECUTION_REMINDER

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class _VisibleTextHTMLParser(HTMLParser):
    """Extract readable plain text from HTML using stdlib html.parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth == 0 and tag_lower in _BLOCK_TAGS:
            self._chunks.append("\n" if tag_lower in {"p", "div", "br", "hr", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"} else " ")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth == 0 and tag_lower in _BLOCK_TAGS:
            self._chunks.append("\n" if tag_lower in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"} else " ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        raw = raw.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in raw.split("\n")]
        cleaned_lines = [line for line in lines if line]
        return "\n".join(cleaned_lines)


def strip_html_to_text(raw_text: str) -> str:
    """Strip HTML tags and decode entities cleanly via stdlib html.parser."""
    if not raw_text:
        return ""
    if not re.search(r"<(?:/?[a-zA-Z][a-zA-Z0-9:_-]*(?:\s[^>]*)?>|!DOCTYPE\b)", raw_text):
        normalized = raw_text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in normalized.split("\n")]
        return "\n".join(line for line in lines if line)

    parser = _VisibleTextHTMLParser()
    parser.feed(raw_text)
    parser.close()
    return parser.get_text()


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _make_snippet(text: str, start: int, end: int, *, label: str = "", radius: int = 85) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    if left > 0:
        space_idx = text.find(" ", left)
        if 0 < space_idx < start:
            left = space_idx + 1
    if right < len(text):
        space_idx = text.rfind(" ", end, right)
        if space_idx > end:
            right = space_idx
    excerpt = text[left:right].strip()
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    core = f"{prefix}{excerpt}{suffix}"
    return f"{label}: {core}" if label else core


def _parse_dollar(raw: str) -> float | None:
    try:
        val = float(raw.replace(",", ""))
    except ValueError:
        return None
    if val <= 0 or val > 50_000:
        return None
    return val


def _is_valid_price_match(text: str, start: int, end: int, val: float | None, *, range_start: int | None = None) -> bool:
    if val is None:
        return False
    after = text[end : end + 35]
    if _SCALE_SUFFIX_RE.match(after):
        return False
    ctx_pos = range_start if range_start is not None else start
    before = text[max(0, ctx_pos - 70) : ctx_pos]
    if _BAD_PRICE_CONTEXT_BEFORE_RE.search(before):
        return False
    return True


def _match_offer_prices(text: str) -> tuple[str, float | None, float | None, str | None]:
    """Return (offer_type, min_price, max_price, snippet) from a text slice."""
    explicit_dutch_patterns = (
        _DUTCH_NOT_GREATER_RE,
        _DUTCH_NOT_LESS_RE,
        _DUTCH_BETWEEN_RE,
        _DUTCH_TO_PER_SHARE_RE,
        _DUTCH_PRICE_TO_RE,
    )
    for pattern in explicit_dutch_patterns:
        for match in pattern.finditer(text):
            p1 = _parse_dollar(match.group(1))
            p2 = _parse_dollar(match.group(2))
            if not (
                _is_valid_price_match(text, match.start(1), match.end(1), p1, range_start=match.start())
                and _is_valid_price_match(text, match.start(2), match.end(2), p2, range_start=match.start())
            ):
                continue
            assert p1 is not None and p2 is not None
            low = min(p1, p2)
            high = max(p1, p2)
            snippet = _make_snippet(text, match.start(), match.end(), label="offer_price")
            if abs(high - low) < 1e-9:
                return "fixed_price", low, high, snippet
            return "dutch_auction", low, high, snippet

    for pattern in _FIXED_OFFER_PATTERNS:
        for match in pattern.finditer(text):
            price = _parse_dollar(match.group(1))
            if not _is_valid_price_match(text, match.start(1), match.end(1), price, range_start=match.start()):
                continue
            snippet = _make_snippet(text, match.start(), match.end(), label="offer_price")
            return "fixed_price", price, price, snippet

    for match in _DUTCH_GENERIC_TO_RE.finditer(text):
        p1 = _parse_dollar(match.group(1))
        p2 = _parse_dollar(match.group(2))
        if not (
            _is_valid_price_match(text, match.start(1), match.end(1), p1, range_start=match.start())
            and _is_valid_price_match(text, match.start(2), match.end(2), p2, range_start=match.start())
        ):
            continue
        assert p1 is not None and p2 is not None
        low = min(p1, p2)
        high = max(p1, p2)
        snippet = _make_snippet(text, match.start(), match.end(), label="offer_price")
        if abs(high - low) < 1e-9:
            return "fixed_price", low, high, snippet
        return "dutch_auction", low, high, snippet

    return "unknown", None, None, None


def _parse_offer_prices(text: str) -> tuple[str, float | None, float | None, str | None]:
    """Prefer cover/summary offer prices before scanning deeper historical tables."""
    if len(text) > 6000:
        cover_match = _match_offer_prices(text[:6000])
        if cover_match[1] is not None:
            return cover_match
    return _match_offer_prices(text)


def _parse_date_token(token: str) -> date | None:
    cleaned = token.strip()
    m_iso = _ISO_DATE_RE.fullmatch(cleaned)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            return None

    m_text = _TEXT_DATE_RE.fullmatch(cleaned)
    if m_text:
        month_num = _MONTH_MAP.get(m_text.group(1).lower())
        if month_num is None:
            return None
        try:
            return date(int(m_text.group(3)), month_num, int(m_text.group(2)))
        except ValueError:
            return None

    m_slash = _SLASH_DATE_RE.fullmatch(cleaned)
    if m_slash:
        try:
            return date(int(m_slash.group(3)), int(m_slash.group(1)), int(m_slash.group(2)))
        except ValueError:
            return None
    return None


def _parse_expiration_date(text: str) -> tuple[date | None, str, str | None]:
    for match in _EXPIRATION_BEFORE_RE.finditer(text):
        parsed = _parse_date_token(match.group(1))
        if parsed is not None:
            return parsed, match.group(1).strip(), _make_snippet(text, match.start(), match.end(), label="expiration")

    for match in _EXPIRATION_AFTER_RE.finditer(text):
        parsed = _parse_date_token(match.group(1))
        if parsed is not None:
            return parsed, match.group(1).strip(), _make_snippet(text, match.start(), match.end(), label="expiration")

    return None, "", None


def _detect_odd_lot_priority(text: str) -> tuple[bool, int, str | None]:
    if _ODD_LOT_DOC_DENIAL_RE.search(text):
        return False, 99, None

    positive_hit: re.Match[str] | None = None
    for pattern in _ODD_LOT_PATTERNS:
        for match in pattern.finditer(text):
            win_start = max(0, match.start() - 60)
            win_end = min(len(text), match.end() + 90)
            window = text[win_start:win_end]
            if _ODD_LOT_NEGATION_RE.search(window):
                continue
            positive_hit = match
            break
        if positive_hit is not None:
            break

    if positive_hit is None:
        return False, 99, None

    max_shares = 99
    win = text[max(0, positive_hit.start() - 60) : min(len(text), positive_hit.end() + 80)]
    m_lt = _MAX_SHARES_LT_RE.search(win)
    m_le = _MAX_SHARES_LE_RE.search(win)
    if m_lt:
        n = int(m_lt.group(1))
        if 1 < n <= 999:
            max_shares = n - 1
    elif m_le:
        n = int(m_le.group(1))
        if 1 <= n <= 999:
            max_shares = n

    snippet = _make_snippet(text, positive_hit.start(), positive_hit.end(), label="odd_lot")
    return True, max_shares, snippet


def _detect_risk_terms(text: str, odd_lot_priority: bool) -> tuple[bool, bool, bool, list[str], list[str]]:
    """Return (withdrawal_risk, financing_risk, proration_risk, risk_terms, snippets)."""
    risk_terms: list[str] = []
    snippets: list[str] = []

    withdrawal_risk = False
    m_withdraw = _WITHDRAWAL_RISK_RE.search(text)
    if m_withdraw:
        withdrawal_risk = True
        risk_terms.append("withdrawal_risk")
        snippets.append(_make_snippet(text, m_withdraw.start(), m_withdraw.end(), label="risk_withdrawal"))

    financing_risk = False
    for m_fin in _FINANCING_RISK_RE.finditer(text):
        win = text[max(0, m_fin.start() - 40) : min(len(text), m_fin.end() + 40)]
        if _FINANCING_NEGATED_RE.search(win):
            continue
        financing_risk = True
        risk_terms.append("financing_risk")
        snippets.append(_make_snippet(text, m_fin.start(), m_fin.end(), label="risk_financing"))
        break

    proration_risk = False
    m_odd_prorated = _ODD_LOT_PRORATED_RE.search(text)
    if m_odd_prorated:
        proration_risk = True
        risk_terms.append("proration_risk")
        snippets.append(_make_snippet(text, m_odd_prorated.start(), m_odd_prorated.end(), label="risk_proration"))
    else:
        m_proration = _PRORATION_TERM_RE.search(text)
        if m_proration:
            if odd_lot_priority and _PRORATION_ODD_LOT_EXEMPT_RE.search(text):
                proration_risk = False
            elif not odd_lot_priority:
                proration_risk = True
                risk_terms.append("proration_risk")
                snippets.append(_make_snippet(text, m_proration.start(), m_proration.end(), label="risk_proration"))
            elif "proration risk" in text.lower():
                proration_risk = True
                risk_terms.append("proration_risk")
                snippets.append(_make_snippet(text, m_proration.start(), m_proration.end(), label="risk_proration"))

    return withdrawal_risk, financing_risk, proration_risk, risk_terms, snippets


def _coerce_date(value: date | str | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return _parse_date_token(str(value)) or date.fromisoformat(str(value)[:10])


def parse_tender_offer_text(
    text: str,
    symbol: str = "",
    *,
    company_name: str = "",
    cik: str = "",
    form_type: str = "",
    accession_number: str = "",
    filing_date: date | str | None = None,
    filing_url: str = "",
    source_path: str = "",
    title: str = "",
) -> TenderOfferFiling:
    """Parse raw tender offer text or HTML into a TenderOfferFiling model (pure, offline)."""
    plain = strip_html_to_text(text)
    flat = _collapse_ws(plain)

    resolved_symbol = symbol.strip().upper()
    if not resolved_symbol:
        m_sym = _TICKER_IN_TEXT_RE.search(flat)
        if m_sym:
            resolved_symbol = m_sym.group(1).upper()

    offer_type, min_price, max_price, price_snippet = _parse_offer_prices(flat)
    odd_lot_priority, odd_lot_max_shares, odd_lot_snippet = _detect_odd_lot_priority(flat)
    expiration_date, expiration_text, exp_snippet = _parse_expiration_date(flat)
    withdrawal_risk, financing_risk, proration_risk, risk_terms, risk_snippets = _detect_risk_terms(
        flat, odd_lot_priority
    )

    snippets: list[str] = []
    for item in [odd_lot_snippet, price_snippet, exp_snippet, *risk_snippets]:
        if item and item not in snippets:
            snippets.append(item)

    return TenderOfferFiling(
        symbol=resolved_symbol,
        company_name=company_name.strip(),
        cik=cik.strip(),
        form_type=form_type.strip(),
        accession_number=accession_number.strip(),
        filing_date=_coerce_date(filing_date),
        filing_url=filing_url.strip(),
        source_path=source_path.strip(),
        title=title.strip(),
        offer_type=offer_type,
        min_offer_price=min_price,
        max_offer_price=max_price,
        odd_lot_priority=odd_lot_priority,
        odd_lot_max_shares=odd_lot_max_shares,
        expiration_date=expiration_date,
        expiration_text=expiration_text,
        proration_risk=proration_risk,
        withdrawal_risk=withdrawal_risk,
        financing_risk=financing_risk,
        risk_terms=risk_terms,
        evidence_snippets=snippets,
    )


def evaluate_odd_lot_opportunity(
    filing: TenderOfferFiling,
    ask_price: float | None,
    *,
    as_of_date: date | None = None,
    buy_commission: float = 1.0,
    slippage_bps: float = 10.0,
    voluntary_ca_fee: float = 0.0,
    max_shares: int = 99,
) -> OddLotOpportunity:
    """Evaluate conservative odd-lot arbitrage economics and human-review status."""
    if buy_commission < 0:
        raise ValueError("buy_commission must be non-negative")
    if slippage_bps < 0:
        raise ValueError("slippage_bps must be non-negative")
    if voluntary_ca_fee < 0:
        raise ValueError("voluntary_ca_fee must be non-negative")
    if max_shares < 1:
        raise ValueError("max_shares must be >= 1")

    eval_date = as_of_date or date.today()
    shares = min(max_shares, filing.odd_lot_max_shares) if filing.odd_lot_max_shares > 0 else max_shares

    risk_flags: list[str] = []
    if voluntary_ca_fee == 0:
        risk_flags.append("unverified_voluntary_ca_fee")
    if filing.withdrawal_risk:
        risk_flags.append("withdrawal_risk")
    if filing.financing_risk:
        risk_flags.append("financing_risk")
    if filing.proration_risk:
        risk_flags.append("proration_risk")

    is_expired = filing.expiration_date is not None and filing.expiration_date < eval_date
    if is_expired:
        risk_flags.append("expired")
    elif filing.expiration_date is None:
        risk_flags.append("missing_expiration_date")

    if not filing.odd_lot_priority:
        risk_flags.append("no_odd_lot_priority")
    if filing.min_offer_price is None:
        risk_flags.append("missing_offer_price")

    valid_ask = ask_price if (ask_price is not None and ask_price > 0) else None
    if valid_ask is None:
        risk_flags.append("missing_ask_quote")

    effective_entry_price: float | None = None
    capital_required: float | None = None
    worst_gross: float | None = None
    best_gross: float | None = None
    worst_net: float | None = None
    best_net: float | None = None
    worst_ret_pct: float | None = None
    best_ret_pct: float | None = None
    holding_days: int | None = None
    worst_ann_pct: float | None = None
    best_ann_pct: float | None = None

    if filing.expiration_date is not None and not is_expired:
        holding_days = (filing.expiration_date - eval_date).days + SETTLEMENT_DAYS

    if valid_ask is not None and filing.min_offer_price is not None:
        effective_entry_price = round(valid_ask * (1.0 + slippage_bps / 10_000.0), 6)
        capital_required = round(effective_entry_price * shares + buy_commission, 4)
        best_offer = filing.max_offer_price if filing.max_offer_price is not None else filing.min_offer_price

        worst_gross = round(filing.min_offer_price * shares, 4)
        best_gross = round(best_offer * shares, 4)
        worst_net = round(worst_gross - capital_required - voluntary_ca_fee, 4)
        best_net = round(best_gross - capital_required - voluntary_ca_fee, 4)

        if capital_required > 0:
            worst_ret_pct = round((worst_net / capital_required) * 100.0, 4)
            best_ret_pct = round((best_net / capital_required) * 100.0, 4)
            if holding_days is not None and holding_days > 0:
                worst_ann_pct = round(worst_ret_pct * (365.0 / holding_days), 4)
                best_ann_pct = round(best_ret_pct * (365.0 / holding_days), 4)

        if worst_net <= 0:
            risk_flags.append("fee_exceeds_spread")
        if (
            filing.offer_type == "dutch_auction"
            and filing.max_offer_price is not None
            and valid_ask > filing.min_offer_price
            and valid_ask <= filing.max_offer_price
        ):
            risk_flags.append("ask_above_min_dutch_price")

    if is_expired:
        status = OpportunityStatus.EXPIRED
    elif not filing.odd_lot_priority:
        status = OpportunityStatus.NO_ODD_LOT_PRIORITY
    elif valid_ask is None or filing.min_offer_price is None:
        status = OpportunityStatus.NEEDS_QUOTE
    elif worst_net is not None and worst_net <= 0:
        status = OpportunityStatus.LOW_OR_NEGATIVE_NET
    else:
        status = OpportunityStatus.ACTIONABLE_REVIEW

    dedup_flags = list(dict.fromkeys(risk_flags))
    return OddLotOpportunity(
        symbol=filing.symbol,
        company_name=filing.company_name,
        form_type=filing.form_type,
        filing_url=filing.filing_url,
        source_path=filing.source_path,
        offer_type=filing.offer_type,
        odd_lot_priority=filing.odd_lot_priority,
        max_shares=shares,
        min_offer_price=filing.min_offer_price,
        max_offer_price=filing.max_offer_price,
        ask_price=valid_ask,
        effective_entry_price=effective_entry_price,
        buy_commission=buy_commission,
        slippage_bps=slippage_bps,
        voluntary_ca_fee=voluntary_ca_fee,
        capital_required=capital_required,
        worst_case_gross_proceeds=worst_gross,
        best_case_gross_proceeds=best_gross,
        worst_case_net_profit=worst_net,
        best_case_net_profit=best_net,
        worst_case_return_pct=worst_ret_pct,
        best_case_return_pct=best_ret_pct,
        holding_days=holding_days,
        worst_case_annualized_pct=worst_ann_pct,
        best_case_annualized_pct=best_ann_pct,
        expiration_date=filing.expiration_date,
        as_of_date=eval_date,
        status=status,
        risk_flags=dedup_flags,
        evidence_snippets=list(filing.evidence_snippets),
    )


def validate_sec_user_agent(user_agent: str) -> str:
    """Validate SEC EDGAR compliant User-Agent (AppName contact@domain.tld)."""
    cleaned = user_agent.strip()
    if len(cleaned) < 8:
        raise ValueError(
            "SEC EDGAR requires a descriptive User-Agent header with contact email "
            "(e.g. 'IBKRPortfolioMonitor research@yourdomain.com')."
        )
    lowered = cleaned.lower()
    if lowered.startswith(("mozilla/", "python-requests", "python-httpx", "curl/", "httpx/")):
        raise ValueError(
            "Generic HTTP client User-Agent is blocked by SEC EDGAR; pass 'AppName email@domain.com'."
        )
    if not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", cleaned):
        raise ValueError(
            "SEC EDGAR User-Agent must include a valid contact email address "
            "(e.g. 'IBKRPortfolioMonitor research@yourdomain.com')."
        )
    return cleaned


def _extract_ticker_and_company(display_names: Iterable[str]) -> tuple[str, str]:
    for raw in display_names:
        text = str(raw).strip()
        if not text:
            continue
        company = re.sub(r"\s*\(CIK\s+\d+\)", "", text, flags=re.IGNORECASE)
        m_ticker = re.search(r"\s*\(([A-Z]{1,5}(?:\.[A-Z])?)(?:,\s*[A-Z0-9.-]+)*\)", company)
        ticker = m_ticker.group(1) if m_ticker else ""
        if m_ticker:
            company = company[: m_ticker.start()] + company[m_ticker.end() :]
        return ticker, re.sub(r"\s+", " ", company).strip()
    return "", ""


def _build_archive_url(cik: str, accession_number: str, filename: str) -> str:
    cik_int = str(int(cik))
    adsh_nodash = accession_number.replace("-", "")
    return EDGAR_ARCHIVE_URL.format(cik_int=cik_int, adsh_nodash=adsh_nodash, filename=filename)


def _exhibit_priority(filename: str) -> int:
    name = filename.lower()
    if re.search(r"(?:ex(?:hibit)?[_-]?(?:99[_-]?)?a1(?:a|i)(?![iv])|offer)", name):
        return 0
    if re.search(r"(?:scto|sc13e3|ex(?:hibit)?[_-]?(?:99[_-]?)?a1(?:\.|_|-|$))", name):
        return 1
    return 2


def _dedupe_and_sort_edgar_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the highest-priority exhibit per accession number, newest filing date first."""
    best_by_key: dict[str, tuple[int, int, str, dict[str, Any]]] = {}
    for idx, hit in enumerate(hits):
        source = hit.get("_source", {})
        hit_id = str(hit.get("_id", ""))
        adsh = str(source.get("adsh") or (hit_id.split(":", 1)[0] if ":" in hit_id else "")).strip()
        key = adsh or hit_id or f"row-{idx}"
        filename = hit_id.split(":", 1)[1] if ":" in hit_id else ""
        priority = _exhibit_priority(filename)
        file_date = str(source.get("file_date") or "").strip()
        existing = best_by_key.get(key)
        if existing is None or (priority, idx) < (existing[0], existing[1]):
            best_by_key[key] = (priority, idx, file_date, hit)

    ordered = sorted(
        best_by_key.values(),
        key=lambda item: (item[2], -item[0], -item[1]),
        reverse=True,
    )
    return [item[3] for item in ordered]


def _dedupe_filings_by_issuer(filings: list[TenderOfferFiling]) -> list[TenderOfferFiling]:
    """Keep one filing per issuer (newest first), backfilling omitted amendment terms from older filings."""
    by_key: dict[str, TenderOfferFiling] = {}
    ordered_keys: list[str] = []

    sorted_filings = sorted(
        filings,
        key=lambda f: f.filing_date.isoformat() if f.filing_date else "",
        reverse=True,
    )
    for idx, filing in enumerate(sorted_filings):
        key = filing.cik.strip() or filing.symbol.strip() or filing.accession_number.strip() or f"filing-{idx}"
        primary = by_key.get(key)
        if primary is None:
            by_key[key] = filing.model_copy(deep=True)
            ordered_keys.append(key)
            continue

        if primary.min_offer_price is None and filing.min_offer_price is not None:
            primary.offer_type = filing.offer_type
            primary.min_offer_price = filing.min_offer_price
            primary.max_offer_price = filing.max_offer_price
        if primary.expiration_date is None and filing.expiration_date is not None:
            primary.expiration_date = filing.expiration_date
            primary.expiration_text = filing.expiration_text
        if not primary.symbol and filing.symbol:
            primary.symbol = filing.symbol
        if not primary.company_name and filing.company_name:
            primary.company_name = filing.company_name
        for snippet in filing.evidence_snippets:
            if snippet not in primary.evidence_snippets and len(primary.evidence_snippets) < 8:
                primary.evidence_snippets.append(snippet)

    return [by_key[k] for k in ordered_keys]


def fetch_recent_edgar_tender_filings(
    user_agent: str,
    days_back: int = 30,
    forms: tuple[str, ...] | list[str] = DEFAULT_EDGAR_FORMS,
    max_results: int = 20,
    *,
    query: str | None = None,
    listed_only: bool = False,
    dedupe_by_issuer: bool = True,
    rate_limit_seconds: float = 0.25,
    timeout_seconds: float = 20.0,
    http_client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[TenderOfferFiling]:
    """Fetch and parse recent tender offer filings from SEC EDGAR (read-only)."""
    ua = validate_sec_user_agent(user_agent)
    if days_back <= 0:
        raise ValueError("days_back must be positive")
    if max_results <= 0:
        raise ValueError("max_results must be positive")
    if rate_limit_seconds < 0.2:
        raise ValueError("rate_limit_seconds must be >= 0.2 for SEC EDGAR compliance")
    form_list = [f.strip() for f in forms if f.strip()]
    if not form_list:
        raise ValueError("at least one SEC form type is required")

    end_dt = date.today()
    start_dt = end_dt - timedelta(days=days_back)
    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout_seconds, headers=headers, follow_redirects=True)
    filings: list[TenderOfferFiling] = []
    cik_meta_cache: dict[str, tuple[str, str]] = {}

    def _lookup_cik_meta(cik_val: str) -> tuple[str, str]:
        if not cik_val.isdigit():
            return "", ""
        cik10 = cik_val.zfill(10)
        if cik10 in cik_meta_cache:
            return cik_meta_cache[cik10]
        sub_url = EDGAR_SUBMISSIONS_URL.format(cik10=cik10)
        sleep_fn(rate_limit_seconds)
        sub_resp = client.get(sub_url, headers=headers)
        if sub_resp.status_code == 200:
            sub_json = sub_resp.json()
            tickers = sub_json.get("tickers") or []
            sym = str(tickers[0]).upper().strip() if tickers else ""
            cname = str(sub_json.get("name") or "").strip()
            cik_meta_cache[cik10] = (sym, cname)
            return sym, cname
        cik_meta_cache[cik10] = ("", "")
        return "", ""

    try:
        fetch_size = min(100, max(max_results * 4, 50))
        params = {
            "forms": ",".join(form_list),
            "dateRange": "custom",
            "startdt": start_dt.isoformat(),
            "enddt": end_dt.isoformat(),
            "from": "0",
            "size": str(fetch_size),
        }
        if query and query.strip():
            params["q"] = query.strip()

        search_resp = client.get(EDGAR_SEARCH_URL, params=params, headers=headers)
        search_resp.raise_for_status()
        raw_hits = search_resp.json().get("hits", {}).get("hits", [])
        hits = _dedupe_and_sort_edgar_hits(raw_hits)

        fetch_limit = max_results * 2 if dedupe_by_issuer else max_results
        for hit in hits:
            if len(filings) >= fetch_limit:
                break
            source = hit.get("_source", {})
            hit_id = str(hit.get("_id", ""))
            adsh = str(source.get("adsh") or (hit_id.split(":", 1)[0] if ":" in hit_id else "")).strip()
            ciks = source.get("ciks") or []
            cik = str(ciks[0]).strip() if ciks else ""
            form_type = str(source.get("form") or "").strip()
            file_date_str = str(source.get("file_date") or "").strip()
            symbol, company_name = _extract_ticker_and_company(source.get("display_names") or [])

            if not symbol and cik.isdigit():
                looked_sym, looked_name = _lookup_cik_meta(cik)
                symbol = looked_sym or symbol
                company_name = company_name or looked_name

            if listed_only and not symbol:
                continue

            filename = hit_id.split(":", 1)[1] if ":" in hit_id else ""
            filing_url = ""
            combined_text = ""
            if cik and adsh and filename:
                filing_url = _build_archive_url(cik, adsh, filename)
                sleep_fn(rate_limit_seconds)
                doc_resp = client.get(filing_url, headers=headers)
                if doc_resp.status_code == 200:
                    combined_text = doc_resp.text

            parsed = parse_tender_offer_text(
                combined_text,
                symbol=symbol,
                company_name=company_name,
                cik=cik.zfill(10) if cik.isdigit() else cik,
                form_type=form_type,
                accession_number=adsh,
                filing_date=file_date_str or None,
                filing_url=filing_url,
            )

            # Check primary Offer-to-Purchase exhibit if cover document lacked offer terms.
            if cik and adsh and (parsed.min_offer_price is None or not parsed.odd_lot_priority):
                index_url = EDGAR_INDEX_JSON_URL.format(
                    cik_int=str(int(cik)), adsh_nodash=adsh.replace("-", "")
                )
                sleep_fn(rate_limit_seconds)
                idx_resp = client.get(index_url, headers=headers)
                if idx_resp.status_code == 200:
                    items = idx_resp.json().get("directory", {}).get("item", [])
                    exhibit_names = sorted(
                        (
                            str(item.get("name", ""))
                            for item in items
                            if re.search(
                                r"(?:ex|offer|99|a1a).*\.(?:htm|html|txt)$",
                                str(item.get("name", "")),
                                re.IGNORECASE,
                            )
                            and str(item.get("name", "")) != filename
                        ),
                        key=_exhibit_priority,
                    )
                    if exhibit_names:
                        exhibit_url = _build_archive_url(cik, adsh, exhibit_names[0])
                        sleep_fn(rate_limit_seconds)
                        ex_resp = client.get(exhibit_url, headers=headers)
                        if ex_resp.status_code == 200:
                            merged_text = f"{combined_text}\n{ex_resp.text}"
                            parsed = parse_tender_offer_text(
                                merged_text,
                                symbol=parsed.symbol or symbol,
                                company_name=company_name,
                                cik=cik.zfill(10) if cik.isdigit() else cik,
                                form_type=form_type,
                                accession_number=adsh,
                                filing_date=file_date_str or None,
                                filing_url=filing_url or exhibit_url,
                            )

            filings.append(parsed)
    finally:
        if owns_client:
            client.close()

    if dedupe_by_issuer:
        return _dedupe_filings_by_issuer(filings)[:max_results]
    return filings[:max_results]


def fetch_tws_ask_quotes(
    symbols: list[str],
    *,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 79,
    timeout_seconds: float = 5.0,
    market_data_type: int = 1,
    client: IBKRClient | None = None,
    quote_sources: dict[str, str] | None = None,
) -> dict[str, float | None]:
    """Read-only TWS snapshot quote fetcher (Ask preferred; delayed/last/close fallback)."""
    unique_symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
    if not unique_symbols:
        return {}

    fatal_codes = (
        {200, 354, 10089, 10168, 10189}
        if market_data_type in (3, 4)
        else {200, 354, 10089, 10167, 10168, 10189}
    )

    owns_client = client is None
    ib_client = client or IBKRClient()
    results: dict[str, float | None] = {sym: None for sym in unique_symbols}

    def _pick_reference(info: dict[str, Any]) -> tuple[float | None, str]:
        prefix = "delayed_" if info.get("is_delayed") else ""
        ask = info.get("ask")
        if isinstance(ask, (int, float)) and ask > 0:
            return float(ask), f"{prefix}ask"
        last = info.get("last")
        if isinstance(last, (int, float)) and last > 0:
            return float(last), f"{prefix}last_fallback"
        close = info.get("close")
        if isinstance(close, (int, float)) and close > 0:
            return float(close), f"{prefix}close_fallback"
        return None, ""

    try:
        if owns_client:
            ib_client.connect(host, port, client_id)
            threading.Thread(target=ib_client.run, daemon=True).start()
            if not ib_client.connected_event.wait(10.0):
                raise RuntimeError(f"TWS API handshake failed at {host}:{port}")

        ib_client.reqMarketDataType(market_data_type)

        for sym in unique_symbols:
            contract = make_stock_contract(sym)
            ticker_id = ib_client.allocate_ticker_id({"symbol": sym, "sec_type": "STK"})
            fallback_started_at: float | None = None
            try:
                ib_client.reqMktData(ticker_id, contract, "", True, False, [])
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() <= deadline:
                    info = ib_client.get_ticker_info(ticker_id)
                    ask = info.get("ask")
                    if isinstance(ask, (int, float)) and ask > 0:
                        results[sym] = float(ask)
                        if quote_sources is not None:
                            quote_sources[sym] = "delayed_ask" if info.get("is_delayed") else "ask"
                        break
                    last = info.get("last")
                    close = info.get("close")
                    has_fallback = (isinstance(last, (int, float)) and last > 0) or (
                        isinstance(close, (int, float)) and close > 0
                    )
                    if has_fallback:
                        if fallback_started_at is None:
                            fallback_started_at = time.monotonic()
                        elif time.monotonic() - fallback_started_at >= 1.5:
                            ref_val, ref_src = _pick_reference(info)
                            if ref_val is not None:
                                results[sym] = ref_val
                                if quote_sources is not None:
                                    quote_sources[sym] = ref_src
                                logger.warning("TWS ask unavailable for %s; using %s=%.4f", sym, ref_src, ref_val)
                            break

                    errors = ib_client.drain_errors()
                    drain_notices = getattr(ib_client, "drain_market_data_notices", None)
                    notices = drain_notices() if callable(drain_notices) else []
                    fatal = [
                        item for item in [*errors, *notices]
                        if item.get("reqId") == ticker_id
                        and item.get("errorCode") in fatal_codes
                    ]
                    if fatal:
                        logger.warning("TWS snapshot quote unavailable for %s: %s", sym, fatal)
                        break
                    time.sleep(0.1)

                if results[sym] is None:
                    ref_val, ref_src = _pick_reference(ib_client.get_ticker_info(ticker_id))
                    if ref_val is not None:
                        results[sym] = ref_val
                        if quote_sources is not None:
                            quote_sources[sym] = ref_src
                        if "fallback" in ref_src:
                            logger.warning("TWS ask unavailable for %s; using %s=%.4f", sym, ref_src, ref_val)
            finally:
                ib_client.clear_ticker(ticker_id)
    finally:
        if owns_client:
            ib_client.disconnect()

    return results
