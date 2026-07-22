import time

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


def enter_position(exchange: Exchange, sig: dict, settings: Settings, state: StateStore) -> None:
    symbol = to_exchange_symbol(sig["symbol"])
    direction = sig["recDir"]
    entry_side = "BUY" if direction == "long" else "SELL"
    close_side = "SELL" if direction == "long" else "BUY"

    # 查询阶段(交易对信息/标记价格/余额)单独隔离异常:交易所限流或网络抖动时,
    # 这次评估失败只跳过这一个信号,不能让异常冒到 main.py 的信号循环里,
    # 连累同一轮里其他候选信号也评估不到
    try:
        filters = exchange.get_symbol_filters(symbol)
        if filters is None:
            log.warning("交易对 %s 不存在于交易所,跳过信号", symbol)
            return

        mark_price = exchange.get_mark_price(symbol)
        if not mark_price:
            log.warning("无法获取 %s 标记价格,跳过", symbol)
            return

        balance = exchange.get_account_balance()
        if balance <= 0:
            log.warning("查询账户余额失败或余额为 0,跳过 %s", symbol)
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


def _close_and_record(exchange: Exchange, state: StateStore, symbol: str, pos: dict, reason: str) -> None:
    try:
        order = exchange.close_position_market(symbol, pos["close_side"], pos["qty"])
    except Exception:
        log.exception("平仓失败,请手动检查 %s 持仓!", symbol)
        return
    pnl = _compute_pnl(pos, order.avg_price)
    state.add_daily_pnl(pnl)
    state.set_cooldown(symbol)
    state.remove_open_position(symbol)
    state.record_closed_trade(
        {
            "symbol": symbol,
            "side": pos["side"],
            "reason": reason,
            "entry_price": pos["entry_price"],
            "exit_price": order.avg_price,
            "qty": pos["qty"],
            "pnl": pnl,
            "opened_at": pos["opened_at"],
            "closed_at": time.time(),
            "signal_score": pos.get("signal_score"),
        }
    )
    log.info("平仓 %s 原因=%s exit=%.6f pnl=%.4f USDT", symbol, reason, order.avg_price, pnl)


def _monitor_one_position(exchange: Exchange, state: StateStore, symbol: str, pos: dict) -> None:
    amt = exchange.get_position_amt(symbol)
    if amt == 0:
        # 没有挂交易所条件单,仓位不该自己消失;出现这种情况基本是手动干预或爆仓
        log.warning("%s 在交易所侧已无持仓(非机器人平仓,可能是手动操作或爆仓),清理本地记录", symbol)
        state.set_cooldown(symbol)
        state.remove_open_position(symbol)
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
