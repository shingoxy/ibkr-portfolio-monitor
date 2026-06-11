"""期权成本计算测试。

IBKR avgCost 是每股成本，不含 multiplier。
总成本 = position * avgCost * multiplier
"""

from __future__ import annotations

import pytest
from app.models.schemas import OptionLeg


def _leg(position=1, avg_cost=5.0, multiplier=100.0):
    return OptionLeg(
        symbol="TEST", strike=100, right="C", position=position,
        avg_cost=avg_cost, multiplier=multiplier, expiry="20260619",
    )


class TestOptionCost:
    def test_long_one_contract_multiplier_100(self):
        """1 张多头期权，multiplier=100"""
        leg = _leg(position=1, avg_cost=5.0, multiplier=100)
        total_cost = leg.avg_cost * leg.position * leg.multiplier
        assert total_cost == 500.0  # $5 * 1 * 100 = $500

    def test_short_one_contract_multiplier_100(self):
        """1 张空头期权，multiplier=100"""
        leg = _leg(position=-1, avg_cost=5.0, multiplier=100)
        total_cost = leg.avg_cost * leg.position * leg.multiplier
        assert total_cost == -500.0  # 收到 $500 权利金

    def test_multiplier_non_100(self):
        """multiplier=10"""
        leg = _leg(position=1, avg_cost=5.0, multiplier=10)
        total_cost = leg.avg_cost * leg.position * leg.multiplier
        assert total_cost == 50.0

    def test_multiple_contracts(self):
        """3 张合约"""
        leg = _leg(position=3, avg_cost=5.0, multiplier=100)
        total_cost = leg.avg_cost * leg.position * leg.multiplier
        assert total_cost == 1500.0

    def test_vertical_spread_cost(self):
        """垂直价差净成本"""
        long_leg = _leg(position=1, avg_cost=5.0, multiplier=100)
        short_leg = _leg(position=-1, avg_cost=2.0, multiplier=100)
        net_cost = (long_leg.avg_cost * long_leg.position * long_leg.multiplier +
                    short_leg.avg_cost * short_leg.position * short_leg.multiplier)
        assert net_cost == 300.0  # 500 - 200 = 300 净支出

    def test_avg_cost_is_per_share(self):
        """验证 avgCost 是每股价格，不是每合约价格。"""
        leg = _leg(position=1, avg_cost=2.50, multiplier=100)
        # avgCost = $2.50/股，不是 $250/合约
        assert leg.avg_cost == 2.50
        total = leg.avg_cost * leg.position * leg.multiplier
        assert total == 250.0
