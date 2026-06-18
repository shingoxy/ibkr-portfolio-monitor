from app.api.routes_trading import _parse_watchlist


def test_parse_watchlist_trims_and_uppercases_symbols():
    assert _parse_watchlist(" spy, qqq,VTI ") == ["SPY", "QQQ", "VTI"]


def test_parse_watchlist_drops_empty_values():
    assert _parse_watchlist("SPY,,QQQ, ") == ["SPY", "QQQ"]
