from typing import Tuple

from config import Settings
from control_store import ControlStore
from signal_client import to_exchange_symbol
from state_store import StateStore


def can_enter(sig: dict, state: StateStore, settings: Settings, balance: float) -> Tuple[bool, str]:
    # 必须用和下单时同一套符号(交易所符号)做状态 key,否则"已有持仓/冷却中"的检查会失效
    symbol = to_exchange_symbol(sig["symbol"])

    # 面板上的"暂停开仓"开关、币种黑名单、以及可覆盖的风控参数:每次都重新从磁盘
    # 读取(不缓存),这样面板(独立进程)随时改的设置,主程序下一轮检查就能生效,
    # 不需要重启交易主程序
    control = ControlStore()
    settings = control.resolve_effective_settings(settings)

    if not control.is_trading_enabled():
        return False, "面板已暂停开仓"

    if symbol in control.get_blacklist():
        return False, "该币种已被面板拉黑,不允许开仓"

    if state.open_position_count() >= settings.max_concurrent_positions:
        return False, "并发持仓已达上限"

    if state.has_open_position(symbol):
        return False, "该币种已有持仓"

    if state.is_in_cooldown(symbol, settings.symbol_cooldown_seconds):
        return False, "该币种在冷却期内"

    # 熔断线按余额百分比算,不是写死的美元数——不然账户资金一变(比如从测试网的
    # 几千U换成实盘的一百U),同一个数字要么形同虚设要么过度保守
    daily_loss_limit = balance * settings.max_daily_loss_pct
    if state.get_today_pnl() <= -abs(daily_loss_limit):
        return False, "当日亏损已触发熔断,停止开新仓"

    return True, ""


def compute_target_risk_amount(balance: float, settings: Settings) -> float:
    """每笔交易愿意承担的目标风险金额(止损真的触发时预期亏掉的钱)。用固定的
    STOP_LOSS_PCT 当基准校准这个金额,不是每笔交易的止损上限——ATR 止损关闭、或者
    ATR 算出来的止损空间刚好等于 STOP_LOSS_PCT 时,这里算出来的风险金额和过去
    "固定保证金 × 杠杆" 的算法完全一致,是新公式的锚点。"""
    return balance * settings.position_size_pct * settings.leverage * settings.stop_loss_pct


def compute_entry_quantity(mark_price: float, balance: float, stop_loss_pct: float, settings: Settings) -> float:
    """按目标风险金额反推仓位数量,而不是按固定保证金算——止损空间越宽(比如 ATR
    算出来波动大的品种),仓位就按比例越小;止损空间越窄,仓位就越大。这样不管
    止损空间怎么变,一旦真的止损,亏掉的绝对金额都基本恒定,不会因为某个品种
    波动大就损失更多真金白银。"""
    target_risk = compute_target_risk_amount(balance, settings)
    return target_risk / (mark_price * stop_loss_pct)


def compute_margin_from_qty(qty: float, price: float, settings: Settings) -> float:
    """仓位保证金现在是算出仓位数量之后反推出来的(qty × 价格 ÷ 杠杆),不再是
    独立设定的固定值——止损空间不同,同样的目标风险金额对应的保证金也会不同。"""
    return qty * price / settings.leverage
