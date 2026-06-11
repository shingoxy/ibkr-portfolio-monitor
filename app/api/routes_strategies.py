"""期权策略路由。"""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse

from app.services.strategy_detector import StrategyDetector
from app.services.payoff_engine import compute_payoff
from app.services.option_pricing import black_scholes_price

router = APIRouter()
_detector = StrategyDetector()


def _get_combos(manager):
    snapshot = manager.cache.get_snapshot()
    positions = snapshot["positions"]
    mkt = snapshot.get("market_data", {})
    combos = _detector.detect(positions, mkt)
    for i, c in enumerate(combos):
        c.id = i + 1
    return combos


@router.get("/strategies", response_class=HTMLResponse)
async def strategies_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "strategies.html"
    )


@router.get("/api/strategies")
async def api_strategies(request: Request):
    manager = request.app.state.manager
    combos = _get_combos(manager)
    return [c.model_dump() for c in combos]


@router.get("/api/strategies/{combo_id}/payoff")
async def api_combo_payoff(combo_id: int, request: Request):
    manager = request.app.state.manager
    combos = _get_combos(manager)
    combo = next((c for c in combos if c.id == combo_id), None)
    if not combo:
        return {"error": "组合未找到"}
    und_price = combo.legs[0].und_price if combo.legs else 0
    if und_price <= 0:
        strikes = [l.strike for l in combo.legs if l.strike > 0]
        und_price = sum(strikes) / len(strikes) if strikes else 0
    payoff = compute_payoff(combo.legs, und_price)
    return payoff.model_dump()


@router.get("/api/strategies/{combo_id}/scenario")
async def api_combo_scenario(
    combo_id: int, request: Request,
    volatility: float = Query(0.3, description="基准波动率"),
    days: int = Query(30, description="分析天数"),
    iv_change_steps: str = Query("-20,-10,0,10,20", description="IV 变化百分比"),
):
    """情景分析：价格 × 时间 × IV 变化。"""
    manager = request.app.state.manager
    combos = _get_combos(manager)
    combo = next((c for c in combos if c.id == combo_id), None)
    if not combo:
        return {"error": "组合未找到"}

    current_price = combo.legs[0].und_price if combo.legs else 0
    if current_price <= 0:
        strikes = [l.strike for l in combo.legs if l.strike > 0]
        current_price = sum(strikes) / len(strikes) if strikes else 100

    strikes = [l.strike for l in combo.legs if l.strike > 0]
    price_min = max(0, min(strikes) * 0.7)
    price_max = max(strikes) * 1.3

    price_steps = np.linspace(price_min, price_max, 30).tolist()
    date_steps = list(range(0, days + 1, 5))
    iv_changes = [float(x) for x in iv_change_steps.split(",")]

    net_cost = sum(l.avg_cost * l.position * l.multiplier for l in combo.legs)

    # 生成多个 IV 变化下的矩阵
    scenarios = {}
    for iv_pct in iv_changes:
        adj_vol = volatility * (1 + iv_pct / 100)
        matrix = []
        for d in date_steps:
            row = []
            T = (days - d) / 365.0
            for S in price_steps:
                total = 0.0
                for leg in combo.legs:
                    if T <= 0:
                        intrinsic = max(S - leg.strike, 0) if leg.right == "C" else max(leg.strike - S, 0)
                        total += leg.position * leg.multiplier * intrinsic
                    else:
                        price = black_scholes_price(leg.right, S, leg.strike, T, 0.05, adj_vol)
                        total += leg.position * leg.multiplier * price
                row.append(round(total - net_cost, 2))
            matrix.append(row)
        scenarios[f"iv_{iv_pct:+.0f}%"] = matrix

    return {
        "combo_id": combo_id,
        "strategy_name": combo.strategy_name_cn,
        "underlying": combo.underlying,
        "price_steps": [round(p, 2) for p in price_steps],
        "date_steps": [f"Day {d}" for d in date_steps],
        "iv_changes": iv_changes,
        "base_volatility": volatility,
        "scenarios": scenarios,
        "matrix": scenarios.get("iv_+0%", []),  # 默认 IV 不变
    }
