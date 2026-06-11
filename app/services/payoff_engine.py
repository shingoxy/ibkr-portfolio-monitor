"""到期收益计算引擎。"""

from __future__ import annotations

from typing import Optional

import numpy as np

from app.models.schemas import OptionLeg, PayoffCurve, PositionItem


def compute_payoff(
    legs: list[OptionLeg],
    current_price: float,
    stock: Optional[PositionItem] = None,
    commission_per_contract: float = 0.0,
) -> PayoffCurve:
    """计算到期收益曲线。

    每条腿的到期价值:
      Call long:  max(S - K, 0) * multiplier * quantity
      Call short: -max(S - K, 0) * multiplier * abs(quantity)
      Put long:   max(K - S, 0) * multiplier * quantity
      Put short:  -max(K - S, 0) * multiplier * abs(quantity)

    净成本 = sum(avg_cost * position * multiplier)
      正 position (多头) → 付出权利金
      负 position (空头) → 收到权利金
    """
    if not legs:
        return PayoffCurve()

    strikes = [l.strike for l in legs if l.strike > 0]
    if not strikes or current_price <= 0:
        return PayoffCurve()

    min_strike = min(strikes)
    max_strike = max(strikes)
    price_min = max(0, min_strike * 0.5)
    price_max = max_strike * 1.5
    price_min = min(price_min, current_price * 0.5)
    price_max = max(price_max, current_price * 1.5)

    prices = np.linspace(price_min, price_max, 200).tolist()
    pnl_values = []

    # 计算净成本
    net_cost = 0.0
    for leg in legs:
        # avg_cost 是每股成本，position 是合约数，multiplier 是乘数
        net_cost += leg.avg_cost * leg.position * leg.multiplier
    if stock:
        net_cost += stock.avg_cost * stock.position

    # 加上手续费
    total_contracts = sum(abs(leg.position) for leg in legs)
    net_cost += total_contracts * commission_per_contract

    for S in prices:
        total = 0.0
        for leg in legs:
            if leg.right == "C":
                intrinsic = max(S - leg.strike, 0)
            else:
                intrinsic = max(leg.strike - S, 0)
            # position 正=多头, 负=空头
            # 多头: +intrinsic, 空头: -intrinsic
            total += leg.position * leg.multiplier * intrinsic
        if stock:
            total += stock.position * (S - stock.avg_cost)
        pnl_values.append(total - net_cost)

    # 找盈亏平衡点
    breakevens = []
    for i in range(len(pnl_values) - 1):
        if pnl_values[i] * pnl_values[i + 1] < 0:
            x0, x1 = prices[i], prices[i + 1]
            y0, y1 = pnl_values[i], pnl_values[i + 1]
            if abs(y1 - y0) > 1e-12:
                be = x0 - y0 * (x1 - x0) / (y1 - y0)
                breakevens.append(round(be, 2))

    return PayoffCurve(
        underlying=legs[0].symbol,
        current_price=current_price,
        prices=[round(p, 2) for p in prices],
        pnl=[round(v, 2) for v in pnl_values],
        max_profit=round(max(pnl_values), 2),
        max_loss=round(min(pnl_values), 2),
        breakevens=breakevens,
    )
