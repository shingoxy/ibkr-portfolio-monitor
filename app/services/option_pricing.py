"""Black-Scholes 期权定价与 Greeks 计算。"""

from __future__ import annotations

import math
from scipy.stats import norm


def black_scholes_price(
    option_type: str, S: float, K: float, T: float,
    r: float, sigma: float, q: float = 0.0,
) -> float:
    """计算 Black-Scholes 期权价格。

    Args:
        option_type: "C" 或 "P"
        S: 标的价格
        K: 行权价
        T: 到期时间（年）
        r: 无风险利率
        sigma: 波动率
        q: 股息率
    """
    if T <= 0 or sigma <= 0:
        if option_type == "C":
            return max(S - K, 0)
        return max(K - S, 0)

    d1 = (math.log(S / K) + (r - q + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "C":
        return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)


def compute_greeks(
    option_type: str, S: float, K: float, T: float,
    r: float, sigma: float, q: float = 0.0,
) -> dict:
    """计算单个期权的 Greeks（每单位，不含 multiplier）。"""
    if T <= 0 or sigma <= 0:
        return {"delta": 0, "gamma": 0, "vega": 0, "theta": 0, "rho": 0}

    d1 = (math.log(S / K) + (r - q + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    sign = 1 if option_type == "C" else -1

    delta = sign * math.exp(-q * T) * norm.cdf(sign * d1)
    gamma = math.exp(-q * T) * norm.pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T) / 100
    theta = (-(S * math.exp(-q * T) * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
             - sign * r * K * math.exp(-r * T) * norm.cdf(sign * d2)
             + sign * q * S * math.exp(-q * T) * norm.cdf(sign * d1)) / 365
    rho = sign * K * T * math.exp(-r * T) * norm.cdf(sign * d2) / 100

    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho}


def implied_volatility(
    option_type: str, market_price: float, S: float, K: float,
    T: float, r: float, q: float = 0.0,
    max_iter: int = 100, tol: float = 1e-6,
) -> float | None:
    """牛顿法求解隐含波动率。"""
    if T <= 0 or market_price <= 0:
        return None

    sigma = 0.3  # 初始猜测
    for _ in range(max_iter):
        price = black_scholes_price(option_type, S, K, T, r, sigma, q)
        diff = price - market_price

        vega_val = compute_greeks(option_type, S, K, T, r, sigma, q)["vega"] * 100
        if abs(vega_val) < 1e-12:
            return None

        sigma -= diff / vega_val
        if sigma <= 0:
            sigma = 0.01

        if abs(diff) < tol:
            return sigma

    return sigma
