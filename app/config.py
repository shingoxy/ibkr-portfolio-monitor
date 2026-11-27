"""应用配置模块，使用 Pydantic Settings 从 .env 加载配置。"""

from __future__ import annotations

import sys
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # IBKR TWS API 连接
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 17
    ibkr_account: str = ""

    # 代理设置（留空则不使用代理）
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_type: str = "socks5"  # socks5, http

    # 应用服务
    app_host: str = "127.0.0.1"
    app_port: int = 8765
    read_only: bool = True

    # 数据库
    database_url: str = "sqlite+aiosqlite:///./data/ibkr_portfolio.db"

    # 日志
    log_level: str = "INFO"

    # 行情类型 (1=实时, 2=冻结, 3=延时)
    market_data_type: int = 1

    # Trading signal system, Phase 1 only creates simulated order plans.
    trading_enabled: bool = False
    trading_mode: str = "signal_only"  # signal_only, paper
    trading_watchlist: str = "SPY,QQQ,VTI,IWM,DIA,NVDA,TSM,TSLA,AAPL,MSFT"
    trading_max_positions: int = 4
    trading_max_total_exposure_pct: float = 0.60
    trading_max_symbol_exposure_pct: float = 0.20
    trading_risk_per_trade_pct: float = 0.003
    trading_min_cash_reserve_pct: float = 0.25

    # 重连配置
    reconnect_max_retries: int = 20
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 60.0

    # 风险-free 利率（用于 Black-Scholes）
    risk_free_rate: float = 0.05

    @field_validator("app_host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost"):
            print(f"[安全警告] app_host={v} 不安全，仅允许 127.0.0.1 或 localhost")
            sys.exit(1)
        return v

    @field_validator("read_only")
    @classmethod
    def validate_read_only(cls, v: bool) -> bool:
        if not v:
            print("[安全错误] read_only=false 不允许！本程序仅支持只读模式。")
            sys.exit(1)
        return v

    @field_validator("trading_mode")
    @classmethod
    def validate_trading_mode(cls, v: str) -> str:
        allowed = {"signal_only", "paper"}
        if v not in allowed:
            raise ValueError(f"trading_mode must be one of {sorted(allowed)}")
        return v


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()
