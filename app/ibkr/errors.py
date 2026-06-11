"""IBKR TWS API 错误码定义与中文翻译。"""

from __future__ import annotations


# ── 错误码分类 ─────────────────────────────────────────────────────────

# 连接错误（致命）
CONNECTION_ERRORS = {
    502: "TWS 未启动或 API Socket 未启用，请检查 TWS 设置",
    503: "TWS 版本过旧，请升级 TWS",
    504: "未连接到 TWS",
    509: "Socket 读取异常",
    520: "创建 Socket 失败",
    530: "SSL 连接失败",
}

# 连接状态变化
CONNECTIVITY_CODES = {
    1100: "IBKR 与 TWS 之间的连接已断开，数据完整性可能受影响",
    1101: "连接已恢复，但数据已丢失，需要重新请求",
    1102: "连接已恢复，数据保持完整",
}

# 信息性消息（非错误）
INFO_CODES = {
    2104: "市场数据服务器连接正常",
    2106: "历史数据服务器连接正常",
    2158: "证券定义数据服务器连接正常",
}

# 数据请求错误
DATA_ERRORS = {
    162: "历史数据请求频率超限（每10分钟最多60次）",
    321: "请求参数验证错误",
    366: "未找到历史数据查询",
    200: "未找到合约",
    354: "未订阅实时行情，延迟行情可用",
    10167: "请求的行情数据类型不可用",
    10168: "延迟行情未启用，请在 IBKR 账户管理中订阅延迟行情数据",
    10189: "账户未订阅此市场数据",
}

# 订单相关（不应出现，但需识别）
ORDER_ERRORS = {
    100: "订单价格超出允许范围",
    101: "订单数量无效",
    102: "订单类型不支持",
    103: "订单无法取消",
    104: "无法修改已成交订单",
    105: "订单被 TWS 拒绝",
    106: "订单被交易所拒绝",
    107: "订单已取消",
    109: "账户不允许此操作",
    110: "订单大小不满足最小要求",
    111: "价格不满足最小变动单位",
    116: "订单被修改",
    117: "无法取消已提交的订单",
    134: "下单被拒绝：只读模式",
    135: "下单被拒绝：无交易权限",
}


def translate_error(error_code: int) -> str:
    """将 IBKR 错误码翻译成中文说明。"""
    for table in [CONNECTION_ERRORS, CONNECTIVITY_CODES, INFO_CODES,
                  DATA_ERRORS, ORDER_ERRORS]:
        if error_code in table:
            return table[error_code]
    return f"未知错误 (code={error_code})"


def is_connectivity_error(error_code: int) -> bool:
    """判断是否为连接相关错误。"""
    return error_code in CONNECTION_ERRORS or error_code in CONNECTIVITY_CODES


def is_info_message(error_code: int) -> bool:
    """判断是否为信息性消息。"""
    return error_code in INFO_CODES or error_code == -1


def requires_resubscribe(error_code: int) -> bool:
    """判断是否需要重新订阅数据。"""
    return error_code in (1101, 502, 504)
