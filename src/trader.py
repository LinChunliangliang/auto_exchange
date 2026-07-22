import time
from typing import Optional

from config import Settings
from exchange.base import Exchange, RateLimitedError
from logger import get_logger
from risk import compute_entry_quantity, compute_margin
from signal_client import to_exchange_symbol
from state_store import StateStore

log = get_logger("trader")


def _compute_pnl(position: dict, exit_price: float) -> float:
    qty = position["qty"]
    if position["side"] == "long":
        return (exit_price - position["entry_price"]) * qty
    return (position["entry_price"] - exit_price) * qty


def _implied_exit_price(position: dict, pnl: float) -> float:
    """由真实盈亏反推一个"虚拟成交价",只是为了让记录里 exit_price 和 pnl 两个字段
    互相对得上(方便面板显示),不是真的还原了对方平仓那一刻的确切成交价格。"""
    qty = position["qty"]
    if position["side"] == "long":
        return position["entry_price"] + pnl / qty
    return position["entry_price"] - pnl / qty


def enter_position(exchange: Exchange, sig: dict, settings: Settings, state: StateStore, balance: float) -> None:
    symbol = to_exchange_symbol(sig["symbol"])
    direction = sig["recDir"]
    entry_side = "BUY" if direction == "long" else "SELL"
    close_side = "SELL" if direction == "long" else "BUY"

    # 余额由调用方(main.py)每轮只查一次、传进来给这一轮所有信号共用,
    # 不在这里重复查——一是减少一次 API 调用(账户余额查询也算在限流额度里),
    # 二是跟 can_enter 的熔断判断用的是同一个余额快照,逻辑一致
    #
    # 查询阶段(交易对信息/标记价格)单独隔离异常:交易所限流或网络抖动时,
    # 这次评估失败只跳过这一个信号,不能让异常冒到 main.py 的信号循环里,
    # 连累同一轮里其他候选信号也评估不到
    try:
        filters = exchange.get_symbol_filters(symbol)
        if filters is None:
            log.warning("交易对 %s 不存在或不可交易(可能是股票/大宗商品代币化合约),跳过信号", symbol)
            return

        mark_price = exchange.get_mark_price(symbol)
        if not mark_price:
            log.warning("无法获取 %s 标记价格,跳过", symbol)
            return
    except RateLimitedError as exc:
        log.warning("评估信号 %s 时被交易所限流,跳过本次信号: %s", symbol, exc)
        return
    except Exception:
        log.exception("评估信号 %s 时查询交易所信息出错,跳过本次信号", symbol)
        return

    qty = compute_entry_quantity(mark_price, balance, settings)
    if qty * mark_price < filters.min_notional:
        log.warning(
            "%s 计算出的名义价值 %.2f 低于交易所最小限制 %.2f,跳过",
            symbol,
            qty * mark_price,
            filters.min_notional,
        )
        return

    try:
        exchange.set_leverage(symbol, settings.leverage)
        order = exchange.place_market_order(symbol, entry_side, qty)
    except Exception:
        log.exception("开仓下单失败 %s", symbol)
        return

    entry_price = order.avg_price
    if direction == "long":
        tp_price = entry_price * (1 + settings.take_profit_pct)
        sl_price = entry_price * (1 - settings.stop_loss_pct)
    else:
        tp_price = entry_price * (1 - settings.take_profit_pct)
        sl_price = entry_price * (1 + settings.stop_loss_pct)

    # 止盈止损不挂交易所条件单(STOP_MARKET/TAKE_PROFIT_MARKET 在该账号被拒绝,-4120),
    # 改为记录阈值,由 monitor_positions 每轮自己比对标记价格触发市价平仓
    state.add_open_position(
        symbol,
        {
            "side": direction,
            "qty": order.executed_qty,
            "entry_price": entry_price,
            "close_side": close_side,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "opened_at": time.time(),
            "signal_score": sig.get("score"),
        },
    )
    log.info(
        "开仓成功 %s %s qty=%s entry=%.6f tp=%.6f sl=%.6f margin=%.2fUSDT(余额%.2f的%.1f%%) (score=%s)",
        symbol,
        direction,
        order.executed_qty,
        entry_price,
        tp_price,
        sl_price,
        compute_margin(balance, settings),
        balance,
        settings.position_size_pct * 100,
        sig.get("score"),
    )


def _record_trade_and_cleanup(
    state: StateStore, symbol: str, pos: dict, reason: str, exit_price: float, pnl_override: Optional[float] = None
) -> float:
    pnl = pnl_override if pnl_override is not None else _compute_pnl(pos, exit_price)
    state.add_daily_pnl(pnl)
    state.set_cooldown(symbol)
    state.remove_open_position(symbol)
    state.record_closed_trade(
        {
            "symbol": symbol,
            "side": pos["side"],
            "reason": reason,
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "qty": pos["qty"],
            "pnl": pnl,
            "opened_at": pos["opened_at"],
            "closed_at": time.time(),
            "signal_score": pos.get("signal_score"),
        }
    )
    log.info("平仓 %s 原因=%s exit=%.6f pnl=%.4f USDT", symbol, reason, exit_price, pnl)
    return pnl


def _close_and_record(exchange: Exchange, state: StateStore, symbol: str, pos: dict, reason: str) -> None:
    try:
        order = exchange.close_position_market(symbol, pos["close_side"], pos["qty"])
    except Exception:
        log.exception("平仓失败,请手动检查 %s 持仓!", symbol)
        return
    _record_trade_and_cleanup(state, symbol, pos, reason, order.avg_price)


def _monitor_one_position(exchange: Exchange, state: StateStore, symbol: str, pos: dict) -> None:
    amt = exchange.get_position_amt(symbol)
    if amt == 0:
        # 没有挂交易所条件单,仓位不该自己消失;出现这种情况基本是手动干预或爆仓。
        # 这一笔不是我们下单平的,优先查交易所自己算的真实已实现盈亏(准确,包含手续费);
        # 查不到(比如交易所不支持/查询失败)才退回到用标记价格估算,好过完全不记录
        realized_pnl = exchange.get_realized_pnl(symbol, pos["opened_at"])
        if realized_pnl is not None:
            exit_price = _implied_exit_price(pos, realized_pnl)
            log.warning("%s 在交易所侧已无持仓(非机器人平仓),已查到真实已实现盈亏,记录", symbol)
            _record_trade_and_cleanup(state, symbol, pos, "外部平仓", exit_price, pnl_override=realized_pnl)
        else:
            exit_price = exchange.get_mark_price(symbol)
            if exit_price is None:
                exit_price = pos["entry_price"]
            log.warning("%s 在交易所侧已无持仓(非机器人平仓),查不到真实盈亏,按标记价估算并记录", symbol)
            _record_trade_and_cleanup(state, symbol, pos, "外部平仓(估算)", exit_price)
        return

    mark_price = exchange.get_mark_price(symbol)
    if mark_price is None:
        return

    if pos["side"] == "long":
        hit_tp = mark_price >= pos["tp_price"]
        hit_sl = mark_price <= pos["sl_price"]
    else:
        hit_tp = mark_price <= pos["tp_price"]
        hit_sl = mark_price >= pos["sl_price"]

    if hit_tp:
        _close_and_record(exchange, state, symbol, pos, "止盈")
    elif hit_sl:
        _close_and_record(exchange, state, symbol, pos, "止损")
    # 没有超时强平:"舔一口就跑"指的是拿到正确收益就走,不是拿够时间就走,
    # 没到止盈/止损前就一直持有,哪怕这笔仓位拿得比预期久


def monitor_positions(exchange: Exchange, settings: Settings, state: StateStore) -> None:
    for symbol, pos in state.get_open_positions().items():
        # 每个持仓单独隔离异常:交易所限流/某个币种查询报错时,不能连累同一轮里
        # 其他持仓完全没被检查——那些仓位的止盈止损防护不能因为别的币种出问题而失效
        try:
            _monitor_one_position(exchange, state, symbol, pos)
        except RateLimitedError as exc:
            log.warning("盯仓 %s 时被交易所限流,跳过本轮,下一轮再试: %s", symbol, exc)
        except Exception:
            log.exception("盯仓 %s 时出错,跳过本轮,不影响其他持仓", symbol)
