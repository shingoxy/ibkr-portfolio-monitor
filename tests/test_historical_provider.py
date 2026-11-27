from datetime import date
from types import SimpleNamespace

import pytest

from app.ibkr.client import IBKRClient
from app.ibkr.request_ids import RequestIdManager
from app.trading.historical_provider import IBKRHistoricalBarProvider, InMemoryHistoricalBarProvider
from app.trading.models import Bar


@pytest.mark.asyncio
async def test_in_memory_provider_returns_symbol_bars_sorted():
    provider = InMemoryHistoricalBarProvider(
        {
            "SPY": [
                Bar(symbol="SPY", date=date(2026, 1, 2), open=101, high=102, low=100, close=101, volume=1),
                Bar(symbol="SPY", date=date(2026, 1, 1), open=100, high=101, low=99, close=100, volume=1),
            ]
        }
    )
    bars = await provider.get_daily_bars("SPY", lookback_days=200)
    assert [bar.date.isoformat() for bar in bars] == ["2026-01-01", "2026-01-02"]


@pytest.mark.asyncio
async def test_in_memory_provider_returns_empty_for_missing_symbol():
    provider = InMemoryHistoricalBarProvider({})
    assert await provider.get_daily_bars("QQQ", lookback_days=200) == []


@pytest.mark.asyncio
async def test_ibkr_provider_requests_daily_bars_and_returns_sorted_bars():
    client = _FakeHistoricalClient(
        [
            {
                "req_id": 1000,
                "symbol": "SPY",
                "date": "20260102",
                "open": 101.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.5,
                "volume": 2000,
            },
            {
                "req_id": 1000,
                "symbol": "SPY",
                "date": "20260101",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000,
            },
        ],
        [{"req_id": 1000, "start": "20260101", "end": "20260102"}],
    )
    request_ids = RequestIdManager()
    provider = IBKRHistoricalBarProvider(client, request_ids, timeout_seconds=0.05, poll_interval=0)

    bars = await provider.get_daily_bars("SPY", lookback_days=3)

    assert [bar.date.isoformat() for bar in bars] == ["2026-01-01", "2026-01-02"]
    assert client.requests[0]["durationStr"] == "3 D"
    assert client.requests[0]["barSizeSetting"] == "1 day"
    assert client.requests[0]["whatToShow"] == "TRADES"
    assert client.requests[0]["useRTH"] == 1
    assert request_ids.get_by_category("historical") == []


@pytest.mark.asyncio
async def test_ibkr_provider_uses_year_duration_and_adjusted_prices_for_long_history():
    client = _FakeHistoricalClient([], [{"req_id": 1000, "start": "", "end": ""}])
    provider = IBKRHistoricalBarProvider(
        client,
        RequestIdManager(),
        timeout_seconds=0.05,
        poll_interval=0,
        what_to_show="ADJUSTED_LAST",
    )

    await provider.get_daily_bars("NVDA", lookback_days=1095)

    assert client.requests[0]["durationStr"] == "3 Y"
    assert client.requests[0]["whatToShow"] == "ADJUSTED_LAST"


@pytest.mark.asyncio
async def test_ibkr_provider_preserves_interleaved_bars_for_next_request():
    client = _FakeHistoricalClient(
        [
            {
                "req_id": 1001,
                "symbol": "QQQ",
                "date": "20260102",
                "open": 201.0,
                "high": 202.0,
                "low": 200.0,
                "close": 201.5,
                "volume": 2000,
            },
            {
                "req_id": 1000,
                "symbol": "SPY",
                "date": "20260101",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000,
            },
        ],
        [
            {"req_id": 1001, "start": "20260102", "end": "20260102"},
            {"req_id": 1000, "start": "20260101", "end": "20260101"},
        ],
    )
    provider = IBKRHistoricalBarProvider(
        client,
        RequestIdManager(),
        timeout_seconds=0.05,
        poll_interval=0,
    )

    spy_bars = await provider.get_daily_bars("SPY", lookback_days=3)
    qqq_bars = await provider.get_daily_bars("QQQ", lookback_days=3)

    assert [bar.date.isoformat() for bar in spy_bars] == ["2026-01-01"]
    assert [bar.date.isoformat() for bar in qqq_bars] == ["2026-01-02"]


@pytest.mark.asyncio
async def test_ibkr_provider_raises_timeout_when_request_never_completes():
    client = _FakeHistoricalClient([], [])
    provider = IBKRHistoricalBarProvider(
        client,
        RequestIdManager(),
        timeout_seconds=0.01,
        poll_interval=0,
    )

    with pytest.raises(TimeoutError, match="historical data timeout"):
        await provider.get_daily_bars("SPY", lookback_days=3)


def test_ibkr_client_historical_callbacks_are_drained():
    client = IBKRClient()
    bar = SimpleNamespace(
        date="20260102",
        open=101.0,
        high=102.0,
        low=100.0,
        close=101.5,
        volume=2000,
    )

    client.historicalData(123, bar)
    client.historicalDataEnd(123, "20260101", "20260102")

    assert client.drain_historical_data() == [
        {
            "req_id": 123,
            "date": "20260102",
            "open": 101.0,
            "high": 102.0,
            "low": 100.0,
            "close": 101.5,
            "volume": 2000,
        }
    ]
    assert client.drain_historical_data_end() == [
        {"req_id": 123, "start": "20260101", "end": "20260102"}
    ]


class _FakeHistoricalClient:
    def __init__(self, data: list[dict], ends: list[dict]) -> None:
        self._data = data
        self._ends = ends
        self.requests: list[dict] = []

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
    ):
        self.requests.append(
            {
                "reqId": reqId,
                "symbol": contract.symbol,
                "endDateTime": endDateTime,
                "durationStr": durationStr,
                "barSizeSetting": barSizeSetting,
                "whatToShow": whatToShow,
                "useRTH": useRTH,
                "formatDate": formatDate,
                "keepUpToDate": keepUpToDate,
                "chartOptions": chartOptions,
            }
        )

    def drain_historical_data(self) -> list[dict]:
        data = list(self._data)
        self._data.clear()
        return data

    def drain_historical_data_end(self) -> list[dict]:
        ends = list(self._ends)
        self._ends.clear()
        return ends
