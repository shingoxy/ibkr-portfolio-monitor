"""Tests for read-only Index Core Rebalance Advisor and IBKR live/delayed valuation callbacks."""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

from ibapi.contract import Contract

from app.ibkr.client import IBKRClient
from app.ibkr.connection_manager import ConnectionManager, DataCache
from app.trading.index_advisor import (
    build_index_core_report,
    evaluate_trend_regime,
    load_cached_bars,
    load_index_study_summary,
    load_odd_lot_summary,
)
from app.trading.index_backtest import PortfolioRule, _evaluate_weights
from app.trading.models import Bar


def _make_bars(symbol: str, closes: list[float], start: date = date(2025, 1, 2)) -> list[Bar]:
    bars: list[Bar] = []
    cur = start
    for close in closes:
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        bars.append(
            Bar(
                symbol=symbol,
                date=cur,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1_000_000.0,
            )
        )
        cur += timedelta(days=1)
    return bars


def _snapshot(
    *,
    connected: bool = True,
    account: str = "U12345678",
    net_liq: float = 10_000.0,
    settled_cash: float | None = 500.0,
    total_cash: float | None = 500.0,
    available_funds: float | None = 500.0,
    positions: list[dict] | None = None,
    market_data: dict[int, dict] | None = None,
) -> dict:
    summary = [{"account": account, "tag": "AccountType", "value": "INDIVIDUAL", "currency": "USD"}]
    summary.append({"account": account, "tag": "NetLiquidation", "value": str(net_liq), "currency": "USD"})
    if total_cash is not None:
        summary.append({"account": account, "tag": "TotalCashValue", "value": str(total_cash), "currency": "USD"})
    if settled_cash is not None:
        summary.append({"account": account, "tag": "SettledCash", "value": str(settled_cash), "currency": "USD"})
    if available_funds is not None:
        summary.append({"account": account, "tag": "AvailableFunds", "value": str(available_funds), "currency": "USD"})
    return {
        "connected": connected,
        "connection_mode": "Paper" if connected else "Unknown",
        "accounts": [account] if account else [],
        "account_summary": summary,
        "positions": positions or [],
        "market_data": market_data or {},
        "portfolio_values": {},
        "data_freshness": "fresh" if connected else "stale",
        "last_update": None,
    }


class TestTrendRegimeHysteresis:
    def test_matches_index_backtest_evaluate_weights_step_by_step(self):
        # 200 flat closes at 100.0 -> SMA starts at 100.0.
        # Day 200: 102.0 (RISK_ON)
        # Day 201: 98.5 (within -2% buffer >= 98.0 -> stays RISK_ON)
        # Day 202: 96.0 (breaks below -2% -> flips DEFENSIVE)
        # Day 203: 101.0 (within +2% buffer <= 102.0 -> stays DEFENSIVE)
        # Day 204: 105.0 (breaks above +2% -> flips RISK_ON)
        closes = [100.0] * 199 + [102.0, 98.5, 96.0, 101.0, 105.0]
        spy_bars = _make_bars("SPY", closes)
        qqq_bars = _make_bars("QQQ", [100.0] * len(closes))
        rule = PortfolioRule(
            name="trend",
            target_weights={"SPY": 0.70, "QQQ": 0.30},
            rebalance_frequency="monthly",
            trend_window=200,
            defensive_symbol="BIL",
            trend_buffer_pct=0.02,
        )

        prev_risk_on: dict[str, bool] = {}
        expected_switches: list[tuple[date, bool]] = []
        for idx in range(200, len(spy_bars) + 1):
            histories = {"SPY": spy_bars[:idx], "QQQ": qqq_bars[:idx]}
            _, prev_risk_on = _evaluate_weights(rule, histories, prev_risk_on)
            regime = evaluate_trend_regime(
                "SPY", histories["SPY"], trend_window=200, trend_buffer_pct=0.02
            )
            assert regime["available"] is True
            assert regime["risk_on"] is prev_risk_on["SPY"]
            if idx == 201:
                assert regime["risk_on"] is True
                assert regime["in_buffer_zone"] is True
            if idx == 202:
                assert regime["risk_on"] is False
                expected_switches.append((spy_bars[idx - 1].date, False))
            if idx == 203:
                assert regime["risk_on"] is False
                assert regime["in_buffer_zone"] is True
            if idx == 204:
                assert regime["risk_on"] is True
                expected_switches.append((spy_bars[idx - 1].date, True))

        final_regime = evaluate_trend_regime("SPY", spy_bars, trend_window=200, trend_buffer_pct=0.02)
        assert final_regime["last_switch_date"] == expected_switches[-1][0].isoformat()

    def test_insufficient_bars_returns_unavailable(self):
        bars = _make_bars("SPY", [500.0] * 50)
        regime = evaluate_trend_regime("SPY", bars, trend_window=200, trend_buffer_pct=0.02)
        assert regime["available"] is False
        assert regime["regime"] == "UNAVAILABLE"


class TestIndexCorePlans:
    def _bars_all_bull(self) -> dict[str, list[Bar]]:
        return {
            "SPY": _make_bars("SPY", [400.0] * 200 + [500.0]),
            "QQQ": _make_bars("QQQ", [350.0] * 200 + [500.0]),
            "BIL": _make_bars("BIL", [91.5] * 201),
        }

    def test_plan_a_hold_when_within_drift_threshold(self):
        # 14 SPY @ $500 = $7,000 (70%), 6 QQQ @ $500 = $3,000 (30%), $0 cash
        snap = _snapshot(
            net_liq=10_000.0,
            settled_cash=0.0,
            total_cash=0.0,
            available_funds=0.0,
            positions=[
                {"symbol": "SPY", "sec_type": "STK", "con_id": 1, "position": 14, "avg_cost": 480.0, "market_price": 500.0, "data_status": "portfolio"},
                {"symbol": "QQQ", "sec_type": "STK", "con_id": 2, "position": 6, "avg_cost": 460.0, "market_price": 500.0, "data_status": "portfolio"},
            ],
        )
        report = build_index_core_report(snap, self._bars_all_bull())
        plan_a = report["plans"]["drift_rebalance"]
        assert plan_a["status"] == "HOLD"
        assert plan_a["suggested_orders"] == []
        assert report["account"]["account_masked"] == "U12****78"
        assert "U12345678" not in json.dumps(report)

    def test_plan_a_dca_buy_only_allocates_to_underweight_without_selling(self):
        # Existing 28 SPY ($14,000) + 12 QQQ ($6,000) + $0 cash + $600 DCA -> $20,600 equity
        # Drift is 2.04% (< 5.0% threshold), so no sells occur; $600 buys 1 underweight SPY share.
        snap = _snapshot(
            net_liq=20_000.0,
            settled_cash=0.0,
            total_cash=0.0,
            available_funds=0.0,
            positions=[
                {"symbol": "SPY", "sec_type": "STK", "con_id": 1, "position": 28, "avg_cost": 480.0, "market_price": 500.0},
                {"symbol": "QQQ", "sec_type": "STK", "con_id": 2, "position": 12, "avg_cost": 460.0, "market_price": 500.0},
            ],
        )
        report = build_index_core_report(snap, self._bars_all_bull(), extra_contribution=600.0)
        plan_a = report["plans"]["drift_rebalance"]
        assert plan_a["status"] == "DCA_BUY_ONLY"
        orders = plan_a["suggested_orders"]
        assert len(orders) == 1
        assert orders[0]["symbol"] == "SPY"
        assert orders[0]["action"] == "BUY"
        total_outlay = sum(o["quantity"] * o["suggested_limit_price"] + 1.0 for o in orders)
        assert total_outlay <= 600.0 + 1e-6

    def test_plan_a_rebalance_triggered_sells_overweight_and_buys_underweight(self):
        # 16 SPY ($8,000 = 80%) + 4 QQQ ($2,000 = 20%) -> 10% drift >= 5%
        snap = _snapshot(
            net_liq=10_000.0,
            settled_cash=0.0,
            total_cash=0.0,
            available_funds=0.0,
            positions=[
                {"symbol": "SPY", "sec_type": "STK", "con_id": 1, "position": 16, "avg_cost": 450.0, "market_price": 500.0},
                {"symbol": "QQQ", "sec_type": "STK", "con_id": 2, "position": 4, "avg_cost": 450.0, "market_price": 500.0},
            ],
        )
        report = build_index_core_report(snap, self._bars_all_bull())
        plan_a = report["plans"]["drift_rebalance"]
        assert plan_a["status"] == "REBALANCE_TRIGGERED"
        assert plan_a["max_drift_pct"] >= 5.0
        actions = {(o["symbol"], o["action"]): o for o in plan_a["suggested_orders"]}
        assert ("SPY", "SELL") in actions
        assert ("QQQ", "BUY") in actions
        assert plan_a["suggested_orders"][0]["action"] == "SELL"
        assert plan_a["estimated_cash_after"] >= 0.0

    def test_plan_a_initial_allocation_with_disconnected_override_cash(self):
        snap = _snapshot(connected=False, account="", net_liq=0.0, settled_cash=None, total_cash=None, available_funds=None)
        report = build_index_core_report(snap, self._bars_all_bull(), override_cash=5650.0)
        plan_a = report["plans"]["drift_rebalance"]
        assert report["capital"]["is_simulated_cash"] is True
        assert report["capital"]["cash_base"] == 5650.0
        assert plan_a["status"] == "INITIAL_ALLOCATION"
        assert {o["symbol"] for o in plan_a["suggested_orders"]} == {"SPY", "QQQ"}
        assert plan_a["estimated_cash_after"] >= 0.0

    def test_plan_b_regime_switch_required_when_qqq_breaks_below_buffer(self):
        bars = {
            "SPY": _make_bars("SPY", [400.0] * 200 + [500.0]),  # RISK_ON
            "QQQ": _make_bars("QQQ", [500.0] * 200 + [480.0]),  # 480 < 500 * 0.98 (490) -> DEFENSIVE
            "BIL": _make_bars("BIL", [91.5] * 201),
        }
        snap = _snapshot(
            net_liq=10_000.0,
            settled_cash=0.0,
            total_cash=0.0,
            available_funds=0.0,
            positions=[
                {"symbol": "SPY", "sec_type": "STK", "con_id": 1, "position": 14, "avg_cost": 480.0, "market_price": 500.0},
                {"symbol": "QQQ", "sec_type": "STK", "con_id": 2, "position": 6, "avg_cost": 490.0, "market_price": 480.0},
            ],
        )
        report = build_index_core_report(snap, bars)
        plan_b = report["plans"]["trend_200sma_bil"]
        assert report["regimes"]["SPY"]["risk_on"] is True
        assert report["regimes"]["QQQ"]["risk_on"] is False
        assert plan_b["target_weights"] == {"SPY": 0.70, "QQQ": 0.0, "BIL": 0.30}
        assert plan_b["status"] == "REGIME_SWITCH_REQUIRED"
        actions = {(o["symbol"], o["action"]): o for o in plan_b["suggested_orders"]}
        assert ("QQQ", "SELL") in actions
        assert actions[("QQQ", "SELL")]["quantity"] == 6
        assert ("BIL", "BUY") in actions

    def test_non_core_options_isolated_and_defensive_falls_back_to_shy(self):
        bars = {
            "SPY": _make_bars("SPY", [400.0] * 200 + [500.0]),
            "QQQ": _make_bars("QQQ", [350.0] * 200 + [500.0]),
            "SHY": _make_bars("SHY", [82.0] * 201),
        }
        snap = _snapshot(
            account="U12345678",
            net_liq=5650.0,
            settled_cash=None,
            total_cash=None,
            available_funds=530.72,
            positions=[
                {
                    "account": "U12345678",
                    "symbol": "QQQ",
                    "local_symbol": "QQQ   260717P00680000",
                    "sec_type": "OPT",
                    "con_id": 999,
                    "position": 1.0,
                    "avg_cost": 8.32,
                    "market_price": 12.0,
                    "market_value": 1200.0,
                    "unrealized_pnl": 368.0,
                    "expiry": "20260717",
                    "strike": 680.0,
                    "right": "P",
                }
            ],
        )
        report = build_index_core_report(snap, bars, defensive_symbol="BIL")
        assert report["capital"]["defensive_symbol"] == "SHY"
        assert report["capital"]["cash_source"] == "available_funds"
        assert report["capital"]["cash_base"] == 530.72
        assert report["other_positions"]["count"] == 1
        assert report["other_positions"]["items"][0]["account"] == "U12****78"
        assert "U12345678" not in json.dumps(report)

    def test_price_source_priority_tws_portfolio_then_quote_then_history(self):
        bars = {
            "SPY": _make_bars("SPY", [500.0] * 201),
            "QQQ": _make_bars("QQQ", [450.0] * 201),
            "BIL": _make_bars("BIL", [91.5] * 201),
        }
        snap = _snapshot(
            positions=[
                {"symbol": "SPY", "sec_type": "STK", "con_id": 1, "position": 5, "avg_cost": 400.0, "market_price": 512.0, "data_status": "portfolio"},
                {"symbol": "QQQ", "sec_type": "STK", "con_id": 2, "position": 3, "avg_cost": 410.0, "market_price": 0.0},
            ],
            market_data={2: {"con_id": 2, "symbol": "QQQ", "sec_type": "STK", "last": 468.5}},
        )
        report = build_index_core_report(snap, bars)
        rows = {r["symbol"]: r for r in report["plans"]["drift_rebalance"]["weight_rows"]}
        assert rows["SPY"]["price"] == 512.0
        assert rows["SPY"]["price_source"] == "tws_portfolio"
        assert rows["QQQ"]["price"] == 468.5
        assert rows["QQQ"]["price_source"] == "tws_quote"
        assert rows["BIL"]["price"] == 91.5
        assert rows["BIL"]["price_source"] == "history_close"


class TestClientAndCacheValuation:
    def test_update_portfolio_and_tick_price_enrich_snapshot_without_mutation(self):
        client = IBKRClient()
        contract = Contract()
        contract.conId = 756733
        contract.symbol = "SPY"
        contract.secType = "STK"
        client.updatePortfolio(contract, 5.0, 520.25, 2601.25, 480.0, 201.25, 0.0, "U12345678")

        contract_qqq = Contract()
        contract_qqq.conId = 320227571
        contract_qqq.symbol = "QQQ"
        contract_qqq.secType = "STK"
        tid = client.allocate_ticker_id({"con_id": 320227571, "symbol": "QQQ", "sec_type": "STK"})
        client.tickPrice(tid, 68, 495.5, None)  # delayed last

        cache = DataCache()
        cache.update_positions([
            {"con_id": 756733, "symbol": "SPY", "sec_type": "STK", "position": 5.0, "avg_cost": 480.0, "market_price": 0.0, "market_value": 0.0},
            {"con_id": 320227571, "symbol": "QQQ", "sec_type": "STK", "position": 2.0, "avg_cost": 450.0, "market_price": 0.0, "market_value": 0.0},
        ])
        cache.update_portfolio_values(client.get_portfolio_values())
        cache.update_market_data(client.drain_market_data())

        snap1 = cache.get_snapshot()
        pos_map = {p["symbol"]: p for p in snap1["positions"]}
        assert pos_map["SPY"]["market_price"] == 520.25
        assert pos_map["SPY"]["market_value"] == 2601.25
        assert pos_map["SPY"]["unrealized_pnl"] == 201.25
        assert pos_map["QQQ"]["market_price"] == 495.5
        assert pos_map["QQQ"]["market_value"] == 991.0
        assert pos_map["QQQ"]["unrealized_pnl"] == 91.0
        assert pos_map["QQQ"]["data_status"] == "delayed"

        # Mutating returned snapshot must not affect underlying cache storage
        pos_map["SPY"]["market_value"] = 0.0
        snap2 = cache.get_snapshot()
        pos_map2 = {p["symbol"]: p for p in snap2["positions"]}
        assert pos_map2["SPY"]["market_value"] == 2601.25


class TestArtifactLoaders:
    def test_load_cached_bars_and_json_summaries(self, tmp_path: Path):
        hist_dir = tmp_path / "history"
        hist_dir.mkdir()
        with (hist_dir / "SPY.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            writer.writerow(["2026-11-24", "595", "598", "594", "597", "1000"])
            writer.writerow(["2026-11-25", "598", "601", "597", "600", "1100"])

        bars = load_cached_bars(hist_dir, ["SPY", "MISSING"])
        assert list(bars.keys()) == ["SPY"]
        assert len(bars["SPY"]) == 2
        assert bars["SPY"][-1].close == 600.0

        odd_path = tmp_path / "odd-lot-scan-latest.json"
        odd_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-11-26T18:00:53",
                    "as_of_date": "2026-11-26",
                    "summary": {"EXPIRED": 8, "NEEDS_QUOTE": 1},
                    "opportunities": [
                        {"symbol": "MYPS", "company_name": "PLAYSTUDIOS", "status": "NEEDS_QUOTE", "min_offer_price": 4.3, "max_offer_price": 5.5},
                        {"symbol": "OLD", "company_name": "OLD CO", "status": "EXPIRED"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        odd = load_odd_lot_summary(odd_path)
        assert odd["available"] is True
        assert [o["symbol"] for o in odd["active_opportunities"]] == ["MYPS"]
        assert odd["expired_count"] == 8

        study_path = tmp_path / "index-study-latest.json"
        study_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-11-26T17:10:13",
                    "index_strategies": [
                        {
                            "strategy_name": "SPY 买入持有",
                            "total_return_pct": 278.28,
                            "cagr_pct": 14.23,
                            "max_drawdown_pct": -33.64,
                            "sharpe_ratio": 0.827,
                            "order_count": 1,
                            "equity_curve": [{"date": "2026-11-25", "equity": 41610.56}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        study = load_index_study_summary(study_path)
        assert study["available"] is True
        assert len(study["index_strategies"]) == 1
        assert "equity_curve" not in json.dumps(study)

        assert load_odd_lot_summary(tmp_path / "nope.json")["available"] is False
        assert load_index_study_summary(tmp_path / "nope.json")["available"] is False

    def test_datacache_incremental_merge_and_zero_removal(self):
        cache = DataCache()
        cache.update_account_summary([
            {"account": "U12345678", "tag": "TotalCashValue", "value": "462.42", "currency": "USD"},
            {"account": "U12345678", "tag": "AvailableFunds", "value": "530.76", "currency": "USD"},
            {"account": "U12345678", "tag": "AccountType", "value": "INDIVIDUAL", "currency": ""},
            {"account": "U12345678", "tag": "NetLiquidation", "value": "5750.00", "currency": "USD"},
        ])
        cache.update_account_summary([
            {"account": "U12345678", "tag": "NetLiquidation", "value": "5843.14", "currency": "USD"},
        ])
        snap = cache.get_snapshot()
        by_tag = {item["tag"]: item["value"] for item in snap["account_summary"]}
        assert by_tag == {
            "TotalCashValue": "462.42",
            "AvailableFunds": "530.76",
            "AccountType": "INDIVIDUAL",
            "NetLiquidation": "5843.14",
        }

        cache.update_positions([
            {"account": "U12345678", "con_id": 873192277, "symbol": "2476", "sec_type": "STK", "currency": "HKD", "position": 400.0},
            {"account": "U12345678", "con_id": 72539702, "symbol": "TQQQ", "sec_type": "STK", "currency": "USD", "position": 7.0},
        ])
        cache.update_positions([
            {"account": "U12345678", "con_id": 72539702, "symbol": "TQQQ", "sec_type": "STK", "currency": "USD", "position": 10.0},
            {"account": "U12345678", "con_id": 873192277, "symbol": "2476", "sec_type": "STK", "currency": "HKD", "position": 0.0},
        ])
        positions = cache.get_snapshot()["positions"]
        assert len(positions) == 1
        assert positions[0]["symbol"] == "TQQQ"
        assert positions[0]["position"] == 10.0

    def test_subscribe_uses_hk_exchange_for_non_usd_and_smart_for_usd(self):
        mgr = ConnectionManager({"host": "127.0.0.1", "port": 7497, "client_id": 17})
        client = IBKRClient()
        reqs: list[tuple[int, Contract]] = []
        client.reqMktData = lambda tid, contract, *_args: reqs.append((tid, contract))  # type: ignore[method-assign]
        mgr._client = client

        mgr._subscribe_option_market_data([
            {"con_id": 873192277, "symbol": "2476", "sec_type": "STK", "currency": "HKD", "exchange": "SEHK"},
            {"con_id": 72539702, "symbol": "TQQQ", "sec_type": "STK", "currency": "USD", "exchange": "NASDAQ"},
        ])

        assert len(reqs) == 2
        by_symbol = {contract.symbol: contract for _, contract in reqs}
        assert by_symbol["2476"].exchange == "SEHK"
        assert by_symbol["2476"].currency == "HKD"
        assert by_symbol["TQQQ"].exchange == "SMART"
        assert by_symbol["TQQQ"].currency == "USD"

    def test_readonly_321_filtered_while_genuine_321_is_queued(self):
        client = IBKRClient()
        client.error(-1, 321, "确认请求时出错。:-'bZ'：原因 - API接口现处于只读模式。")
        client.error(1001, 321, "Error validating request:-'bS' : cause - Invalid order type")
        queued = client.drain_errors()
        assert len(queued) == 1
        assert queued[0]["reqId"] == 1001
        assert queued[0]["errorCode"] == 321

    def test_build_index_core_report_separates_multi_currency_other_positions(self):
        bars = {
            "SPY": _make_bars("SPY", [400.0] * 200 + [500.0]),
            "QQQ": _make_bars("QQQ", [350.0] * 200 + [500.0]),
            "BIL": _make_bars("BIL", [91.5] * 201),
        }
        snap = _snapshot(
            net_liq=5843.14,
            settled_cash=None,
            total_cash=462.42,
            available_funds=530.76,
            positions=[
                {
                    "account": "U12345678",
                    "symbol": "2476",
                    "sec_type": "STK",
                    "con_id": 873192277,
                    "currency": "HKD",
                    "exchange": "SEHK",
                    "position": 400.0,
                    "avg_cost": 201.61,
                    "market_price": 99.09,
                    "market_value": 39636.0,
                    "unrealized_pnl": -41009.29,
                },
                {
                    "account": "U12345678",
                    "symbol": "TQQQ",
                    "sec_type": "STK",
                    "con_id": 72539702,
                    "currency": "USD",
                    "exchange": "NASDAQ",
                    "position": 7.0,
                    "avg_cost": 52.44,
                    "market_price": 45.94,
                    "market_value": 321.58,
                    "unrealized_pnl": -45.51,
                },
            ],
        )
        report = build_index_core_report(snap, bars)
        other = report["other_positions"]
        assert other["count"] == 2
        assert other["has_multi_currency"] is True
        assert other["total_market_value"] is None
        assert other["totals_by_currency"] == {"HKD": 39636.0, "USD": 321.58}
        items = {item["symbol"]: item for item in other["items"]}
        assert items["2476"]["currency"] == "HKD"
        assert items["2476"]["exchange"] == "SEHK"
        assert items["TQQQ"]["currency"] == "USD"
        assert items["TQQQ"]["exchange"] == "NASDAQ"
        assert "U12345678" not in json.dumps(report)
