"""期权组合识别测试。"""

from __future__ import annotations

import pytest
from app.services.strategy_detector import StrategyDetector
from app.models.schemas import PositionItem


@pytest.fixture
def detector():
    return StrategyDetector(risk_free_rate=0.05)


def _opt(symbol="AAPL", con_id=1001, position=1, strike=180.0, right="C",
         expiry="20260619", avg_cost=5.0, multiplier=100.0, trading_class="AAPL"):
    return PositionItem(
        account="U12345678", symbol=symbol, sec_type="OPT",
        con_id=con_id, position=position, avg_cost=avg_cost,
        market_price=avg_cost + 1, strike=strike, right=right,
        expiry=expiry, multiplier=multiplier, trading_class=trading_class,
    )


def _stk(symbol="AAPL", con_id=265598, position=100, avg_cost=150.0):
    return PositionItem(
        account="U12345678", symbol=symbol, sec_type="STK",
        con_id=con_id, position=position, avg_cost=avg_cost,
        market_price=avg_cost + 10,
    )


class TestSingleLeg:
    def test_long_call(self, detector):
        combos = detector.detect([_opt(position=1, right="C")])
        assert len(combos) == 1
        assert combos[0].strategy_name == "Long Call"

    def test_short_put(self, detector):
        combos = detector.detect([_opt(position=-1, right="P")])
        assert len(combos) == 1
        assert combos[0].strategy_name == "Short Put"


class TestVerticalSpread:
    def test_bull_call_spread(self, detector):
        positions = [
            _opt(position=1, strike=170, right="C", con_id=1001),
            _opt(position=-1, strike=190, right="C", con_id=1002),
        ]
        combos = detector.detect(positions)
        assert len(combos) == 1
        assert combos[0].strategy_name == "Bull Call Spread"
        assert combos[0].strategy_name_cn == "牛市看涨价差"

    def test_bear_put_spread(self, detector):
        positions = [
            _opt(position=1, strike=190, right="P", con_id=1001),
            _opt(position=-1, strike=170, right="P", con_id=1002),
        ]
        combos = detector.detect(positions)
        assert len(combos) == 1
        assert combos[0].strategy_name == "Bear Put Spread"


class TestStraddleStrangle:
    def test_long_straddle(self, detector):
        positions = [
            _opt(position=1, strike=180, right="C", con_id=1001),
            _opt(position=1, strike=180, right="P", con_id=1002),
        ]
        combos = detector.detect(positions)
        assert combos[0].strategy_name == "Long Straddle"

    def test_long_strangle(self, detector):
        positions = [
            _opt(position=1, strike=190, right="C", con_id=1001),
            _opt(position=1, strike=170, right="P", con_id=1002),
        ]
        combos = detector.detect(positions)
        assert combos[0].strategy_name == "Long Strangle"


class TestIronCondor:
    def test_iron_condor(self, detector):
        positions = [
            _opt(position=1, strike=400, right="P", con_id=2001, symbol="SPY", trading_class="SPY"),
            _opt(position=-1, strike=420, right="P", con_id=2002, symbol="SPY", trading_class="SPY"),
            _opt(position=-1, strike=480, right="C", con_id=2003, symbol="SPY", trading_class="SPY"),
            _opt(position=1, strike=500, right="C", con_id=2004, symbol="SPY", trading_class="SPY"),
        ]
        combos = detector.detect(positions)
        assert len(combos) == 1
        assert combos[0].strategy_name == "Iron Condor"
        assert len(combos[0].legs) == 4


class TestCoveredCall:
    def test_covered_call(self, detector):
        positions = [
            _stk(position=100),
            _opt(position=-1, strike=190, right="C", con_id=1001),
        ]
        combos = detector.detect(positions)
        assert combos[0].strategy_name == "Covered Call"

    def test_protective_put(self, detector):
        positions = [
            _stk(position=100),
            _opt(position=1, strike=170, right="P", con_id=1001),
        ]
        combos = detector.detect(positions)
        assert combos[0].strategy_name == "Protective Put"


class TestCustomMultiLeg:
    def test_three_unmatched_legs(self, detector):
        positions = [
            _opt(position=1, strike=170, right="C", con_id=1001),
            _opt(position=1, strike=180, right="C", con_id=1002),
            _opt(position=-1, strike=190, right="C", con_id=1003),
        ]
        combos = detector.detect(positions)
        assert len(combos) >= 1

    def test_empty_positions(self, detector):
        assert detector.detect([]) == []

    def test_no_options(self, detector):
        assert detector.detect([_stk()]) == []


class TestEdgeCases:
    def test_quantity_mismatch_still_recognized(self, detector):
        """数量不匹配的垂直价差仍应被识别。"""
        positions = [
            _opt(position=2, strike=170, right="C", con_id=1001),
            _opt(position=-1, strike=190, right="C", con_id=1002),
        ]
        combos = detector.detect(positions)
        # 应识别为某种组合（可能是 Vertical Spread 或 Ratio Spread）
        assert len(combos) >= 1
        # 风险提示应包含数量不匹配警告
        combo = combos[0]
        has_qty_warning = any("数量不匹配" in w for w in combo.risk_warnings)
        assert has_qty_warning

    def test_expiry_mismatch_warning(self, detector):
        """到期日不匹配应产生风险提示。"""
        positions = [
            _opt(position=1, strike=170, right="C", expiry="20260619", con_id=1001),
            _opt(position=-1, strike=190, right="C", expiry="20260717", con_id=1002),
        ]
        combos = detector.detect(positions)
        assert len(combos) >= 1
        combo = combos[0]
        has_expiry_warning = any("到期日不一致" in w for w in combo.risk_warnings)
        assert has_expiry_warning

    def test_multiplier_mismatch_warning(self, detector):
        """乘数不匹配应产生风险提示。"""
        positions = [
            _opt(position=1, strike=170, right="C", multiplier=100, con_id=1001),
            _opt(position=-1, strike=190, right="C", multiplier=10, con_id=1002),
        ]
        combos = detector.detect(positions)
        assert len(combos) >= 1
        combo = combos[0]
        has_mult_warning = any("乘数不一致" in w for w in combo.risk_warnings)
        assert has_mult_warning

    def test_same_underlying_different_trading_class(self, detector):
        """不同 trading_class 的期权不应被分到同一组合。"""
        positions = [
            _opt(position=1, strike=170, right="C", con_id=1001, trading_class="AAPL"),
            _opt(position=-1, strike=190, right="C", con_id=1002, trading_class="AAPL2"),
        ]
        combos = detector.detect(positions)
        # 不同 trading_class 应该分别识别
        assert len(combos) >= 1
