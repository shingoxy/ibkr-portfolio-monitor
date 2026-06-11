"""收益计算测试。"""

from __future__ import annotations

import pytest
from app.services.payoff_engine import compute_payoff
from app.models.schemas import OptionLeg


def _leg(strike=100, right="C", position=1, avg_cost=5.0, multiplier=100.0):
    return OptionLeg(
        symbol="TEST", strike=strike, right=right, position=position,
        avg_cost=avg_cost, multiplier=multiplier, expiry="20260619",
    )


class TestSingleLegPayoff:
    def test_long_call(self):
        legs = [_leg(strike=100, right="C", position=1, avg_cost=5.0)]
        payoff = compute_payoff(legs, 100)
        # S=120: intrinsic=20, value=20*100=2000, cost=5*1*100=500, pnl=1500
        assert payoff.max_profit > 0
        assert payoff.max_loss < 0
        assert len(payoff.prices) == 200

    def test_short_put(self):
        legs = [_leg(strike=100, right="P", position=-1, avg_cost=5.0)]
        payoff = compute_payoff(legs, 100)
        # 空头卖 put: 收到权利金 500, 最大亏损 = 100*100 - 500 = 9500
        assert payoff.max_profit == 500.0  # 收到的权利金
        assert payoff.max_loss < 0

    def test_long_put(self):
        legs = [_leg(strike=100, right="P", position=1, avg_cost=3.0)]
        payoff = compute_payoff(legs, 100)
        # S=80: intrinsic=20, value=20*100=2000, cost=3*100=300, pnl=1700
        assert payoff.max_profit > 0
        assert payoff.max_loss == -300.0


class TestSpreadPayoff:
    def test_bull_call_spread(self):
        legs = [
            _leg(strike=100, right="C", position=1, avg_cost=5.0),
            _leg(strike=110, right="C", position=-1, avg_cost=2.0),
        ]
        payoff = compute_payoff(legs, 105)
        # 净成本 = 5*1*100 + (-2)*(-1)*100 = 500 - 200 = 300
        # 最大盈利 = (110-100)*100 - 300 = 700
        assert payoff.max_profit == 700.0
        assert payoff.max_loss == -300.0
        assert len(payoff.breakevens) == 1

    def test_bear_put_spread(self):
        legs = [
            _leg(strike=110, right="P", position=1, avg_cost=7.0),
            _leg(strike=100, right="P", position=-1, avg_cost=3.0),
        ]
        payoff = compute_payoff(legs, 105)
        # 净成本 = 7*100 - 3*100 = 400
        # 最大盈利 = (110-100)*100 - 400 = 600
        assert payoff.max_profit == 600.0
        assert payoff.max_loss == -400.0


class TestMultiplier:
    def test_multiplier_100(self):
        legs = [_leg(strike=100, right="C", position=1, avg_cost=5.0, multiplier=100)]
        payoff = compute_payoff(legs, 100)
        # cost = 5 * 1 * 100 = 500
        assert payoff.max_loss == -500.0

    def test_multiplier_non_100(self):
        legs = [_leg(strike=100, right="C", position=1, avg_cost=5.0, multiplier=10)]
        payoff = compute_payoff(legs, 100)
        # cost = 5 * 1 * 10 = 50
        assert payoff.max_loss == -50.0

    def test_multiple_contracts(self):
        legs = [_leg(strike=100, right="C", position=3, avg_cost=5.0, multiplier=100)]
        payoff = compute_payoff(legs, 100)
        # cost = 5 * 3 * 100 = 1500
        assert payoff.max_loss == -1500.0


class TestCommission:
    def test_commission_increases_cost(self):
        legs = [_leg(strike=100, right="C", position=1, avg_cost=5.0)]
        payoff_no_comm = compute_payoff(legs, 100, commission_per_contract=0)
        payoff_comm = compute_payoff(legs, 100, commission_per_contract=1.0)
        # 手续费增加成本，最大亏损更大
        assert payoff_comm.max_loss < payoff_no_comm.max_loss
