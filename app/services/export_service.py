"""导出服务：生成脱敏的 Markdown 和 JSON。"""

from __future__ import annotations

import json
from datetime import datetime

from app.ibkr.connection_manager import ConnectionManager
from app.logging_config import mask_account
from app.models.schemas import ExportResult


class ExportService:
    """导出脱敏数据。"""

    def __init__(self, manager: ConnectionManager) -> None:
        self._manager = manager

    def export_json(self, local: bool = False) -> ExportResult:
        """导出 JSON。local=True 时包含完整本地数据。"""
        snapshot = self._manager.cache.get_snapshot()
        account = snapshot["accounts"][0] if snapshot["accounts"] else "****"
        account_masked = mask_account(account)

        acct_map = {item["tag"]: item["value"] for item in snapshot["account_summary"]}

        data = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "data_freshness": snapshot.get("data_freshness", "unknown"),
            "connection_mode": snapshot.get("connection_mode", "Unknown"),
            "account": {
                "account_masked": account_masked,
                "base_currency": "USD",
                "account_type": acct_map.get("AccountType", ""),
                "net_liquidation": self._f(acct_map.get("NetLiquidation")),
                "total_cash": self._f(acct_map.get("TotalCashValue")),
                "settled_cash": self._f(acct_map.get("SettledCash")),
                "buying_power": self._f(acct_map.get("BuyingPower")),
                "available_funds": self._f(acct_map.get("AvailableFunds")),
                "excess_liquidity": self._f(acct_map.get("ExcessLiquidity")),
                "full_available_funds": self._f(acct_map.get("FullAvailableFunds")),
                "full_excess_liquidity": self._f(acct_map.get("FullExcessLiquidity")),
                "init_margin_req": self._f(acct_map.get("InitMarginReq")),
                "maint_margin_req": self._f(acct_map.get("MaintMarginReq")),
                "unrealized_pnl": self._f(acct_map.get("UnrealizedPnL")),
                "realized_pnl": self._f(acct_map.get("RealizedPnL")),
                "daily_pnl": snapshot.get("pnl", {}).get("daily", 0),
                "cushion": self._f(acct_map.get("Cushion")),
                "day_trades_remaining": acct_map.get("DayTradesRemaining", ""),
                "leverage": self._f(acct_map.get("Leverage")),
            },
            "positions": [
                {
                    "symbol": p.get("symbol", ""),
                    "local_symbol": p.get("local_symbol", ""),
                    "security_type": p.get("sec_type", ""),
                    "currency": p.get("currency", "USD"),
                    "exchange": p.get("exchange", ""),
                    "primary_exchange": p.get("primary_exchange", ""),
                    "con_id": p.get("con_id", 0),
                    "quantity": p.get("position", 0),
                    "multiplier": p.get("multiplier", 100),
                    "average_price_per_share": p.get("avg_cost", 0),
                    "market_price": p.get("market_price", 0),
                    "market_value": p.get("market_value", 0),
                    "unrealized_pnl": p.get("unrealized_pnl", 0),
                    "realized_pnl": p.get("realized_pnl", 0),
                    "daily_pnl": p.get("daily_pnl", 0),
                    "data_status": p.get("data_status", ""),
                    **({"strike": p.get("strike"), "right": p.get("right"),
                        "expiry": p.get("expiry"),
                        "delta": p.get("delta", 0), "gamma": p.get("gamma", 0),
                        "theta": p.get("theta", 0), "vega": p.get("vega", 0),
                        "implied_vol": p.get("implied_vol", 0),
                        "und_price": p.get("und_price", 0),
                        } if p.get("sec_type") == "OPT" else {}),
                }
                for p in snapshot["positions"]
            ],
            "orders": [
                {
                    "symbol": o.get("symbol", ""),
                    "action": o.get("action", ""),
                    "order_type": o.get("order_type", ""),
                    "quantity": o.get("total_qty", 0),
                    "limit_price": o.get("lmt_price", 0),
                    "stop_price": o.get("stop_price", 0),
                    "filled": o.get("filled", 0),
                    "remaining": o.get("remaining", 0),
                    "status": o.get("status", ""),
                    "parent_id": o.get("parent_id", 0),
                    "oca_group": o.get("oca_group", ""),
                }
                for o in snapshot["orders"]
            ],
        }

        if local:
            data["executions"] = snapshot.get("executions", [])
            data["completed_orders"] = snapshot.get("completed_orders", [])
            data["account_raw"] = acct_map

        content = json.dumps(data, ensure_ascii=False, indent=2)
        return ExportResult(
            format="json", content=content,
            record_count=len(snapshot["positions"]) + len(snapshot["orders"]),
            exported_at=datetime.utcnow(),
        )

    def export_markdown(self) -> ExportResult:
        """导出脱敏 Markdown。"""
        snapshot = self._manager.cache.get_snapshot()
        account = snapshot["accounts"][0] if snapshot["accounts"] else "****"
        account_masked = mask_account(account)
        acct_map = {item["tag"]: item["value"] for item in snapshot["account_summary"]}
        positions = snapshot["positions"]
        orders = snapshot["orders"]
        freshness = snapshot.get("data_freshness", "unknown")

        lines = [
            "# IBKR 持仓快照",
            "",
            f"生成时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            f"数据状态：{freshness}",
            f"账户类型：{acct_map.get('AccountType', 'N/A')}",
            f"连接模式：{snapshot.get('connection_mode', 'Unknown')}",
            "基础货币：USD",
            "",
            "## 账户概要",
            "",
            f"- 净资产：{acct_map.get('NetLiquidation', 'N/A')}",
            f"- 现金：{acct_map.get('TotalCashValue', 'N/A')}",
            f"- 可用资金：{acct_map.get('AvailableFunds', 'N/A')}",
            f"- Excess Liquidity：{acct_map.get('ExcessLiquidity', 'N/A')}",
            f"- 初始保证金：{acct_map.get('InitMarginReq', 'N/A')}",
            f"- 维持保证金：{acct_map.get('MaintMarginReq', 'N/A')}",
            f"- 购买力：{acct_map.get('BuyingPower', 'N/A')}",
            "",
        ]

        stock_pos = [p for p in positions if p.get("sec_type") == "STK"]
        opt_pos = [p for p in positions if p.get("sec_type") == "OPT"]

        if stock_pos:
            lines.extend([
                "## 股票和 ETF 持仓",
                "",
                "| 标的 | 数量 | 成本 | 当前价 | 市值 | 未实现盈亏 |",
                "|---|---:|---:|---:|---:|---:|",
            ])
            for p in stock_pos:
                lines.append(
                    f"| {p.get('symbol', '')} | {p.get('position', 0):.0f} | "
                    f"{p.get('avg_cost', 0):.2f} | {p.get('market_price', 0):.2f} | "
                    f"{p.get('market_value', 0):.2f} | {p.get('unrealized_pnl', 0):.2f} |"
                )
            lines.append("")

        if opt_pos:
            lines.extend([
                "## 期权持仓",
                "",
                "| 标的 | 到期日 | 类型 | 执行价 | 数量 | 成本 | 当前价 | 盈亏 | Delta | Theta |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
            ])
            for p in opt_pos:
                lines.append(
                    f"| {p.get('symbol', '')} | {p.get('expiry', '')} | "
                    f"{p.get('right', '')} | {p.get('strike', 0):.1f} | "
                    f"{p.get('position', 0):.0f} | {p.get('avg_cost', 0):.2f} | "
                    f"{p.get('market_price', 0):.2f} | {p.get('unrealized_pnl', 0):.2f} | "
                    f"{p.get('delta', 0):.3f} | {p.get('theta', 0):.2f} |"
                )
            lines.append("")

        if orders:
            lines.extend([
                "## 当前挂单",
                "",
                "| 标的 | 方向 | 类型 | 数量 | 限价 | 状态 |",
                "|---|---|---|---:|---:|---|",
            ])
            for o in orders:
                lines.append(
                    f"| {o.get('symbol', '')} | {o.get('action', '')} | "
                    f"{o.get('order_type', '')} | {o.get('total_qty', 0):.0f} | "
                    f"{o.get('lmt_price', 0):.2f} | {o.get('status', '')} |"
                )
            lines.append("")

        lines.extend([
            "## 分析请求建议",
            "",
            "请根据以上实时持仓，分析：",
            "1. 当前整体方向性风险；",
            "2. 期权组合在不同标的价格下的收益；",
            "3. Theta 和波动率风险；",
            "4. 是否存在裸露空头期权；",
            "5. 可考虑的减仓、止盈和风险控制方案。",
        ])

        content = "\n".join(lines)
        return ExportResult(
            format="markdown", content=content,
            record_count=len(positions) + len(orders),
            exported_at=datetime.utcnow(),
        )

    @staticmethod
    def _f(val: str | None) -> float:
        if not val:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0
