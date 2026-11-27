"""Tests for /trading routes, templates, and read-only index-core API."""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates

from app.api import routes_trading
from app.api.routes_trading import _parse_watchlist


def test_parse_watchlist_trims_and_uppercases_symbols():
    assert _parse_watchlist(" spy, qqq,VTI ") == ["SPY", "QQQ", "VTI"]


def test_parse_watchlist_drops_empty_values():
    assert _parse_watchlist("SPY,,QQQ, ") == ["SPY", "QQQ"]


def _build_test_app(manager=None) -> FastAPI:
    app = FastAPI()
    templates_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    if manager is not None:
        app.state.manager = manager
    app.include_router(routes_trading.router)
    return app


def _write_history_csv(directory: Path, symbol: str, closes: list[float]) -> None:
    cur = date(2025, 1, 2)
    with (directory / f"{symbol}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for close in closes:
            while cur.weekday() >= 5:
                cur += timedelta(days=1)
            writer.writerow([cur.isoformat(), close, close * 1.01, close * 0.99, close, 1_000_000])
            cur += timedelta(days=1)


def test_trading_page_renders_index_core_ui_and_legacy_config_ids():
    client = TestClient(_build_test_app())
    resp = client.get("/trading")
    assert resp.status_code == 200
    assert "指数核心" in resp.text
    assert "/api/trading/index-core" in resp.text
    assert "/api/trading/config" in resp.text
    assert 'id="trading-config"' in resp.text
    assert 'id="trading-mode"' in resp.text


def test_api_trading_config_returns_expected_keys():
    client = TestClient(_build_test_app())
    resp = client.get("/api/trading/config")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["mode"] == "signal_only"
    assert isinstance(payload["watchlist"], list)
    assert "max_positions" in payload


def test_api_index_core_masks_account_and_supports_contribution_and_override(tmp_path, monkeypatch):
    hist_dir = tmp_path / "history"
    hist_dir.mkdir()
    _write_history_csv(hist_dir, "SPY", [400.0] * 200 + [500.0])
    _write_history_csv(hist_dir, "QQQ", [350.0] * 200 + [500.0])
    _write_history_csv(hist_dir, "BIL", [91.5] * 201)

    bt_dir = tmp_path / "backtests"
    bt_dir.mkdir()
    (bt_dir / "index-study-latest.json").write_text(
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
    (bt_dir / "odd-lot-scan-latest.json").write_text(
        json.dumps({"as_of_date": "2026-11-26", "summary": {"EXPIRED": 1}, "opportunities": []}),
        encoding="utf-8",
    )

    monkeypatch.setattr(routes_trading, "HISTORY_DIR", hist_dir)
    monkeypatch.setattr(routes_trading, "BACKTESTS_DIR", bt_dir)

    snapshot = {
        "connected": True,
        "connection_mode": "Paper",
        "accounts": ["U12345678"],
        "account_summary": [
            {"account": "U12345678", "tag": "AccountType", "value": "INDIVIDUAL", "currency": "USD"},
            {"account": "U12345678", "tag": "NetLiquidation", "value": "20000.0", "currency": "USD"},
            {"account": "U12345678", "tag": "TotalCashValue", "value": "0.0", "currency": "USD"},
        ],
        "positions": [
            {"account": "U12345678", "symbol": "SPY", "sec_type": "STK", "con_id": 1, "position": 28, "avg_cost": 480.0, "market_price": 500.0},
            {"account": "U12345678", "symbol": "QQQ", "sec_type": "STK", "con_id": 2, "position": 12, "avg_cost": 460.0, "market_price": 500.0},
        ],
        "market_data": {},
        "data_freshness": "fresh",
        "last_update": None,
    }
    fake_manager = SimpleNamespace(cache=SimpleNamespace(get_snapshot=lambda: snapshot))
    client = TestClient(_build_test_app(manager=fake_manager))

    resp = client.get("/api/trading/index-core?extra_contribution=600")
    assert resp.status_code == 200
    body_text = resp.text
    assert "U12****78" in body_text
    assert "U12345678" not in body_text
    assert "equity_curve" not in body_text

    data = resp.json()
    assert data["read_only"] is True
    assert data["plans"]["drift_rebalance"]["status"] == "DCA_BUY_ONLY"
    assert len(data["plans"]["drift_rebalance"]["suggested_orders"]) > 0
    assert data["plans"]["trend_200sma_bil"]["status"] == "DCA_BUY_ONLY"

    # Disconnected fallback + override_cash
    disconnected_client = TestClient(_build_test_app(manager=None))
    resp_off = disconnected_client.get("/api/trading/index-core?override_cash=5650")
    assert resp_off.status_code == 200
    off_data = resp_off.json()
    assert off_data["connection"]["connected"] is False
    assert off_data["capital"]["is_simulated_cash"] is True
    assert off_data["capital"]["cash_base"] == 5650.0
    assert off_data["plans"]["drift_rebalance"]["status"] == "INITIAL_ALLOCATION"
