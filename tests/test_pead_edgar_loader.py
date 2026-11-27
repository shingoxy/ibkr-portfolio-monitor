"""Tests for read-only SEC EDGAR Point-in-Time PEAD event loader and CLI."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.trading.pead_edgar_loader import (
    build_pead_events_from_sec,
    classify_ny_session,
    extract_8k_earnings_releases,
    extract_xbrl_yoy_quarterly_facts,
    fetch_pead_events_for_symbols,
    write_pead_events_csv,
)
from app.trading.pead_strategy import EarningsSession
from scripts import fetch_pead_events, run_pead_study


class TestSessionAnd8KTiming:
    def test_classify_ny_session_respects_est_edt_930_cutoff(self):
        # EDT (-04:00): 12:25Z = 08:25 NY -> BMO; 13:30Z = 09:30 NY -> AMC; 20:30Z = 16:30 NY -> AMC
        assert classify_ny_session("2026-07-30T12:25:00.000Z") == (date(2026, 7, 30), EarningsSession.BMO)
        assert classify_ny_session("2026-07-30T13:30:00.000Z") == (date(2026, 7, 30), EarningsSession.AMC)
        assert classify_ny_session("2026-07-30T20:30:28.000Z") == (date(2026, 7, 30), EarningsSession.AMC)

        # EST (-05:00): 14:15Z = 09:15 NY -> BMO; 21:30Z = 16:30 NY -> AMC
        assert classify_ny_session("2026-11-02T14:15:00Z") == (date(2026, 11, 2), EarningsSession.BMO)
        assert classify_ny_session("2026-11-02T21:30:23.000Z") == (date(2026, 11, 2), EarningsSession.AMC)

    def test_extract_8k_earnings_releases_handles_string_and_list_items(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["10-Q", "8-K", "8-K", "8-K"],
                    "items": ["", "2.02,9.01", ["5.02"], ["2.02", "9.01"]],
                    "acceptanceDateTime": [
                        "2026-05-01T20:00:00.000Z",
                        "2026-04-30T20:30:41.000Z",
                        "2026-03-15T12:00:00.000Z",
                        "2026-01-29T12:30:00.000Z",
                    ],
                    "filingDate": ["2026-05-01", "2026-04-30", "2026-03-15", "2026-01-29"],
                    "accessionNumber": ["0001", "0002", "0003", "0004"],
                }
            }
        }
        releases = extract_8k_earnings_releases(submissions)
        assert releases == [
            (date(2026, 1, 29), EarningsSession.BMO, "0004"),
            (date(2026, 4, 30), EarningsSession.AMC, "0002"),
        ]


class TestXbrlSplitConsistentPairing:
    def _sample_companyfacts(self) -> dict:
        return {
            "facts": {
                "us-gaap": {
                    "EarningsPerShareDiluted": {
                        "units": {
                            "USD/shares": [
                                # Q2 10-Q filed 2026-05-01 (same accn has split-consistent YoY pair)
                                {
                                    "start": "2026-01-01",
                                    "end": "2026-03-31",
                                    "filed": "2026-05-01",
                                    "val": 2.01,
                                    "form": "10-Q",
                                    "accn": "accn-q2-2026",
                                },
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-03-31",
                                    "filed": "2026-05-01",
                                    "val": 1.65,
                                    "form": "10-Q",
                                    "accn": "accn-q2-2026",
                                },
                                # Q3 10-Q 9M YTD (for deriving Q4)
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-09-30",
                                    "filed": "2025-11-01",
                                    "val": 5.00,
                                    "form": "10-Q",
                                    "accn": "accn-q3-2025",
                                },
                                {
                                    "start": "2024-01-01",
                                    "end": "2024-09-30",
                                    "filed": "2025-11-01",
                                    "val": 4.20,
                                    "form": "10-Q",
                                    "accn": "accn-q3-2025",
                                },
                                # FY 10-K annual YoY pair -> Q4 derived: 7.40-5.00=2.40 vs 6.20-4.20=2.00
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-15",
                                    "val": 7.40,
                                    "form": "10-K",
                                    "accn": "accn-fy-2025",
                                },
                                {
                                    "start": "2024-01-01",
                                    "end": "2024-12-31",
                                    "filed": "2026-02-15",
                                    "val": 6.20,
                                    "form": "10-K",
                                    "accn": "accn-fy-2025",
                                },
                            ]
                        }
                    },
                    # Legacy + ASC 606 revenue concepts merged cleanly
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2026-01-01",
                                    "end": "2026-03-31",
                                    "filed": "2026-05-01",
                                    "val": 95_000_000_000.0,
                                    "form": "10-Q",
                                    "accn": "accn-q2-2026",
                                },
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-03-31",
                                    "filed": "2026-05-01",
                                    "val": 90_000_000_000.0,
                                    "form": "10-Q",
                                    "accn": "accn-q2-2026",
                                },
                            ]
                        }
                    },
                    "SalesRevenueNet": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2017-01-01",
                                    "end": "2017-03-31",
                                    "filed": "2017-05-03",
                                    "val": 52_000_000_000.0,
                                    "form": "10-Q",
                                    "accn": "accn-q2-2017",
                                },
                                {
                                    "start": "2016-01-01",
                                    "end": "2016-03-31",
                                    "filed": "2017-05-03",
                                    "val": 50_000_000_000.0,
                                    "form": "10-Q",
                                    "accn": "accn-q2-2017",
                                },
                            ]
                        }
                    },
                }
            }
        }

    def test_extract_xbrl_yoy_quarterly_facts_derives_q4_and_merges_revenue_tags(self):
        cf = self._sample_companyfacts()
        eps = extract_xbrl_yoy_quarterly_facts(cf, ("EarningsPerShareDiluted",), "USD/shares")
        assert date(2026, 3, 31) in eps
        assert eps[date(2026, 3, 31)]["actual"] == pytest.approx(2.01)
        assert eps[date(2026, 3, 31)]["estimate"] == pytest.approx(1.65)
        assert date(2025, 12, 31) in eps
        assert eps[date(2025, 12, 31)]["form"] == "10-K_Q4"
        assert eps[date(2025, 12, 31)]["actual"] == pytest.approx(2.40)
        assert eps[date(2025, 12, 31)]["estimate"] == pytest.approx(2.00)

        rev = extract_xbrl_yoy_quarterly_facts(
            cf,
            ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
            "USD",
            value_decimals=2,
        )
        assert date(2026, 3, 31) in rev
        assert date(2017, 3, 31) in rev

    def test_build_pead_events_aligns_8k_and_falls_back_to_10k_amc(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "items": ["2.02,9.01"],
                    "acceptanceDateTime": ["2026-04-30T20:30:41.000Z"],
                    "filingDate": ["2026-04-30"],
                    "accessionNumber": ["0002"],
                }
            }
        }
        events = build_pead_events_from_sec("AAPL", submissions, self._sample_companyfacts())
        assert len(events) == 2

        # Q4 2025 had no matching 8-K -> falls back to 10-K filed date (2026-02-15) as AMC
        ev_q4 = events[0]
        assert ev_q4.announcement_date == date(2026, 2, 15)
        assert ev_q4.session == EarningsSession.AMC
        assert ev_q4.source == "SEC_EDGAR_10QK+XBRL_SRW"
        assert ev_q4.eps_actual == pytest.approx(2.40)
        assert ev_q4.eps_estimate == pytest.approx(2.00)

        # Q2 2026 matched 8-K Item 2.02 (2026-04-30 AMC)
        ev_q2 = events[1]
        assert ev_q2.announcement_date == date(2026, 4, 30)
        assert ev_q2.session == EarningsSession.AMC
        assert ev_q2.source == "SEC_EDGAR_8K_202+XBRL_SRW"
        assert ev_q2.revenue_actual == pytest.approx(95_000_000_000.0)
        assert ev_q2.revenue_estimate == pytest.approx(90_000_000_000.0)

    def test_build_pead_events_prefers_earnings_8k_over_early_deliveries_8k(self):
        submissions = {
            "filings": {
                "recent": {
                    "form": ["8-K", "8-K"],
                    "items": ["2.02,9.01", "2.02,9.01"],
                    "acceptanceDateTime": [
                        "2026-04-29T20:35:00.000Z",  # actual earnings 2 days before 10-Q
                        "2026-04-02T13:07:00.000Z",  # deliveries 8-K on quarter+2d
                    ],
                    "filingDate": ["2026-04-29", "2026-04-02"],
                    "accessionNumber": ["0002", "0001"],
                }
            }
        }
        events = build_pead_events_from_sec("TSLA", submissions, self._sample_companyfacts())
        q2_ev = [e for e in events if e.announcement_date.month == 4][0]
        assert q2_ev.announcement_date == date(2026, 4, 29)
        assert q2_ev.session == EarningsSession.AMC


class TestEdgarFetchAndCsvRoundTrip:
    def test_fetch_events_resolves_ciks_skips_missing_and_loads_in_run_pead_study(self, tmp_path: Path):
        tickers_payload = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        submissions_payload = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "items": ["2.02"],
                    "acceptanceDateTime": ["2026-04-30T11:30:00.000Z"],
                    "filingDate": ["2026-04-30"],
                    "accessionNumber": ["0001"],
                }
            }
        }
        companyfacts_payload = {
            "facts": {
                "us-gaap": {
                    "EarningsPerShareDiluted": {
                        "units": {
                            "USD/shares": [
                                {
                                    "start": "2026-01-01",
                                    "end": "2026-03-31",
                                    "filed": "2026-05-01",
                                    "val": 2.10,
                                    "form": "10-Q",
                                    "accn": "accn1",
                                },
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-03-31",
                                    "filed": "2026-05-01",
                                    "val": 1.75,
                                    "form": "10-Q",
                                    "accn": "accn1",
                                },
                            ]
                        }
                    }
                }
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("user-agent") == "IBKRMonitor dev@example.com"
            if "company_tickers.json" in str(request.url):
                return httpx.Response(200, json=tickers_payload)
            if "submissions/CIK0000320193.json" in str(request.url):
                return httpx.Response(200, json=submissions_payload)
            if "companyfacts/CIK0000320193.json" in str(request.url):
                return httpx.Response(200, json=companyfacts_payload)
            return httpx.Response(404)

        cache_dir = tmp_path / "cache"
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            events, skipped = fetch_pead_events_for_symbols(
                ["AAPL", "TSM"],
                "IBKRMonitor dev@example.com",
                cache_dir=cache_dir,
                rate_limit_seconds=0.2,
                http_client=client,
                sleep_fn=lambda _s: None,
            )

        assert skipped == ["TSM"]
        assert len(events) == 1
        assert events[0].session == EarningsSession.BMO

        csv_path = write_pead_events_csv(events, tmp_path / "pead_events.csv")
        loaded = run_pead_study.load_events_csv(csv_path)
        assert len(loaded) == 1
        assert loaded[0].symbol == "AAPL"
        assert loaded[0].announcement_date == date(2026, 4, 30)
        assert loaded[0].session == EarningsSession.BMO
        assert loaded[0].eps_actual == pytest.approx(2.10)
        assert loaded[0].eps_estimate == pytest.approx(1.75)

        assert fetch_pead_events.parse_args(["--symbols", "AAPL"]).symbols == "AAPL"
