import time

from config import Settings, load_settings
from exchange.base import Exchange
from exchange.binance_futures import BinanceFutures
from exchange.dry_run import DryRunExchange
from logger import get_logger
from risk import can_enter
from signal_client import fetch_signals, get_actionable_signals, signal_key
from state_store import StateStore
from trader import enter_position, monitor_positions

log = get_logger("main")


def build_exchange(settings: Settings) -> Exchange:
    if settings.dry_run:
        log.info("==== DRY_RUN 模式:纯模拟,不会下任何真实订单 ====")
        return DryRunExchange(testnet=settings.binance_testnet)

    if settings.trade_exchange == "binance":
        mode = "测试网" if settings.binance_testnet else "实盘(真实资金!)"
        log.info("==== 真实下单模式:币安合约 %s ====", mode)
        return BinanceFutures(settings.binance_api_key, settings.binance_api_secret, settings.binance_testnet)

    raise RuntimeError(f"暂不支持的交易所: {settings.trade_exchange}")


def run() -> None:
    settings = load_settings()
    exchange = build_exchange(settings)
    state = StateStore()

    if settings.dry_run:
        # DRY_RUN 的"交易所"状态只存在于本次进程内存中,进程重启后不会记得任何模拟持仓,
        # 但 state.json 是跨进程持久化的,如果不清理,重启后会把上一次的模拟持仓误判为
        # "外部平仓、原因不明"。真实交易所模式不会走这一步,因为真实持仓必须跨重启保留。
        stale = list(state.get_open_positions().keys())
        for symbol in stale:
            state.remove_open_position(symbol)
        if stale:
            log.warning("DRY_RUN 模式清理上次遗留的模拟持仓记录(非真实资金,无影响): %s", stale)

    log.info(
        "参数: poll=%ss size=%.1fUSDT x%d leverage tp=%.2f%% sl=%.2f%% max_hold=%ss max_concurrent=%d "
        "cooldown=%ss daily_loss_limit=%.1fUSDT",
        settings.poll_interval_seconds,
        settings.position_size_usdt,
        settings.leverage,
        settings.take_profit_pct * 100,
        settings.stop_loss_pct * 100,
        settings.max_hold_seconds,
        settings.max_concurrent_positions,
        settings.symbol_cooldown_seconds,
        settings.max_daily_loss_usdt,
    )

    while True:
        try:
            monitor_positions(exchange, settings, state)

            raw_signals = fetch_signals(settings.ybradar_api_url, settings.ybradar_session_cookie)
            actionable = get_actionable_signals(raw_signals, settings.trade_exchange)
            hot_count = sum(1 for s in raw_signals if s.get("signalKey") == "hot")
            log.info(
                "本轮心跳: 总信号=%d 强信号(hot)=%d 可执行(active+方向明确)=%d 持仓中=%d",
                len(raw_signals),
                hot_count,
                len(actionable),
                state.open_position_count(),
            )

            for sig in actionable:
                key = signal_key(sig)
                if state.already_seen(key):
                    continue
                # 无论最终是否进场,这个强信号窗口都只评估一次,避免它在 strongState=active
                # 期间被每一轮循环重复触发/刷日志
                state.mark_seen(key)

                ok, reason = can_enter(sig, state, settings)
                if not ok:
                    log.info("跳过信号 %s:%s score=%s -> %s", sig["exchange"], sig["symbol"], sig.get("score"), reason)
                    continue

                enter_position(exchange, sig, settings, state)

        except Exception:
            log.exception("主循环出现异常,记录后继续下一轮")

        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    run()
