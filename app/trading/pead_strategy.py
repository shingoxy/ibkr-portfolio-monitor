from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from math import isfinite
from typing import Any, Sequence

from pydantic import BaseModel, Field, field_validator

from app.trading.models import Bar


class EarningsSession(str, Enum):
    """Official release timing session for an earnings announcement."""

    BMO = "BMO"
    AMC = "AMC"


class EarningsEvent(BaseModel):
    """Point-in-time earnings event with actual vs consensus estimates."""

    symbol: str
    announcement_date: date
    session: EarningsSession
    eps_actual: float
    eps_estimate: float
    revenue_actual: float | None = None
    revenue_estimate: float | None = None
    source: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> str:
        text = str(value or "").strip().upper()
        if not text:
            raise ValueError("symbol must not be empty")
        return text

    @field_validator("session", mode="before")
    @classmethod
    def _normalize_session(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("eps_actual", "eps_estimate")
    @classmethod
    def _finite_required_float(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("EPS values must be finite numbers")
        return value

    @field_validator("revenue_actual", "revenue_estimate")
    @classmethod
    def _finite_optional_float(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("revenue values must be finite numbers")
        return value

    @property
    def eps_surprise_pct(self) -> float | None:
        """Compute EPS surprise using abs(estimate); returns None if estimate is 0."""
        if abs(self.eps_estimate) <= 1e-12:
            return None
        return (self.eps_actual - self.eps_estimate) / abs(self.eps_estimate)

    @property
    def revenue_surprise_pct(self) -> float | None:
        """Compute revenue surprise using abs(estimate); returns None if missing or 0."""
        if (
            self.revenue_actual is None
            or self.revenue_estimate is None
            or abs(self.revenue_estimate) <= 1e-12
        ):
            return None
        return (self.revenue_actual - self.revenue_estimate) / abs(self.revenue_estimate)


@dataclass(frozen=True)
class PEADConfig:
    """Configuration for long-only PEAD candidate filtering and holding rules."""

    min_eps_surprise_pct: float = 0.05
    min_revenue_surprise_pct: float = 0.0
    min_gap_pct: float = 0.02
    max_gap_pct: float = 0.25
    min_avg_dollar_volume: float = 10_000_000.0
    min_price: float = 5.0
    spy_regime_window: int = 200
    holding_days: int = 10
    stop_loss_pct: float = 0.08
    dollar_volume_window: int = 20
    volume_multiplier: float = 1.0
    require_spy_regime: bool = False
    require_revenue_data: bool = False

    def __post_init__(self) -> None:
        if self.min_gap_pct > self.max_gap_pct:
            raise ValueError("min_gap_pct must be <= max_gap_pct")
        if self.min_avg_dollar_volume < 0:
            raise ValueError("min_avg_dollar_volume must be non-negative")
        if self.min_price < 0:
            raise ValueError("min_price must be non-negative")
        if self.spy_regime_window < 0:
            raise ValueError("spy_regime_window must be non-negative")
        if self.holding_days < 1:
            raise ValueError("holding_days must be at least 1")
        if not 0.0 <= self.stop_loss_pct < 1.0:
            raise ValueError("stop_loss_pct must be in [0.0, 1.0)")
        if self.dollar_volume_window < 1:
            raise ValueError("dollar_volume_window must be at least 1")
        if self.volume_multiplier <= 0:
            raise ValueError("volume_multiplier must be positive")


class PEADCandidate(BaseModel):
    """Accepted PEAD trade candidate with strictly pre-entry metrics and entry open."""

    symbol: str
    announcement_date: date
    session: EarningsSession
    entry_date: date
    prev_date: date
    entry_open: float
    prev_close: float
    gap_pct: float
    eps_surprise_pct: float
    revenue_surprise_pct: float | None = None
    avg_dollar_volume: float
    spy_prior_close: float | None = None
    spy_sma: float | None = None
    source: str = ""
    used_trades_prices: bool = False
    score: float = 0.0


class PEADCandidateEvaluation(BaseModel):
    """Structured result from evaluating one earnings event."""

    accepted: bool
    symbol: str
    announcement_date: date
    session: EarningsSession
    entry_date: date | None = None
    candidate: PEADCandidate | None = None
    rejection_reason: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


def resolve_entry_bar_index(bars: Sequence[Bar], event: EarningsEvent) -> int | None:
    """Resolve the first tradable bar index after an earnings release.

    BMO releases can trade starting at the open on ``bar.date >= announcement_date``.
    AMC releases can trade only starting at the open on ``bar.date > announcement_date``.
    Never uses a prior close as the fill bar.
    """
    best_idx: int | None = None
    best_date: date | None = None
    for idx, bar in enumerate(bars):
        qualifies = (
            bar.date >= event.announcement_date
            if event.session == EarningsSession.BMO
            else bar.date > event.announcement_date
        )
        if qualifies and (best_date is None or bar.date < best_date):
            best_date = bar.date
            best_idx = idx
    return best_idx


def evaluate_pead_candidate(
    event: EarningsEvent,
    bars: Sequence[Bar],
    spy_bars: Sequence[Bar] | None = None,
    config: PEADConfig | None = None,
    trades_bars: Sequence[Bar] | None = None,
) -> PEADCandidateEvaluation:
    """Evaluate a PEAD candidate with zero lookahead.

    Liquidity, minimum price, prior close for opening gap, and SPY regime SMA
    strictly use bars with ``bar.date < entry_date``. Only ``entry_bar.open`` is
    used from the entry date for the opening gap check.
    """
    config = config or PEADConfig()
    sorted_bars = sorted(bars, key=lambda bar: bar.date)
    reasons: list[str] = []
    details: dict[str, Any] = {"source": event.source}

    eps_surprise = event.eps_surprise_pct
    if eps_surprise is None:
        reasons.append("invalid_eps_estimate")
    else:
        details["eps_surprise_pct"] = round(eps_surprise, 6)
        if eps_surprise < config.min_eps_surprise_pct:
            reasons.append("eps_surprise_below_threshold")

    rev_surprise = event.revenue_surprise_pct
    if event.revenue_actual is not None and event.revenue_estimate is not None:
        if rev_surprise is None:
            reasons.append("invalid_revenue_estimate")
        else:
            details["revenue_surprise_pct"] = round(rev_surprise, 6)
            if rev_surprise < config.min_revenue_surprise_pct:
                reasons.append("revenue_surprise_below_threshold")
    elif config.min_revenue_surprise_pct > 0.0 or config.require_revenue_data:
        reasons.append("missing_revenue_data")

    entry_idx = resolve_entry_bar_index(sorted_bars, event)
    if entry_idx is None:
        reasons.append("no_entry_bar")
        return _reject(event, reasons, details)

    entry_bar = sorted_bars[entry_idx]
    entry_date = entry_bar.date
    details["entry_date"] = entry_date.isoformat()

    prior_bars = [bar for bar in sorted_bars if bar.date < entry_date]
    if not prior_bars:
        reasons.append("no_prior_bar")
        return _reject(event, reasons, details, entry_date=entry_date)

    prev_bar = prior_bars[-1]
    prev_date = prev_bar.date
    entry_open = entry_bar.open
    prev_close = prev_bar.close
    liquidity_bars = prior_bars[-config.dollar_volume_window :]
    used_trades = False

    if trades_bars is not None:
        sorted_trades = sorted(trades_bars, key=lambda bar: bar.date)
        trades_by_date = {bar.date: bar for bar in sorted_trades}
        trades_entry = trades_by_date.get(entry_date)
        trades_prior = [bar for bar in sorted_trades if bar.date < entry_date]
        if trades_entry is None or not trades_prior:
            reasons.append("missing_trades_bar")
            return _reject(event, reasons, details, entry_date=entry_date)
        trades_prev = trades_prior[-1]
        entry_open = trades_entry.open
        prev_close = trades_prev.close
        prev_date = trades_prev.date
        if len(trades_prior) >= len(liquidity_bars):
            liquidity_bars = trades_prior[-config.dollar_volume_window :]
        used_trades = True

    avg_dollar_volume = (
        sum(bar.close * bar.volume * config.volume_multiplier for bar in liquidity_bars)
        / len(liquidity_bars)
    )
    gap_pct = entry_open / prev_close - 1.0

    details.update(
        {
            "prev_date": prev_date.isoformat(),
            "prev_close": round(prev_close, 6),
            "entry_open": round(entry_open, 6),
            "gap_pct": round(gap_pct, 6),
            "avg_dollar_volume": round(avg_dollar_volume, 2),
            "used_trades_prices": used_trades,
        }
    )

    if prev_close < config.min_price:
        reasons.append("price_below_min")
    if avg_dollar_volume < config.min_avg_dollar_volume:
        reasons.append("illiquid_avg_dollar_volume")
    if gap_pct < config.min_gap_pct:
        reasons.append("gap_below_min")
    if gap_pct > config.max_gap_pct:
        reasons.append("gap_above_max")

    spy_prior_close: float | None = None
    spy_sma: float | None = None
    if config.spy_regime_window > 0:
        if spy_bars is None:
            if config.require_spy_regime:
                reasons.append("missing_spy_bars")
        else:
            prior_spy = sorted(
                (bar for bar in spy_bars if bar.date < entry_date),
                key=lambda bar: bar.date,
            )
            if len(prior_spy) < config.spy_regime_window:
                reasons.append("insufficient_spy_history")
            else:
                spy_window = [bar.close for bar in prior_spy[-config.spy_regime_window :]]
                spy_sma = sum(spy_window) / len(spy_window)
                spy_prior_close = prior_spy[-1].close
                details["spy_prior_close"] = round(spy_prior_close, 6)
                details["spy_sma"] = round(spy_sma, 6)
                if spy_prior_close <= spy_sma:
                    reasons.append("spy_below_regime_sma")

    if reasons:
        return _reject(event, reasons, details, entry_date=entry_date)

    assert eps_surprise is not None
    candidate = PEADCandidate(
        symbol=event.symbol,
        announcement_date=event.announcement_date,
        session=event.session,
        entry_date=entry_date,
        prev_date=prev_date,
        entry_open=entry_open,
        prev_close=prev_close,
        gap_pct=round(gap_pct, 6),
        eps_surprise_pct=round(eps_surprise, 6),
        revenue_surprise_pct=round(rev_surprise, 6) if rev_surprise is not None else None,
        avg_dollar_volume=round(avg_dollar_volume, 2),
        spy_prior_close=round(spy_prior_close, 6) if spy_prior_close is not None else None,
        spy_sma=round(spy_sma, 6) if spy_sma is not None else None,
        source=event.source,
        used_trades_prices=used_trades,
        score=round(eps_surprise, 6),
    )
    return PEADCandidateEvaluation(
        accepted=True,
        symbol=event.symbol,
        announcement_date=event.announcement_date,
        session=event.session,
        entry_date=entry_date,
        candidate=candidate,
        details=details,
    )


def _reject(
    event: EarningsEvent,
    reasons: list[str],
    details: dict[str, Any],
    entry_date: date | None = None,
) -> PEADCandidateEvaluation:
    return PEADCandidateEvaluation(
        accepted=False,
        symbol=event.symbol,
        announcement_date=event.announcement_date,
        session=event.session,
        entry_date=entry_date,
        candidate=None,
        rejection_reason=reasons[0],
        rejection_reasons=reasons,
        details=details,
    )
