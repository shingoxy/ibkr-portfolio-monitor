"""Tests for read-only US odd-lot tender offer parser, evaluator, EDGAR fetch, and CLI."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from app.trading.odd_lot_scanner import (
    OpportunityStatus,
    TenderOfferFiling,
    evaluate_odd_lot_opportunity,
    fetch_recent_edgar_tender_filings,
    fetch_tws_ask_quotes,
    parse_tender_offer_text,
    strip_html_to_text,
)
from scripts import scan_odd_lots


class TestHtmlStrippingAndParsing:
    def test_strip_html_removes_scripts_styles_and_preserves_cell_spacing(self):
        raw_html = """
        <html>
          <head>
            <style>.hidden { color: red; }</style>
            <script>var secret = "ignore me $999.00";</script>
          </head>
          <body>
            <h1>Acme&nbsp;Corp (NASDAQ: ACME)</h1>
            <table>
              <tr><td>Purchase Price:</td><td>$24.00</td><td>to</td><td>$28.50</td><td>per share</td></tr>
            </table>
            <p>Holders of <b>odd</b>-<b>lot</b> shares (&lt; 100 shares) are not subject to proration.</p>
          </body>
        </html>
        """
        plain = strip_html_to_text(raw_html)
        assert "ignore me" not in plain
        assert "color: red" not in plain
        assert "Acme Corp (NASDAQ: ACME)" in plain
        assert "$24.00 to $28.50 per share" in plain
        assert "odd-lot shares (< 100 shares)" in plain

    @pytest.mark.parametrize(
        ("phrase", "expected_min", "expected_max"),
        [
            ("Offer to purchase shares at $24.00 to $28.00 per share.", 24.0, 28.0),
            ("Offer price between $18.25 and $21.75 per share in cash.", 18.25, 21.75),
            ("Purchase price not greater than $30.00 nor less than $25.50 per share.", 25.5, 30.0),
            ("Purchase price not less than $19.00 nor greater than $22.50 per share.", 19.0, 22.5),
            ("At a Purchase Price Not Less Than $4.30 Per Share and Not More Than $5.50 Per Share.", 4.3, 5.5),
            ("Modified Dutch auction tender price range from US$5.00 per Share to US$5.75 per Share.", 5.0, 5.75),
        ],
    )
    def test_dutch_auction_parsing_variants(self, phrase: str, expected_min: float, expected_max: float):
        text = (
            f"ACME Corp (NYSE: ACME) Offer for up to $250,000,000 of Common Stock. {phrase} "
            "Holders of fewer than 100 shares will receive odd-lot priority. "
            "The Offer expires at 5:00 p.m., New York City time, on December 15, 2026."
        )
        filing = parse_tender_offer_text(text)
        assert filing.symbol == "ACME"
        assert filing.offer_type == "dutch_auction"
        assert filing.min_offer_price == pytest.approx(expected_min)
        assert filing.max_offer_price == pytest.approx(expected_max)
        assert filing.odd_lot_priority is True
        assert filing.odd_lot_max_shares == 99
        assert filing.expiration_date == date(2026, 12, 15)
        assert filing.evidence_snippets
        # JSON serializable
        encoded = json.dumps(filing.to_dict())
        assert '"dutch_auction"' in encoded

    def test_fixed_price_parsing_ignores_par_value_and_historical_trading_range(self):
        text = """
        Beta Holdings Inc. (NASDAQ: BETA) Offer to Purchase Up to $150 million of Common Stock
        (par value $0.01 per share) at a fixed cash purchase price of $42.50 per share.
        During Q3 2026, our shares traded from $34.00 to $39.50.
        Holders of 99 shares or fewer are exempt from proration (odd lot priority).
        Expiration Date: 2026-12-20.
        The Offer is not conditioned upon any financing.
        """
        filing = parse_tender_offer_text(text)
        assert filing.symbol == "BETA"
        assert filing.offer_type == "fixed_price"
        assert filing.min_offer_price == pytest.approx(42.50)
        assert filing.max_offer_price == pytest.approx(42.50)
        assert filing.odd_lot_priority is True
        assert filing.expiration_date == date(2026, 12, 20)
        assert filing.financing_risk is False
        assert filing.proration_risk is False

    def test_odd_lot_priority_positive_and_negative_cases(self):
        positive_phrases = [
            "Tenders of odd lots will be accepted in full before proration.",
            "Holders of odd-lot shares have priority.",
            "Beneficial owners of fewer than 100 shares are not subject to proration.",
            "Shareholders owning up to 99 shares will be purchased without proration.",
        ]
        for clause in positive_phrases:
            filing = parse_tender_offer_text(f"Offer at $20.00 per share. {clause}")
            assert filing.odd_lot_priority is True, f"Expected True for: {clause}"

        negative_cases = [
            ("Offer at $20.00 per share. All tendered shares are subject to proration.", True),
            ("Offer at $20.00 per share. There is no odd-lot priority; odd lots will also be prorated.", True),
            ("Offer at $20.00 per share. Odd-lot priority is not available in this tender offer.", False),
            ("Offer at $20.00 per share. Holders of fewer than 100 shares will be subject to proration.", True),
        ]
        for text, expected_proration in negative_cases:
            filing = parse_tender_offer_text(text)
            assert filing.odd_lot_priority is False, f"Expected False for: {text}"
            assert filing.proration_risk is expected_proration

    def test_cover_fixed_offer_price_beats_historical_repurchase_footnote(self):
        filler = "Summary details of the offer. " * 260
        text = (
            "UTAH MEDICAL PRODUCTS INC (NASDAQ: UTMD) OFFER TO PURCHASE AT A PURCHASE PRICE OF $75.00 PER SHARE. "
            "Odd lots are not subject to proration. Expires October 7, 2026. "
            + filler
            + "During the 60 days prior to the date hereof, the Company purchased 10,000 Shares at prices "
            "between $69.10 and $71.00 (average price of $69.88 per Share) in open market transactions."
        )
        filing = parse_tender_offer_text(text)
        assert filing.offer_type == "fixed_price"
        assert filing.min_offer_price == pytest.approx(75.0)
        assert filing.max_offer_price == pytest.approx(75.0)

    def test_odd_lot_explicit_denials_override_generic_mentions(self):
        xflt_text = (
            "XAI Floating Rate Trust (NYSE: XFLT) tender offer at $15.00 per share. "
            "The Initial Tender Offer shall require odd lot tenders to be subject to the same proration terms "
            "as tenders of 100 shares or more. Also mentions odd lot elsewhere."
        )
        nfj_text = (
            "Virtus NFJ (NYSE: NFJ) tender offer at $16.24 per share. "
            "This tender offer will not have any special pro ration provision for odd-lot tenders, "
            "which means that all odd-lot tenders (including Shareholders who own fewer than 100 Shares) "
            "are subject to proration."
        )
        assert parse_tender_offer_text(xflt_text).odd_lot_priority is False
        assert parse_tender_offer_text(nfj_text).odd_lot_priority is False

    def test_expiration_date_parsing_formats(self):
        f1 = parse_tender_offer_text("Offer at $15.00 per share. Valid until Dec. 5, 2026 unless extended.")
        f2 = parse_tender_offer_text("Offer at $15.00 per share. Deadline: 2027-01-18.")
        f3 = parse_tender_offer_text(
            "Filed November 10, 2026. The Offer will expire at 5:00 p.m. E.T. on December 22, 2026."
        )
        assert f1.expiration_date == date(2026, 12, 5)
        assert f2.expiration_date == date(2027, 1, 18)
        assert f3.expiration_date == date(2026, 12, 22)

    def test_risk_flags_withdrawal_financing_proration(self):
        text = """
        Gamma Corp (NYSE: GAMA) tender offer at $18.00 per share.
        The Offer is conditioned upon receipt of financing.
        The Company reserves the right to withdraw or terminate the offer upon a material adverse change.
        All shares are subject to proration with no odd-lot priority.
        """
        filing = parse_tender_offer_text(text)
        assert filing.financing_risk is True
        assert filing.withdrawal_risk is True
        assert filing.proration_risk is True
        assert set(filing.risk_terms) >= {"financing_risk", "withdrawal_risk", "proration_risk"}


class TestEvaluationMathAndStatuses:
    def _sample_filing(self, **overrides) -> TenderOfferFiling:
        defaults = dict(
            symbol="ACME",
            company_name="Acme Corp",
            form_type="SC TO-I",
            offer_type="dutch_auction",
            min_offer_price=24.00,
            max_offer_price=28.00,
            odd_lot_priority=True,
            odd_lot_max_shares=99,
            expiration_date=date(2026, 12, 10),
        )
        defaults.update(overrides)
        return TenderOfferFiling(**defaults)

    def test_net_profit_and_annualized_math_zero_vs_high_voluntary_ca_fee(self):
        filing = self._sample_filing()
        as_of = date(2026, 11, 26)  # 14 days to exp + 3 settlement days = 17 holding days

        opp_zero_fee = evaluate_odd_lot_opportunity(
            filing,
            ask_price=23.40,
            as_of_date=as_of,
            buy_commission=1.0,
            slippage_bps=10.0,
            voluntary_ca_fee=0.0,
            max_shares=99,
        )
        # Entry = 23.40 * 1.001 = 23.4234
        # Capital = 23.4234 * 99 + 1.0 = 2319.9166
        # Worst net = 24.00 * 99 - 2319.9166 - 0.0 = 56.0834
        # Best net = 28.00 * 99 - 2319.9166 - 0.0 = 452.0834
        assert opp_zero_fee.status == OpportunityStatus.ACTIONABLE_REVIEW
        assert opp_zero_fee.holding_days == 17
        assert opp_zero_fee.worst_case_net_profit == pytest.approx(56.0834, abs=1e-3)
        assert opp_zero_fee.best_case_net_profit == pytest.approx(452.0834, abs=1e-3)
        expected_worst_ret = (56.0834 / 2319.9166) * 100.0
        assert opp_zero_fee.worst_case_return_pct == pytest.approx(expected_worst_ret, abs=1e-3)
        assert opp_zero_fee.worst_case_annualized_pct == pytest.approx(expected_worst_ret * (365.0 / 17.0), abs=1e-2)
        assert "unverified_voluntary_ca_fee" in opp_zero_fee.risk_flags
        assert "fee_exceeds_spread" not in opp_zero_fee.risk_flags

        # With $60 Voluntary CA fee, worst-case net turns negative: 56.0834 - 60 = -3.9166
        # Even at $50 fee on a tighter ask ($23.55), check $50 flips positive spread negative.
        opp_high_fee = evaluate_odd_lot_opportunity(
            filing,
            ask_price=23.55,
            as_of_date=as_of,
            buy_commission=1.0,
            slippage_bps=10.0,
            voluntary_ca_fee=50.0,
            max_shares=99,
        )
        assert opp_high_fee.worst_case_net_profit < 0
        assert opp_high_fee.status == OpportunityStatus.LOW_OR_NEGATIVE_NET
        assert "fee_exceeds_spread" in opp_high_fee.risk_flags
        assert "unverified_voluntary_ca_fee" not in opp_high_fee.risk_flags

        # Compare same ask ($23.55) with $0 fee vs $50 fee directly
        opp_tight_zero = evaluate_odd_lot_opportunity(
            filing,
            ask_price=23.55,
            as_of_date=as_of,
            buy_commission=1.0,
            slippage_bps=10.0,
            voluntary_ca_fee=0.0,
            max_shares=99,
        )
        assert opp_tight_zero.worst_case_net_profit > 0
        assert opp_tight_zero.status == OpportunityStatus.ACTIONABLE_REVIEW
        assert opp_tight_zero.worst_case_net_profit - opp_high_fee.worst_case_net_profit == pytest.approx(50.0, abs=1e-4)

        # JSON-serializable
        json.dumps(opp_high_fee.to_dict())

    def test_settlement_days_minimum_when_expiring_today(self):
        as_of = date(2026, 12, 10)
        filing = self._sample_filing(expiration_date=as_of)
        opp = evaluate_odd_lot_opportunity(filing, ask_price=23.0, as_of_date=as_of)
        assert opp.holding_days == 3
        assert opp.worst_case_annualized_pct == pytest.approx(opp.worst_case_return_pct * (365.0 / 3.0), abs=1e-2)

    def test_expired_missing_quote_and_no_odd_lot_states(self):
        as_of = date(2026, 11, 26)
        expired_filing = self._sample_filing(expiration_date=date(2026, 11, 20))
        opp_exp = evaluate_odd_lot_opportunity(expired_filing, ask_price=22.0, as_of_date=as_of)
        assert opp_exp.status == OpportunityStatus.EXPIRED
        assert opp_exp.holding_days is None
        assert opp_exp.worst_case_annualized_pct is None
        assert "expired" in opp_exp.risk_flags

        no_odd_lot = self._sample_filing(odd_lot_priority=False)
        opp_no_odd = evaluate_odd_lot_opportunity(no_odd_lot, ask_price=22.0, as_of_date=as_of)
        assert opp_no_odd.status == OpportunityStatus.NO_ODD_LOT_PRIORITY
        assert "no_odd_lot_priority" in opp_no_odd.risk_flags

        needs_quote = self._sample_filing()
        opp_missing_quote = evaluate_odd_lot_opportunity(needs_quote, ask_price=None, as_of_date=as_of)
        assert opp_missing_quote.status == OpportunityStatus.NEEDS_QUOTE
        assert "missing_ask_quote" in opp_missing_quote.risk_flags


class TestEdgarAndTwsReadOnly:
    def test_edgar_rejects_invalid_user_agent_and_fast_rate_limit(self):
        with pytest.raises(ValueError, match="User-Agent"):
            fetch_recent_edgar_tender_filings("")
        with pytest.raises(ValueError, match="User-Agent"):
            fetch_recent_edgar_tender_filings("Mozilla/5.0")
        with pytest.raises(ValueError, match="contact email"):
            fetch_recent_edgar_tender_filings("IBKRMonitorNoEmail")
        with pytest.raises(ValueError, match="rate_limit_seconds"):
            fetch_recent_edgar_tender_filings("IBKRMonitor dev@example.com", rate_limit_seconds=0.05)

    def test_edgar_fetch_with_mock_transport_and_rate_limit_pauses(self):
        calls: list[tuple[str, str]] = []
        sleeps: list[float] = []

        search_payload = {
            "hits": {
                "hits": [
                    {
                        "_id": "0001193125-26-000123:d123sctoi.htm",
                        "_source": {
                            "ciks": ["0000123456"],
                            "display_names": ["Acme Corp (ACME) (CIK 0000123456)"],
                            "form": "SC TO-I",
                            "file_date": "2026-11-20",
                            "adsh": "0001193125-26-000123",
                        },
                    }
                ]
            }
        }
        filing_html = (
            "<html><body><p>Offer to purchase shares between $25.00 and $29.00 per share.</p>"
            "<p>Odd-lot holders of fewer than 100 shares are not subject to proration.</p>"
            "<p>Expires December 20, 2026.</p></body></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.host, request.url.path))
            assert request.headers.get("user-agent") == "IBKRMonitor research@example.com"
            if request.url.host == "efts.sec.gov":
                return httpx.Response(200, json=search_payload)
            return httpx.Response(200, text=filing_html)

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            filings = fetch_recent_edgar_tender_filings(
                "IBKRMonitor research@example.com",
                days_back=15,
                max_results=5,
                rate_limit_seconds=0.2,
                http_client=client,
                sleep_fn=sleeps.append,
            )

        assert len(filings) == 1
        assert filings[0].symbol == "ACME"
        assert filings[0].offer_type == "dutch_auction"
        assert filings[0].min_offer_price == 25.0
        assert filings[0].max_offer_price == 29.0
        assert filings[0].odd_lot_priority is True
        assert filings[0].expiration_date == date(2026, 12, 20)
        assert all(s >= 0.2 for s in sleeps) and len(sleeps) >= 1

    def test_fetch_tws_ask_quotes_uses_snapshot_only(self):
        class FakeClient:
            def __init__(self) -> None:
                self.reqs: list[tuple[int, str, bool]] = []
                self.market_data_types: list[int] = []
                self._tickers: dict[int, dict] = {}
                self._counter = 1000

            def reqMarketDataType(self, market_data_type: int) -> None:
                self.market_data_types.append(market_data_type)

            def allocate_ticker_id(self, info: dict) -> int:
                self._counter += 1
                self._tickers[self._counter] = dict(info)
                return self._counter

            def reqMktData(self, ticker_id, contract, generic_tick_list, snapshot, regulatory_snapshot, options):
                self.reqs.append((ticker_id, contract.symbol, snapshot))
                self._tickers[ticker_id]["ask"] = 23.45

            def get_ticker_info(self, ticker_id: int) -> dict:
                return dict(self._tickers.get(ticker_id, {}))

            def drain_errors(self) -> list[dict]:
                return []

            def clear_ticker(self, ticker_id: int) -> None:
                self._tickers.pop(ticker_id, None)

        fake = FakeClient()
        quotes = fetch_tws_ask_quotes(["ACME"], client=fake, timeout_seconds=0.5)
        assert quotes == {"ACME": 23.45}
        assert fake.reqs == [(1001, "ACME", True)]

    def test_fetch_tws_ask_quotes_breaks_early_on_ibkr_market_data_notices(self, monkeypatch):
        import time
        from app.ibkr.client import IBKRClient

        ib_client = IBKRClient()
        monkeypatch.setattr(ib_client, "reqMarketDataType", lambda _t: None)

        def emit_notice_354(ticker_id, contract, generic_tick_list, snapshot, regulatory_snapshot, options):
            assert snapshot is True
            code = 354 if contract.symbol == "ACME" else 10168
            ib_client.error(ticker_id, code, "Requested market data is not subscribed.")

        monkeypatch.setattr(ib_client, "reqMktData", emit_notice_354)

        started = time.monotonic()
        quotes = fetch_tws_ask_quotes(
            ["ACME", "BETA"],
            client=ib_client,
            timeout_seconds=2.0,
        )
        elapsed = time.monotonic() - started
        assert quotes == {"ACME": None, "BETA": None}
        assert elapsed < 0.8

    def test_ibkr_client_maps_delayed_ticks_without_overwriting_live_fields(self):
        from app.ibkr.client import IBKRClient

        ib_client = IBKRClient()
        tid = ib_client.allocate_ticker_id({"symbol": "SPY", "sec_type": "STK"})
        # Delayed ask + last + close
        ib_client.tickPrice(tid, 67, 501.25, None)
        ib_client.tickPrice(tid, 68, 501.10, None)
        ib_client.tickPrice(tid, 75, 500.50, None)
        info = ib_client.get_ticker_info(tid)
        assert info["ask"] == 501.25
        assert info["last"] == 501.10
        assert info["close"] == 500.50
        assert info["is_delayed"] is True

        # Live ask arrives and must not be overwritten by subsequent delayed ask
        ib_client.tickPrice(tid, 2, 502.00, None)
        ib_client.tickPrice(tid, 67, 499.00, None)
        info_after = ib_client.get_ticker_info(tid)
        assert info_after["ask"] == 502.00
        assert info_after["is_delayed"] is False

    def test_fetch_tws_ask_quotes_delayed_mode_ignores_10167_and_falls_back_to_last_close(self, monkeypatch):
        from app.ibkr.client import IBKRClient

        ib_client = IBKRClient()
        seen_types: list[int] = []
        monkeypatch.setattr(ib_client, "reqMarketDataType", lambda mdt: seen_types.append(mdt))

        def emit_delayed_ticks(ticker_id, contract, generic_tick_list, snapshot, regulatory_snapshot, options):
            ib_client.error(ticker_id, 10167, "Requested market data is not subscribed. Displaying delayed market data.")
            if contract.symbol == "ACME":
                ib_client.tickPrice(ticker_id, 67, 23.45, None)
            elif contract.symbol == "BETA":
                ib_client.tickPrice(ticker_id, 68, 19.80, None)
            elif contract.symbol == "GAMMA":
                ib_client.tickPrice(ticker_id, 75, 14.20, None)

        monkeypatch.setattr(ib_client, "reqMktData", emit_delayed_ticks)

        sources: dict[str, str] = {}
        quotes = fetch_tws_ask_quotes(
            ["ACME", "BETA", "GAMMA"],
            client=ib_client,
            market_data_type=4,
            timeout_seconds=0.15,
            quote_sources=sources,
        )
        assert seen_types == [4]
        assert quotes == {"ACME": 23.45, "BETA": 19.80, "GAMMA": 14.20}
        assert sources == {
            "ACME": "delayed_ask",
            "BETA": "delayed_last_fallback",
            "GAMMA": "delayed_close_fallback",
        }

    def test_edgar_query_dedupes_exhibits_and_filters_unlisted(self):
        requested_urls: list[str] = []
        search_payload = {
            "hits": {
                "hits": [
                    {
                        "_id": "0001193125-26-000111:d111ex99a1iii.htm",
                        "_source": {
                            "ciks": ["0000111111"],
                            "display_names": ["Acme Corp (ACME) (CIK 0000111111)"],
                            "form": "SC TO-I",
                            "file_date": "2026-11-22",
                            "adsh": "0001193125-26-000111",
                        },
                    },
                    {
                        "_id": "0001193125-26-000111:d111ex99a1i.htm",
                        "_source": {
                            "ciks": ["0000111111"],
                            "display_names": ["Acme Corp (ACME) (CIK 0000111111)"],
                            "form": "SC TO-I",
                            "file_date": "2026-11-22",
                            "adsh": "0001193125-26-000111",
                        },
                    },
                    {
                        "_id": "0001193125-26-000222:privfund_a1a.htm",
                        "_source": {
                            "ciks": ["0000222222"],
                            "display_names": ["Private Credit Fund (CIK 0000222222)"],
                            "form": "SC TO-I",
                            "file_date": "2026-11-24",
                            "adsh": "0001193125-26-000222",
                        },
                    },
                ]
            }
        }
        primary_html = (
            "<p>Offer to purchase up to $50M at $12.00 per share.</p>"
            "<p>Odd-lot priority for fewer than 100 shares. Expires December 30, 2026.</p>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if request.url.host == "efts.sec.gov":
                assert request.url.params.get("q") == '"odd lot" OR "odd-lot"'
                return httpx.Response(200, json=search_payload)
            if request.url.host == "data.sec.gov":
                return httpx.Response(200, json={"name": "Private Credit Fund", "tickers": []})
            assert "d111ex99a1i.htm" in request.url.path
            return httpx.Response(200, text=primary_html)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            filings = fetch_recent_edgar_tender_filings(
                "IBKRMonitor research@example.com",
                query='"odd lot" OR "odd-lot"',
                listed_only=True,
                max_results=5,
                rate_limit_seconds=0.2,
                http_client=client,
                sleep_fn=lambda _s: None,
            )

        assert len(filings) == 1
        assert filings[0].symbol == "ACME"
        assert filings[0].min_offer_price == 12.0
        assert not any("privfund_a1a.htm" in url for url in requested_urls)
        assert not any("d111ex99a1iii.htm" in url for url in requested_urls)

    def test_edgar_dedupes_amendment_and_base_filing_by_issuer(self):
        search_payload = {
            "hits": {
                "hits": [
                    {
                        "_id": "0001096906-26-001481:utah_exa1a.htm",
                        "_source": {
                            "ciks": ["0000706698"],
                            "display_names": ["UTAH MEDICAL PRODUCTS INC (UTMD) (CIK 0000706698)"],
                            "form": "SC TO-I/A",
                            "file_date": "2026-09-29",
                            "adsh": "0001096906-26-001481",
                        },
                    },
                    {
                        "_id": "0001096906-26-001428:utmd_exa1a.htm",
                        "_source": {
                            "ciks": ["0000706698"],
                            "display_names": ["UTAH MEDICAL PRODUCTS INC (UTMD) (CIK 0000706698)"],
                            "form": "SC TO-I",
                            "file_date": "2026-09-24",
                            "adsh": "0001096906-26-001428",
                        },
                    },
                ]
            }
        }
        amendment_html = "<p>Amendment extending odd-lot tender offer.</p><p>Odd-lot priority applies.</p>"
        base_html = (
            "<p>Offer to purchase at $75.00 per share.</p>"
            "<p>Odd lots are not subject to proration. Expires October 7, 2026.</p>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "efts.sec.gov":
                return httpx.Response(200, json=search_payload)
            if "utah_exa1a.htm" in request.url.path:
                return httpx.Response(200, text=amendment_html)
            if "utmd_exa1a.htm" in request.url.path:
                return httpx.Response(200, text=base_html)
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            filings = fetch_recent_edgar_tender_filings(
                "IBKRMonitor research@example.com",
                max_results=5,
                rate_limit_seconds=0.2,
                http_client=client,
                sleep_fn=lambda _s: None,
            )

        assert len(filings) == 1
        assert filings[0].symbol == "UTMD"
        assert filings[0].form_type == "SC TO-I/A"
        assert filings[0].min_offer_price == pytest.approx(75.0)
        assert filings[0].expiration_date == date(2026, 10, 7)


class TestEndToEndOfflineCli:
    def test_cli_offline_input_dir_writes_json_and_markdown_checklist(self, tmp_path: Path, monkeypatch):
        input_dir = tmp_path / "filings"
        output_dir = tmp_path / "out"
        input_dir.mkdir()

        # 1) HTML Dutch auction fixture (actionable with $0 fee, negative with $50 fee)
        (input_dir / "ACME_sc_toi.html").write_text(
            """
            <html><body>
              <h2>ACME Corp (NASDAQ: ACME)</h2>
              <p>Offer to purchase shares at a price not greater than $28.00 nor less than $24.00 per share.</p>
              <p>Holders of fewer than 100 shares (odd-lot priority) are not subject to proration.</p>
              <p>The Offer will expire at 5:00 p.m., New York City time, on December 18, 2026.</p>
            </body></html>
            """,
            encoding="utf-8",
        )
        # 2) TXT fixture without odd-lot priority
        (input_dir / "NOODD_offer.txt").write_text(
            "NoOdd Inc (NYSE: NOODD) fixed offer price of $15.00 per share. "
            "There is no odd-lot priority. Expires December 22, 2026.",
            encoding="utf-8",
        )
        # 3) JSON fixture missing quote in --quotes -> NEEDS_QUOTE
        (input_dir / "missq.json").write_text(
            json.dumps(
                {
                    "symbol": "MISSQ",
                    "company_name": "Missing Quote Inc",
                    "form_type": "SC TO-T",
                    "text": "Offer at $30.00 per share. Odd-lot holders of 99 shares or fewer are exempt from proration. Expires 2026-12-25.",
                }
            ),
            encoding="utf-8",
        )
        # 4) Expired fixture
        (input_dir / "OLD.txt").write_text(
            "Old Co (NYSE: OLD) offer at $20.00 per share. Odd lot priority applies. Expires 2026-10-01.",
            encoding="utf-8",
        )

        exit_code = scan_odd_lots.main([
            "--input-dir", str(input_dir),
            "--quotes", "ACME=23.55,NOODD=14.00,OLD=19.00",
            "--as-of-date", "2026-11-26",
            "--voluntary-ca-fee", "0",
            "--commission", "1.0",
            "--slippage-bps", "10",
            "--output-dir", str(output_dir),
        ])
        assert exit_code == 0

        latest_json = output_dir / "odd-lot-scan-latest.json"
        latest_md = output_dir / "odd-lot-scan-latest.md"
        assert latest_json.exists()
        assert latest_md.exists()

        timestamped_jsons = list(output_dir.glob("odd-lot-scan-2*.json"))
        timestamped_mds = list(output_dir.glob("odd-lot-scan-2*.md"))
        assert timestamped_jsons and timestamped_mds

        data = json.loads(latest_json.read_text(encoding="utf-8"))
        assert data["read_only"] is True
        assert "IBKR Client Portal -> Corporate Actions" in data["manual_execution_reminder"]
        assert data["summary"]["total_filings"] == 4
        assert data["summary"]["ACTIONABLE_REVIEW"] == 1
        assert data["summary"]["NEEDS_QUOTE"] == 1
        assert data["summary"]["NO_ODD_LOT_PRIORITY"] == 1
        assert data["summary"]["EXPIRED"] == 1

        by_sym = {row["symbol"]: row for row in data["opportunities"]}
        assert by_sym["ACME"]["status"] == "ACTIONABLE_REVIEW"
        assert "unverified_voluntary_ca_fee" in by_sym["ACME"]["risk_flags"]
        assert by_sym["ACME"]["worst_case_net_profit"] > 0
        assert by_sym["MISSQ"]["status"] == "NEEDS_QUOTE"
        assert by_sym["NOODD"]["status"] == "NO_ODD_LOT_PRIORITY"
        assert by_sym["OLD"]["status"] == "EXPIRED"

        md_text = latest_md.read_text(encoding="utf-8")
        assert "IBKR Client Portal -> Corporate Actions" in md_text
        assert "ACME" in md_text
        assert "ACTIONABLE_REVIEW" in md_text

        # Re-run with $50 Voluntary CA fee -> ACME turns LOW_OR_NEGATIVE_NET with fee_exceeds_spread
        exit_code_fee = scan_odd_lots.main([
            "--input-dir", str(input_dir),
            "--quotes", "ACME=23.55,NOODD=14.00,OLD=19.00",
            "--as-of-date", "2026-11-26",
            "--voluntary-ca-fee", "50",
            "--output-dir", str(output_dir),
        ])
        assert exit_code_fee == 0
        data_fee = json.loads(latest_json.read_text(encoding="utf-8"))
        by_sym_fee = {row["symbol"]: row for row in data_fee["opportunities"]}
        assert by_sym_fee["ACME"]["status"] == "LOW_OR_NEGATIVE_NET"
        assert "fee_exceeds_spread" in by_sym_fee["ACME"]["risk_flags"]
