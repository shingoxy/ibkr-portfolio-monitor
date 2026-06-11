"""IBKR 合约构建工具。"""

from __future__ import annotations

from ibapi.contract import Contract


def make_stock_contract(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    primary_exchange: str = "",
) -> Contract:
    """构建股票/ETF 合约。"""
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = exchange
    contract.currency = currency
    if primary_exchange:
        contract.primaryExchange = primary_exchange
    return contract


def make_option_contract(
    symbol: str,
    expiry: str,
    strike: float,
    right: str,
    exchange: str = "SMART",
    currency: str = "USD",
    multiplier: str = "100",
    trading_class: str = "",
    con_id: int = 0,
) -> Contract:
    """构建期权合约。

    Args:
        symbol: 标的代码
        expiry: 到期日 YYYYMMDD
        strike: 行权价
        right: C 或 P
        multiplier: 合约乘数
        trading_class: 交易类别
        con_id: IBKR 合约 ID（已有持仓时使用）
    """
    contract = Contract()
    if con_id:
        contract.conId = con_id
    contract.symbol = symbol
    contract.secType = "OPT"
    contract.exchange = exchange
    contract.currency = currency
    contract.lastTradeDateOrContractMonth = expiry
    contract.strike = strike
    contract.right = right
    contract.multiplier = multiplier
    if trading_class:
        contract.tradingClass = trading_class
    return contract


def make_bag_contract(symbol: str, currency: str = "USD") -> Contract:
    """构建组合（BAG）合约。"""
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "BAG"
    contract.exchange = "SMART"
    contract.currency = currency
    return contract
