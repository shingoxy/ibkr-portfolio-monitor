"""风险评估服务。"""

from __future__ import annotations

from typing import Optional

from app.models.schemas import OptionLeg, PositionItem


def assess_risk(
    legs: list[OptionLeg],
    stock: Optional[PositionItem] = None,
    days_to_expiry: int = 0,
) -> list[str]:
    """评估期权组合风险，返回风险提示列表。"""
    warnings: list[str] = []

    # 检查空头期权
    short_legs = [l for l in legs if l.position < 0]
    for leg in short_legs:
        if leg.right == "C":
            warnings.append(
                f"空头看涨期权 {leg.symbol} {leg.strike}C: "
                f"理论亏损无上限，存在指派风险"
            )
        elif leg.right == "P":
            warnings.append(
                f"空头看跌期权 {leg.symbol} {leg.strike}P: "
                f"标的价格跌至 0 时最大亏损 = {leg.strike * leg.multiplier * abs(leg.position):.0f}"
            )

    # 临近到期风险
    if days_to_expiry <= 7 and short_legs:
        warnings.append(
            f"距到期仅 {days_to_expiry} 天，空头期权 Gamma 风险显著增大，"
            f"价格波动可能导致快速指派"
        )

    # 数量不匹配检查
    if len(legs) == 2:
        l0, l1 = legs[0], legs[1]
        if abs(abs(l0.position) - abs(l1.position)) > 0.01:
            warnings.append(
                f"数量不匹配: {l0.position:.0f} vs {l1.position:.0f}，"
                f"存在未覆盖的裸露腿"
            )
        if l0.multiplier != l1.multiplier:
            warnings.append(
                f"合约乘数不一致: {l0.multiplier} vs {l1.multiplier}，"
                f"收益计算可能不准确"
            )
        if l0.expiry != l1.expiry:
            warnings.append(
                f"到期日不一致: {l0.expiry} vs {l1.expiry}，"
                f"不是标准价差策略"
            )

    # 多头期权最大亏损提示
    long_legs = [l for l in legs if l.position > 0]
    if long_legs and not short_legs:
        total_cost = sum(l.avg_cost * l.position * l.multiplier for l in long_legs)
        warnings.append(f"最大亏损为已付权利金: {total_cost:.2f}")

    return warnings
