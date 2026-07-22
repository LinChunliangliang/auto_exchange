from typing import Tuple

from config import Settings
from signal_client import to_exchange_symbol
from state_store import StateStore


def can_enter(sig: dict, state: StateStore, settings: Settings, balance: float) -> Tuple[bool, str]:
    # 必须用和下单时同一套符号(交易所符号)做状态 key,否则"已有持仓/冷却中"的检查会失效
    symbol = to_exchange_symbol(sig["symbol"])

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


def compute_margin(balance: float, settings: Settings) -> float:
    return balance * settings.position_size_pct


def compute_entry_quantity(mark_price: float, balance: float, settings: Settings) -> float:
    notional = compute_margin(balance, settings) * settings.leverage
    return notional / mark_price
