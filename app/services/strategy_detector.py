"""期权组合自动识别引擎。

支持识别：单腿、垂直价差、Straddle、Strangle、Iron Condor、Butterfly、
Calendar/Diagonal Spread、Covered Call、Protective Put、Ratio Spread 等。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from app.models.schemas import PositionItem, OptionLeg, OptionCombo
from app.services.option_pricing import compute_greeks
from app.services.payoff_engine import compute_payoff
from app.services.risk_service import assess_risk

logger = logging.getLogger(__name__)

STRATEGY_NAMES = {
    "LONG_CALL": ("Long Call", "买入看涨"),
    "LONG_PUT": ("Long Put", "买入看跌"),
    "SHORT_CALL": ("Short Call", "卖出看涨"),
    "SHORT_PUT": ("Short Put", "卖出看跌"),
    "BULL_CALL_SPREAD": ("Bull Call Spread", "牛市看涨价差"),
    "BEAR_PUT_SPREAD": ("Bear Put Spread", "熊市看跌价差"),
    "BULL_PUT_SPREAD": ("Bull Put Spread", "牛市看跌价差"),
    "BEAR_CALL_SPREAD": ("Bear Call Spread", "熊市看涨价差"),
    "LONG_STRADDLE": ("Long Straddle", "买入跨式"),
    "SHORT_STRADDLE": ("Short Straddle", "卖出跨式"),
    "LONG_STRANGLE": ("Long Strangle", "买入宽跨式"),
    "SHORT_STRANGLE": ("Short Strangle", "卖出宽跨式"),
    "IRON_CONDOR": ("Iron Condor", "铁鹰式"),
    "IRON_BUTTERFLY": ("Iron Butterfly", "铁蝶式"),
    "LONG_CALL_BUTTERFLY": ("Long Call Butterfly", "买入看涨蝶式"),
    "LONG_PUT_BUTTERFLY": ("Long Put Butterfly", "买入看跌蝶式"),
    "CALENDAR_SPREAD": ("Calendar Spread", "日历价差"),
    "DIAGONAL_SPREAD": ("Diagonal Spread", "对角价差"),
    "COVERED_CALL": ("Covered Call", "备兑看涨"),
    "PROTECTIVE_PUT": ("Protective Put", "保护性看跌"),
    "COLLAR": ("Collar", "领口策略"),
    "RATIO_SPREAD": ("Ratio Spread", "比例价差"),
    "CUSTOM_MULTI_LEG": ("Custom Multi-Leg", "自定义多腿组合"),
}


class StrategyDetector:
    """期权组合识别器。"""

    def __init__(self, risk_free_rate: float = 0.05) -> None:
        self._r = risk_free_rate

    def detect(self, positions: list[dict] | list[PositionItem],
               market_data: dict[int, dict] | None = None) -> list[OptionCombo]:
        """从持仓数据识别期权组合。"""
        market_data = market_data or {}

        stock_positions: dict[str, PositionItem] = {}
        option_positions: list[PositionItem] = []

        for pos in positions:
            # 兼容 dict 和 PositionItem
            sec_type = pos.sec_type if isinstance(pos, PositionItem) else pos.get("sec_type", "")
            if sec_type == "STK":
                p = pos if isinstance(pos, PositionItem) else PositionItem(**pos)
                stock_positions[p.symbol] = p
            elif sec_type == "OPT":
                p = pos if isinstance(pos, PositionItem) else PositionItem(**pos)
                option_positions.append(p)

        if not option_positions:
            return []

        # 按标的分组
        by_underlying: dict[str, list[PositionItem]] = defaultdict(list)
        for pos in option_positions:
            by_underlying[pos.trading_class or pos.symbol].append(pos)

        combos: list[OptionCombo] = []
        for underlying, opts in by_underlying.items():
            legs = [self._to_option_leg(opt, market_data) for opt in opts]
            identified = self._identify_for_underlying(
                underlying, legs, stock_positions.get(underlying)
            )
            combos.extend(identified)

        return combos

    def _to_option_leg(self, pos: PositionItem, market_data: dict[int, dict]) -> OptionLeg:
        greeks = market_data.get(pos.con_id, {})
        return OptionLeg(
            con_id=pos.con_id, symbol=pos.symbol, sec_type="OPT",
            strike=pos.strike, right=pos.right, expiry=pos.expiry,
            position=pos.position, avg_cost=pos.avg_cost,
            market_price=pos.market_price, multiplier=pos.multiplier,
            delta=greeks.get("delta") or 0,
            gamma=greeks.get("gamma") or 0,
            vega=greeks.get("vega") or 0,
            theta=greeks.get("theta") or 0,
            implied_vol=greeks.get("implied_vol") or 0,
            und_price=greeks.get("und_price") or 0,
        )

    def _identify_for_underlying(
        self, underlying: str, legs: list[OptionLeg],
        stock: Optional[PositionItem] = None,
    ) -> list[OptionCombo]:
        result: list[OptionCombo] = []
        used: set[int] = set()

        by_expiry: dict[str, list[tuple[int, OptionLeg]]] = defaultdict(list)
        for i, leg in enumerate(legs):
            by_expiry[leg.expiry].append((i, leg))

        # 1. Iron Condor (4 腿)
        for expiry, exp_legs in by_expiry.items():
            if len(exp_legs) >= 4:
                combo = self._try_iron_condor(underlying, exp_legs)
                if combo:
                    result.append(combo)
                    for idx, _ in exp_legs:
                        used.add(idx)

        # 2. Iron Butterfly (4 腿)
        for expiry, exp_legs in by_expiry.items():
            remaining = [(i, l) for i, l in exp_legs if i not in used]
            if len(remaining) >= 4:
                combo = self._try_iron_butterfly(underlying, remaining)
                if combo:
                    result.append(combo)
                    for idx, _ in remaining:
                        used.add(idx)

        # 3. Butterfly (3 行权价 1:2:1)
        for expiry, exp_legs in by_expiry.items():
            remaining = [(i, l) for i, l in exp_legs if i not in used]
            if len(remaining) >= 3:
                combo = self._try_butterfly(underlying, remaining)
                if combo:
                    result.append(combo)
                    for idx, _ in remaining:
                        used.add(idx)

        # 4. 2 腿策略
        for expiry, exp_legs in by_expiry.items():
            remaining = [(i, l) for i, l in exp_legs if i not in used]
            if len(remaining) >= 2:
                combos_2 = self._try_multi_two_leg(underlying, remaining, used)
                for combo in combos_2:
                    result.append(combo)

        # 5. 跨到期日策略
        cross_legs = [(i, l) for i, l in enumerate(legs) if i not in used]
        if len(cross_legs) >= 2:
            combo = self._try_calendar(underlying, cross_legs)
            if combo:
                result.append(combo)
                for idx, _ in cross_legs:
                    used.add(idx)

        # 6. 结合股票持仓
        if stock:
            remaining = [(i, l) for i, l in enumerate(legs) if i not in used]
            for idx, leg in remaining:
                combo = self._try_stock_option(underlying, leg, stock)
                if combo:
                    result.append(combo)
                    used.add(idx)

        # 7. 未匹配的单腿或自定义多腿
        remaining = [(i, l) for i, l in enumerate(legs) if i not in used]
        if len(remaining) == 1:
            result.append(self._make_single_leg(underlying, remaining[0][1]))
        elif len(remaining) > 1:
            result.append(self._make_custom(underlying, [l for _, l in remaining]))

        return result

    def _try_iron_condor(self, underlying: str, exp_legs: list[tuple[int, OptionLeg]]) -> Optional[OptionCombo]:
        sorted_legs = sorted(exp_legs, key=lambda x: x[1].strike)
        if len(sorted_legs) < 4:
            return None
        l0, l1, l2, l3 = sorted_legs[0][1], sorted_legs[1][1], sorted_legs[2][1], sorted_legs[3][1]
        if (l0.right == "P" and l0.position > 0 and
            l1.right == "P" and l1.position < 0 and
            l2.right == "C" and l2.position < 0 and
            l3.right == "C" and l3.position > 0 and
            l0.expiry == l1.expiry == l2.expiry == l3.expiry):
            return self._build_combo(underlying, "IRON_CONDOR", [l0, l1, l2, l3])
        return None

    def _try_iron_butterfly(self, underlying: str, exp_legs: list[tuple[int, OptionLeg]]) -> Optional[OptionCombo]:
        sorted_legs = sorted(exp_legs, key=lambda x: x[1].strike)
        if len(sorted_legs) < 4:
            return None
        l0, l1, l2, l3 = sorted_legs[0][1], sorted_legs[1][1], sorted_legs[2][1], sorted_legs[3][1]
        # Iron Butterfly: Buy Put@A, Sell Put@B, Sell Call@B, Buy Call@C (A<B=C)
        if (l0.right == "P" and l0.position > 0 and
            l1.right == "P" and l1.position < 0 and
            l2.right == "C" and l2.position < 0 and
            l3.right == "C" and l3.position > 0 and
            l1.strike == l2.strike):
            return self._build_combo(underlying, "IRON_BUTTERFLY", [l0, l1, l2, l3])
        return None

    def _try_butterfly(self, underlying: str, exp_legs: list[tuple[int, OptionLeg]]) -> Optional[OptionCombo]:
        sorted_legs = sorted(exp_legs, key=lambda x: x[1].strike)
        strike_groups: dict[float, list[OptionLeg]] = defaultdict(list)
        for _, leg in sorted_legs:
            strike_groups[leg.strike].append(leg)
        strikes = sorted(strike_groups.keys())
        if len(strikes) != 3:
            return None
        a_net = sum(l.position for l in strike_groups[strikes[0]])
        b_net = sum(l.position for l in strike_groups[strikes[1]])
        c_net = sum(l.position for l in strike_groups[strikes[2]])
        all_legs_flat = [l for _, l in sorted_legs]
        all_same_type = len(set(l.right for l in all_legs_flat)) == 1
        if all_same_type and abs(a_net) > 0:
            if abs(b_net / a_net + 2) < 0.5 and abs(c_net / a_net - 1) < 0.5:
                key = "LONG_CALL_BUTTERFLY" if all_legs_flat[0].right == "C" else "LONG_PUT_BUTTERFLY"
                return self._build_combo(underlying, key, all_legs_flat)
        return None

    def _try_multi_two_leg(self, underlying: str, exp_legs: list[tuple[int, OptionLeg]],
                           used: set[int]) -> list[OptionCombo]:
        """尝试匹配多个 2 腿策略。"""
        results = []
        remaining = list(exp_legs)
        while len(remaining) >= 2:
            combo = self._try_two_leg(underlying, remaining[:2])
            if combo:
                results.append(combo)
                for idx, _ in remaining[:2]:
                    used.add(idx)
                remaining = remaining[2:]
            else:
                remaining = remaining[1:]
        return results

    def _try_two_leg(self, underlying: str, pair: list[tuple[int, OptionLeg]]) -> Optional[OptionCombo]:
        leg0, leg1 = pair[0][1], pair[1][1]
        sorted_legs = sorted([leg0, leg1], key=lambda x: x.strike)
        if leg0.expiry != leg1.expiry:
            return None
        if leg0.right == leg1.right:
            return self._identify_vertical(underlying, sorted_legs)
        if leg0.strike == leg1.strike:
            both_long = leg0.position > 0 and leg1.position > 0
            both_short = leg0.position < 0 and leg1.position < 0
            if both_long:
                return self._build_combo(underlying, "LONG_STRADDLE", [leg0, leg1])
            elif both_short:
                return self._build_combo(underlying, "SHORT_STRADDLE", [leg0, leg1])
        if leg0.strike != leg1.strike:
            both_long = leg0.position > 0 and leg1.position > 0
            both_short = leg0.position < 0 and leg1.position < 0
            if both_long:
                return self._build_combo(underlying, "LONG_STRANGLE", [leg0, leg1])
            elif both_short:
                return self._build_combo(underlying, "SHORT_STRANGLE", [leg0, leg1])
        return None

    def _identify_vertical(self, underlying: str, legs: list[OptionLeg]) -> Optional[OptionCombo]:
        leg_a, leg_b = legs[0], legs[1]
        long_leg = leg_a if leg_a.position > 0 else leg_b
        short_leg = leg_a if leg_a.position < 0 else leg_b
        if leg_a.right == "C":
            if long_leg.strike < short_leg.strike:
                return self._build_combo(underlying, "BULL_CALL_SPREAD", legs)
            else:
                return self._build_combo(underlying, "BEAR_CALL_SPREAD", legs)
        elif leg_a.right == "P":
            if long_leg.strike > short_leg.strike:
                return self._build_combo(underlying, "BEAR_PUT_SPREAD", legs)
            else:
                return self._build_combo(underlying, "BULL_PUT_SPREAD", legs)
        return None

    def _try_calendar(self, underlying: str, legs: list[tuple[int, OptionLeg]]) -> Optional[OptionCombo]:
        if len(legs) < 2:
            return None
        for i in range(len(legs)):
            for j in range(i + 1, len(legs)):
                l0, l1 = legs[i][1], legs[j][1]
                if l0.right == l1.right and l0.expiry != l1.expiry:
                    if l0.strike == l1.strike:
                        return self._build_combo(underlying, "CALENDAR_SPREAD", [l0, l1])
                    else:
                        return self._build_combo(underlying, "DIAGONAL_SPREAD", [l0, l1])
        return None

    def _try_stock_option(self, underlying: str, leg: OptionLeg,
                          stock: PositionItem) -> Optional[OptionCombo]:
        if stock.position <= 0:
            return None
        if leg.right == "C" and leg.position < 0:
            return self._build_combo(underlying, "COVERED_CALL", [leg], stock)
        elif leg.right == "P" and leg.position > 0:
            return self._build_combo(underlying, "PROTECTIVE_PUT", [leg], stock)
        return None

    def _make_single_leg(self, underlying: str, leg: OptionLeg) -> OptionCombo:
        if leg.right == "C":
            name = "LONG_CALL" if leg.position > 0 else "SHORT_CALL"
        else:
            name = "LONG_PUT" if leg.position > 0 else "SHORT_PUT"
        return self._build_combo(underlying, name, [leg])

    def _make_custom(self, underlying: str, legs: list[OptionLeg]) -> OptionCombo:
        return self._build_combo(underlying, "CUSTOM_MULTI_LEG", legs)

    def _build_combo(self, underlying: str, strategy_key: str,
                     legs: list[OptionLeg], stock: Optional[PositionItem] = None) -> OptionCombo:
        en_name, cn_name = STRATEGY_NAMES.get(strategy_key, ("Custom", "自定义"))

        # 净权利金
        net_premium = sum(leg.avg_cost * leg.position * leg.multiplier for leg in legs)

        # Greeks 汇总
        total_delta = sum(leg.delta * leg.position for leg in legs)
        total_gamma = sum(leg.gamma * leg.position for leg in legs)
        total_vega = sum(leg.vega * leg.position for leg in legs)
        total_theta = sum(leg.theta * leg.position for leg in legs)

        # 收益曲线
        und_price = legs[0].und_price if legs else 0
        if und_price <= 0 and legs:
            strikes = [l.strike for l in legs if l.strike > 0]
            und_price = sum(strikes) / len(strikes) if strikes else 0
        payoff = compute_payoff(legs, und_price, stock)

        # 到期天数
        days_to_expiry = 0
        if legs and legs[0].expiry:
            try:
                exp_date = datetime.strptime(legs[0].expiry, "%Y%m%d")
                days_to_expiry = max(0, (exp_date - datetime.now()).days)
            except ValueError:
                pass

        # 风险评估
        risk_warnings = assess_risk(legs, stock, days_to_expiry)

        return OptionCombo(
            underlying=underlying,
            strategy_name=en_name,
            strategy_name_cn=cn_name,
            legs=legs,
            max_profit=payoff.max_profit,
            max_loss=payoff.max_loss,
            breakevens=payoff.breakevens,
            net_debit_credit=net_premium,
            total_delta=total_delta,
            total_gamma=total_gamma,
            total_vega=total_vega,
            total_theta=total_theta,
            days_to_expiry=days_to_expiry,
            risk_warnings=risk_warnings,
        )
