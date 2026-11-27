"""Tests for Phase 0 No.2 prerequisites diagnostic and IBKRClient market-data notices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ibkr.client import IBKRClient
from scripts.check_no2_prerequisites import (
    check_env_read_only,
    evaluate_track_readiness,
    main as phase0_main,
    run_live_probe,
    save_phase0_report,
    summarize_account_constraints,
    summarize_intraday_probe,
    summarize_market_data_probe,
)


class TestIBKRClientMarketDataNotices:
    def test_354_and_10168_queued_as_notices_only(self):
        client = IBKRClient()

        client.error(1001, 354, "Requested market data is not subscribed. Delayed market data is available.")
        client.error(1002, 10168, "Delayed market data is not enabled.")

        notices = client.drain_market_data_notices()
        assert notices == [
            {
                "reqId": 1001,
                "errorCode": 354,
                "errorString": "Requested market data is not subscribed. Delayed market data is available.",
                "type": "market_data_notice",
            },
            {
                "reqId": 1002,
                "errorCode": 10168,
                "errorString": "Delayed market data is not enabled.",
                "type": "market_data_notice",
            },
        ]
        assert client.drain_market_data_notices() == []
        assert client.drain_errors() == []

    @pytest.mark.parametrize("code", [10089, 10167, 10189])
    def test_10089_10167_10189_enter_both_notice_and_error_queues(self, code: int):
        client = IBKRClient()

        client.error(1003, code, f"Market data notice {code}")

        notices = client.drain_market_data_notices()
        errors = client.drain_errors()

        assert notices == [
            {
                "reqId": 1003,
                "errorCode": code,
                "errorString": f"Market data notice {code}",
                "type": "market_data_notice",
            }
        ]
        assert errors == [
            {
                "reqId": 1003,
                "errorCode": code,
                "errorString": f"Market data notice {code}",
                "type": "data",
            }
        ]

    def test_non_market_data_errors_preserve_existing_routing(self):
        client = IBKRClient()

        client.error(-1, 2104, "Market data farm connection is OK")
        client.error(1004, 200, "No security definition has been found")
        client.error(-1, 1100, "Connectivity between IBKR and TWS has been lost")

        assert client.drain_market_data_notices() == []
        errors = client.drain_errors()
        assert [e["errorCode"] for e in errors] == [200, 1100]
        assert [e["type"] for e in errors] == ["data", "connectivity"]


class TestAccountConstraintsAndMasking:
    def test_cash_account_t_plus_1_and_no_raw_account_leak(self):
        raw_acct = "U12345678"
        items = [
            {"account": raw_acct, "tag": "AccountType", "value": "CASH", "currency": ""},
            {"account": raw_acct, "tag": "NetLiquidation", "value": "11000.50", "currency": "USD"},
            {"account": raw_acct, "tag": "SettledCash", "value": "8500.25", "currency": "USD"},
            {"account": raw_acct, "tag": "AvailableFunds", "value": "8500.25", "currency": "USD"},
            {"account": raw_acct, "tag": "DayTradesRemaining", "value": "-1", "currency": ""},
        ]

        summary = summarize_account_constraints(items, [raw_acct])

        assert summary["is_paper"] is False
        assert summary["is_cash_account"] is True
        assert summary["t_plus_1_cash_only"] is True
        assert summary["pdt_restricted"] is False
        assert summary["net_liquidation"] == 11000.5
        assert summary["settled_cash"] == 8500.25
        assert summary["available_funds"] == 8500.25
        assert summary["day_trades_remaining"] == -1
        assert summary["accounts_masked"] == ["U12****78"]
        assert raw_acct not in json.dumps(summary)

    def test_margin_under_25k_pdt_restricted(self):
        raw_acct = "U98765432"
        items = [
            {"account": raw_acct, "tag": "AccountType", "value": "MARGIN", "currency": ""},
            {"account": raw_acct, "tag": "NetLiquidation", "value": "14000", "currency": "USD"},
            {"account": raw_acct, "tag": "SettledCash", "value": "14000", "currency": "USD"},
            {"account": raw_acct, "tag": "AvailableFunds", "value": "14000", "currency": "USD"},
            {"account": raw_acct, "tag": "DayTradesRemaining", "value": "3", "currency": ""},
        ]

        summary = summarize_account_constraints(items, [raw_acct])

        assert summary["is_paper"] is False
        assert summary["is_cash_account"] is False
        assert summary["t_plus_1_cash_only"] is False
        assert summary["pdt_restricted"] is True
        assert summary["day_trades_remaining"] == 3
        assert summary["accounts_masked"] == ["U98****32"]
        assert raw_acct not in json.dumps(summary)

    def test_margin_over_25k_unrestricted(self):
        items = [
            {"account": "U55556666", "tag": "AccountType", "value": "MARGIN", "currency": ""},
            {"account": "U55556666", "tag": "NetLiquidation", "value": "30000", "currency": "USD"},
            {"account": "U55556666", "tag": "DayTradesRemaining", "value": "-1", "currency": ""},
        ]

        summary = summarize_account_constraints(items, ["U55556666"])
        assert summary["is_cash_account"] is False
        assert summary["pdt_restricted"] is False

    def test_paper_du_account_masked_in_saved_reports(self, tmp_path: Path):
        raw_acct = "DU87654321"
        items = [
            {"account": raw_acct, "tag": "AccountType", "value": "INDIVIDUAL", "currency": ""},
            {"account": raw_acct, "tag": "NetLiquidation", "value": "100000", "currency": "USD"},
            {"account": raw_acct, "tag": "SettledCash", "value": "100000", "currency": "USD"},
            {"account": raw_acct, "tag": "AvailableFunds", "value": "100000", "currency": "USD"},
            {"account": raw_acct, "tag": "BuyingPower", "value": "100000", "currency": "USD"},
            {"account": raw_acct, "tag": "DayTradesRemaining", "value": "-1", "currency": ""},
        ]
        acct_info = summarize_account_constraints(items, [raw_acct])
        assert acct_info["is_paper"] is True
        assert acct_info["accounts_masked"] == ["DU87****21"]
        assert acct_info["t_plus_1_cash_only"] is True

        md_info = summarize_market_data_probe([], [], [], ["SPY"])
        intra_info = summarize_intraday_probe([], 0.1, [])
        tracks = evaluate_track_readiness(acct_info, md_info, intra_info)
        report = {
            "generated_at": "2026-11-26T00:00:00+00:00",
            "connection": {"host": "127.0.0.1", "port": 7497, "client_id": 79},
            "env": {"ok": True},
            "account": acct_info,
            "market_data": md_info,
            "intraday": intra_info,
            "tracks": tracks,
        }
        json_path, md_path = save_phase0_report(tmp_path, report)
        json_text = json_path.read_text(encoding="utf-8")
        md_text = md_path.read_text(encoding="utf-8")

        assert raw_acct not in json_text
        assert raw_acct not in md_text
        assert "DU87****21" in json_text
        assert "DU87****21" in md_text


class TestMarketDataIntradayAndTrackGating:
    def test_market_data_probe_realtime_ok(self):
        items = [
            {"symbol": "SPY", "ticker_id": 1001, "bid": 599.10, "ask": 599.12, "last": 599.11},
            {"symbol": "AAPL", "ticker_id": 1002, "bid": 230.00, "ask": 230.05, "last": 230.02},
        ]
        res = summarize_market_data_probe(items, [], [], ["SPY", "AAPL"])
        assert res["realtime_ok"] is True
        assert res["status"] == "realtime_ok"
        assert res["unsubscribed"] is False
        assert res["issue_codes"] == []
        assert res["symbols_with_quotes"] == ["SPY", "AAPL"]

    def test_market_data_probe_10089_unsubscribed(self):
        notices = [
            {
                "reqId": 1001,
                "errorCode": 10089,
                "errorString": "Requested market data requires additional subscription for API.",
            }
        ]
        errors = [
            {
                "reqId": 1001,
                "errorCode": 10089,
                "errorString": "Requested market data requires additional subscription for API.",
            }
        ]
        res = summarize_market_data_probe(
            [{"symbol": "SPY", "ticker_id": 1001}],
            notices,
            errors,
            ["SPY", "AAPL"],
        )
        assert res["realtime_ok"] is False
        assert res["unsubscribed"] is True
        assert res["issue_codes"] == [10089]
        assert res["status"] == "delayed_only_or_unsubscribed"

    def test_market_data_probe_354_delayed_even_if_ticks_present(self):
        items = [{"symbol": "SPY", "ticker_id": 1001, "bid": 599.0, "ask": 599.1, "last": 599.05}]
        notices = [
            {
                "reqId": 1001,
                "errorCode": 354,
                "errorString": "Requested market data is not subscribed. Delayed market data is available.",
            }
        ]
        res = summarize_market_data_probe(items, notices, [], ["SPY"])
        assert res["realtime_ok"] is False
        assert res["delayed_available_only"] is True
        assert res["issue_codes"] == [354]

    def test_intraday_probe_summary(self):
        bars = [
            {"date": "20261124  15:55:00", "open": 598.0, "high": 599.0, "low": 597.5, "close": 598.8, "volume": 1000},
            {"date": "20261120  09:30:00", "open": 590.0, "high": 591.0, "low": 589.5, "close": 590.5, "volume": 2000},
        ]
        res = summarize_intraday_probe(bars, 1.2346, [])
        assert res["intraday_ok"] is True
        assert res["bar_count"] == 2
        assert res["elapsed_seconds"] == 1.235
        assert res["first_timestamp"] == "20261120  09:30:00"
        assert res["last_timestamp"] == "20261124  15:55:00"

        err_res = summarize_intraday_probe([], 0.5, [{"reqId": 1, "errorCode": 162, "errorString": "No data"}])
        assert err_res["intraday_ok"] is False

    @pytest.mark.parametrize(
        ("realtime_ok", "intraday_ok", "expected_track3"),
        [
            (False, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ],
    )
    def test_track3_strictly_gated_on_realtime_and_intraday(
        self, realtime_ok: bool, intraday_ok: bool, expected_track3: bool
    ):
        acct = {"is_cash_account": True, "t_plus_1_cash_only": True, "pdt_restricted": False, "has_account_summary": True}
        md = {"realtime_ok": realtime_ok, "issue_codes": [] if realtime_ok else [10089]}
        intra = {"intraday_ok": intraday_ok, "bar_count": 390 if intraday_ok else 0, "elapsed_seconds": 1.2}

        tracks = evaluate_track_readiness(acct, md, intra)

        assert tracks["track1_pead"]["ready"] is True
        assert tracks["track2_odd_lot"]["ready"] is True
        reminders = " ".join(tracks["track2_odd_lot"]["manual_reminders"])
        assert "Voluntary Corporate Action" in reminders
        assert "IBKR Client Portal" in reminders

        assert tracks["track3_orb_5m"]["ready"] is expected_track3
        assert tracks["track3_orb_5m"]["unlocked"] is expected_track3


class TestEnvGuardsAndLiveProbe:
    def test_check_env_missing_and_valid_and_guard_failures(self, tmp_path: Path, capsys):
        missing = tmp_path / ".env.missing"
        res_missing = check_env_read_only(missing)
        assert res_missing["exists"] is False
        assert res_missing["ok"] is False

        valid_env = tmp_path / ".env.valid"
        valid_env.write_text(
            "IBKR_HOST=127.0.0.1\nIBKR_PORT=7497\nIBKR_ACCOUNT=U12345678\nAPP_HOST=127.0.0.1\nREAD_ONLY=true\n",
            encoding="utf-8",
        )
        res_valid = check_env_read_only(valid_env)
        assert res_valid["exists"] is True
        assert res_valid["ok"] is True
        assert res_valid["read_only_ok"] is True
        assert res_valid["app_host_ok"] is True
        assert res_valid["ibkr_port"] == 7497
        assert res_valid["ibkr_port_ok"] is True
        assert res_valid["port_mode"] == "Paper"
        assert res_valid["ibkr_account_masked"] == "U12****78"
        assert "U12345678" not in json.dumps(res_valid)

        rc = phase0_main(["--env-only", "--env-file", str(valid_env)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "U12345678" not in out
        assert "U12****78" in out

        bad_ro = tmp_path / ".env.bad_ro"
        bad_ro.write_text("READ_ONLY=false\nAPP_HOST=127.0.0.1\nIBKR_PORT=7497\n", encoding="utf-8")
        res_bad_ro = check_env_read_only(bad_ro)
        assert res_bad_ro["ok"] is False
        assert res_bad_ro["read_only_ok"] is False
        assert phase0_main(["--env-only", "--env-file", str(bad_ro)]) == 1

        bad_host = tmp_path / ".env.bad_host"
        bad_host.write_text("READ_ONLY=true\nAPP_HOST=0.0.0.0\nIBKR_PORT=7497\n", encoding="utf-8")
        res_bad_host = check_env_read_only(bad_host)
        assert res_bad_host["ok"] is False
        assert res_bad_host["app_host_ok"] is False
        assert phase0_main(["--env-only", "--env-file", str(bad_host)]) == 1

        bad_port = tmp_path / ".env.bad_port"
        bad_port.write_text("READ_ONLY=true\nAPP_HOST=127.0.0.1\nIBKR_PORT=abc\n", encoding="utf-8")
        res_bad_port = check_env_read_only(bad_port)
        assert res_bad_port["ok"] is False
        assert res_bad_port["ibkr_port_ok"] is False

    def test_run_live_probe_with_fake_client(self, tmp_path: Path):
        raw_acct = "DU12345678"
        fake = _FakePhase0Client(raw_acct)

        report = run_live_probe(
            host="127.0.0.1",
            port=7497,
            client_id=79,
            symbols=["SPY", "AAPL"],
            timeout=2.0,
            env_info={"ok": True, "read_only_raw": "true", "app_host": "127.0.0.1", "ibkr_port": 7497},
            client_factory=lambda: fake,
        )
        json_path, md_path = save_phase0_report(tmp_path, report)

        assert fake.disconnected is True
        assert fake.market_data_types == [1]
        assert all(req["snapshot"] is True for req in fake.mkt_data_requests)
        assert fake.hist_requests[0]["durationStr"] == "5 D"
        assert fake.hist_requests[0]["barSizeSetting"] == "5 mins"
        assert fake.hist_requests[0]["whatToShow"] == "TRADES"

        assert report["account"]["is_paper"] is True
        assert report["account"]["accounts_masked"] == ["DU12****78"]
        assert report["market_data"]["realtime_ok"] is False
        assert report["market_data"]["issue_codes"] == [10089]
        assert report["intraday"]["intraday_ok"] is True
        assert report["tracks"]["track3_orb_5m"]["ready"] is False

        assert raw_acct not in json_path.read_text(encoding="utf-8")
        assert raw_acct not in md_path.read_text(encoding="utf-8")
        assert "DU12****78" in json_path.read_text(encoding="utf-8")
        assert fake.cancelled_account_summaries == 1
        assert fake.cancelled_mkt_data == [1002]

    def test_run_live_probe_unlocks_track3_when_realtime_and_intraday_ok(self, tmp_path: Path):
        raw_acct = "U24681357"
        fake = _FakePhase0Client(raw_acct, emit_10089_for_spy=False)

        report = run_live_probe(
            host="127.0.0.1",
            port=7496,
            client_id=79,
            symbols=["SPY", "AAPL"],
            timeout=2.0,
            env_info={"ok": True, "read_only_raw": "true", "app_host": "127.0.0.1", "ibkr_port": 7496},
            client_factory=lambda: fake,
        )
        json_path, md_path = save_phase0_report(tmp_path, report)

        assert fake.disconnected is True
        assert report["account"]["is_paper"] is False
        assert report["account"]["accounts_masked"] == ["U24****57"]
        assert report["market_data"]["realtime_ok"] is True
        assert report["intraday"]["intraday_ok"] is True
        assert report["tracks"]["track3_orb_5m"]["ready"] is True
        assert report["tracks"]["track3_orb_5m"]["unlocked"] is True
        assert raw_acct not in json_path.read_text(encoding="utf-8")
        assert raw_acct not in md_path.read_text(encoding="utf-8")

    def test_run_live_probe_drains_multi_batch_and_trailing_bars_with_end(self):
        fake = _MultiBatchHistClient("U13572468")

        report = run_live_probe(
            host="127.0.0.1",
            port=7497,
            client_id=79,
            symbols=["SPY"],
            timeout=2.0,
            env_info={"ok": True, "read_only_raw": "true", "app_host": "127.0.0.1", "ibkr_port": 7497},
            client_factory=lambda: fake,
        )

        assert fake.disconnected is True
        assert report["intraday"]["intraday_ok"] is True
        assert report["intraday"]["bar_count"] == 3
        assert report["intraday"]["first_timestamp"] == "20261124  09:30:00"
        assert report["intraday"]["last_timestamp"] == "20261124  09:40:00"

    def test_run_live_probe_avoids_ticker_id_collision_and_ignores_stale_error_300(self):
        fake = _CollisionAndError300Client("U13572468")

        report = run_live_probe(
            host="127.0.0.1",
            port=7497,
            client_id=79,
            symbols=["SPY", "AAPL"],
            timeout=2.0,
            env_info={"ok": True, "read_only_raw": "true", "app_host": "127.0.0.1", "ibkr_port": 7497},
            client_factory=lambda: fake,
        )

        used_mkt_ids = {req["reqId"] for req in fake.mkt_data_requests}
        assert fake.hist_requests[0]["reqId"] not in used_mkt_ids
        assert fake.cancelled_mkt_data == [1002]
        assert report["intraday"]["intraday_ok"] is True
        assert report["intraday"]["bar_count"] == 1
        assert report["intraday"]["errors"] == []


class _FakePhase0Client(IBKRClient):
    def __init__(self, raw_account: str, emit_10089_for_spy: bool = True) -> None:
        super().__init__()
        self._raw_account = raw_account
        self._emit_10089_for_spy = emit_10089_for_spy
        self.disconnected = False
        self.cancelled_account_summaries = 0
        self.cancelled_mkt_data: list[int] = []
        self.market_data_types: list[int] = []
        self.mkt_data_requests: list[dict] = []
        self.hist_requests: list[dict] = []

    def connect(self, host: str, port: int, clientId: int) -> None:
        self.accounts = [self._raw_account]
        self.current_account = self._raw_account
        self.connected_event.set()

    def run(self) -> None:
        return None

    def disconnect(self) -> None:
        self.disconnected = True

    def reqAccountSummary(self, reqId: int, groupName: str, tags: str) -> None:
        self.accountSummary(reqId, self._raw_account, "AccountType", "CASH", "")
        self.accountSummary(reqId, self._raw_account, "NetLiquidation", "11000", "USD")
        self.accountSummary(reqId, self._raw_account, "SettledCash", "11000", "USD")
        self.accountSummary(reqId, self._raw_account, "AvailableFunds", "11000", "USD")
        self.accountSummary(reqId, self._raw_account, "DayTradesRemaining", "-1", "")
        self.accountSummaryEnd(reqId)

    def cancelAccountSummary(self, reqId: int) -> None:
        self.cancelled_account_summaries += 1

    def reqMarketDataType(self, marketDataType: int) -> None:
        self.market_data_types.append(marketDataType)

    def reqMktData(self, reqId, contract, genericTickList, snapshot, regulatorySnapshot, mktDataOptions) -> None:
        self.mkt_data_requests.append({"reqId": reqId, "symbol": contract.symbol, "snapshot": snapshot})
        if self._emit_10089_for_spy and contract.symbol == "SPY":
            self.error(reqId, 10089, "Requested market data requires additional subscription for API.")
        else:
            self.tickPrice(reqId, 1, 230.10, None)
            self.tickPrice(reqId, 2, 230.12, None)
            self.tickPrice(reqId, 4, 230.11, None)

    def cancelMktData(self, reqId: int) -> None:
        self.cancelled_mkt_data.append(reqId)

    def reqHistoricalData(
        self,
        reqId,
        contract,
        endDateTime,
        durationStr,
        barSizeSetting,
        whatToShow,
        useRTH,
        formatDate,
        keepUpToDate,
        chartOptions,
    ) -> None:
        self.hist_requests.append(
            {
                "reqId": reqId,
                "symbol": contract.symbol,
                "durationStr": durationStr,
                "barSizeSetting": barSizeSetting,
                "whatToShow": whatToShow,
            }
        )
        bar = type("BarObj", (), {"date": "20261125  09:30:00", "open": 600.0, "high": 600.5, "low": 599.5, "close": 600.2, "volume": 1000})()
        self.historicalData(reqId, bar)
        self.historicalDataEnd(reqId, "20261120", "20261125")

    def cancelHistoricalData(self, reqId: int) -> None:
        return None


class _MultiBatchHistClient(_FakePhase0Client):
    """Simulates multi-batch historical bars arriving alongside historicalDataEnd."""

    def __init__(self, raw_account: str) -> None:
        super().__init__(raw_account, emit_10089_for_spy=False)
        self._hist_req_id: int | None = None
        self._end_polls = 0

    def reqHistoricalData(
        self,
        reqId,
        contract,
        endDateTime,
        durationStr,
        barSizeSetting,
        whatToShow,
        useRTH,
        formatDate,
        keepUpToDate,
        chartOptions,
    ) -> None:
        self._hist_req_id = reqId
        bar1 = type("BarObj", (), {"date": "20261124  09:30:00", "open": 600.0, "high": 600.5, "low": 599.5, "close": 600.2, "volume": 1000})()
        self.historicalData(reqId, bar1)

    def drain_historical_data_end(self) -> list[dict]:
        # Skip the pre-request cleanup drain (self._hist_req_id is None until reqHistoricalData).
        if self._hist_req_id is not None:
            self._end_polls += 1
            if self._end_polls == 2:
                bar2 = type("BarObj", (), {"date": "20261124  09:35:00", "open": 600.2, "high": 600.7, "low": 600.0, "close": 600.5, "volume": 1100})()
                self.historicalData(self._hist_req_id, bar2)
                self.historicalDataEnd(self._hist_req_id, "20261124  09:30:00", "20261124  09:40:00")
        return super().drain_historical_data_end()

    def drain_historical_data(self) -> list[dict]:
        items = super().drain_historical_data()
        # Simulate a trailing bar pushed concurrently right before the post-loop drain.
        if self._hist_req_id is not None and self._end_polls == 2 and any(
            b.get("date") == "20261124  09:35:00" for b in items
        ):
            bar3 = type("BarObj", (), {"date": "20261124  09:40:00", "open": 600.5, "high": 601.0, "low": 600.3, "close": 600.8, "volume": 1200})()
            self.historicalData(self._hist_req_id, bar3)
        return items


class _CollisionAndError300Client(_FakePhase0Client):
    """Simulates delayed error 300 arriving during the intraday historical probe."""

    def __init__(self, raw_account: str) -> None:
        super().__init__(raw_account, emit_10089_for_spy=True)

    def reqHistoricalData(
        self,
        reqId,
        contract,
        endDateTime,
        durationStr,
        barSizeSetting,
        whatToShow,
        useRTH,
        formatDate,
        keepUpToDate,
        chartOptions,
    ) -> None:
        self.error(1001, 300, "Can't find EId with tickerId:1001")
        self.error(reqId, 300, f"Can't find EId with tickerId:{reqId}")
        super().reqHistoricalData(
            reqId,
            contract,
            endDateTime,
            durationStr,
            barSizeSetting,
            whatToShow,
            useRTH,
            formatDate,
            keepUpToDate,
            chartOptions,
        )
