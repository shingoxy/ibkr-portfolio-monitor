"""日志配置与安全守卫模块。"""

from __future__ import annotations

import logging
import re
import sys


# 禁止实现的交易方法名
FORBIDDEN_METHODS = frozenset({
    "placeOrder",
    "cancelOrder",
    "reqGlobalCancel",
    "exerciseOptions",
})


def mask_account(account: str) -> str:
    """将账户号脱敏显示。

    示例: U12345678 -> U12****78
    """
    if not account:
        return "****"
    m = re.match(r"^([UD][U]?)(\d+)$", account)
    if not m:
        if len(account) < 6:
            return "*" * len(account)
        return account[:2] + "*" * (len(account) - 4) + account[-2:]
    prefix, digits = m.group(1), m.group(2)
    if len(digits) < 4:
        return prefix + "*" * len(digits)
    return prefix + digits[:2] + "*" * (len(digits) - 4) + digits[-2:]


def check_read_only() -> None:
    """启动时校验只读模式，非只读则拒绝启动。"""
    from app.config import get_settings
    settings = get_settings()
    if not settings.read_only:
        print("[致命错误] read_only 必须为 true，本程序不支持交易功能。")
        sys.exit(1)
    print("[安全模式] 只读模式已启用，所有交易功能已禁用。")


def validate_no_forbidden_methods(cls: type) -> None:
    """检查类中是否包含禁止的交易方法。

    注意：ibapi.EClient 基类本身包含这些方法定义，
    我们只检查类自身定义的方法（不检查继承的方法）。
    静态扫描测试（test_read_only_guards.py）通过 AST 确保业务代码不调用这些方法。
    """
    for method_name in FORBIDDEN_METHODS:
        if method_name in cls.__dict__:
            print(f"[安全错误] {cls.__name__} 中发现禁止的交易方法: {method_name}")
            sys.exit(1)


class AccountMaskFilter(logging.Filter):
    """日志过滤器，自动对日志中的账户号进行脱敏。"""

    ACCOUNT_PATTERN = re.compile(r"\b([UD][U]?)(\d{2,})\b")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._mask(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._mask(str(v)) if isinstance(v, str) else v
                               for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._mask(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True

    def _mask(self, text: str) -> str:
        def replacer(m: re.Match) -> str:
            prefix = m.group(1)
            digits = m.group(2)
            if len(digits) < 4:
                return prefix + "*" * len(digits)
            return prefix + digits[:2] + "*" * (len(digits) - 4) + digits[-2:]
        return self.ACCOUNT_PATTERN.sub(replacer, text)


def setup_logging(level: str = "INFO") -> None:
    """配置全局日志，附加账户脱敏过滤器。"""
    log_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.addFilter(AccountMaskFilter())

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
