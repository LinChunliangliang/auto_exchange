import os
import time

from config import Settings, load_settings
from exchange.base import Exchange
from exchange.binance_futures import BinanceFutures
from exchange.dry_run import DryRunExchange
from logger import get_logger
from risk import can_enter
from signal_client import fetch_signals, get_actionable_signals, signal_key
from state_store import StateStore
from status_file import write_status
from trader import enter_position, monitor_positions

log = get_logger("main")


def build_exchange(settings: Settings) -> Exchange:
    if settings.dry_run:
        log.info("==== DRY_RUN 模式:纯模拟,不会下任何真实订单 ====")
        return DryRunExchange(
            testnet=settings.binance_testnet,
            balance_usdt=settings.dry_run_balance_usdt,
            allow_tradifi_perpetuals=settings.allow_tradifi_perpetuals,
        )

    if settings.trade_exchange == "binance":
        mode = "测试网" if settings.binance_testnet else "实盘(真实资金!)"
        log.info("==== 真实下单模式:币安合约 %s ====", mode)
        return BinanceFutures(
            settings.binance_api_key,
            settings.binance_api_secret,
            settings.binance_testnet,
            allow_tradifi_perpetuals=settings.allow_tradifi_perpetuals,
        )

    raise RuntimeError(f"暂不支持的交易所: {settings.trade_exchange}")


def _mode_label(settings: Settings) -> str:
    if settings.dry_run:
        return "dry_run"
    return "testnet" if settings.binance_testnet else "live"


def run() -> None:
    settings = load_settings()
    exchange = build_exchange(settings)
    state = StateStore()
    mode = _mode_label(settings)
    started_at = time.time()

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
        "参数: signal_poll=%ss position_monitor=%ss max_signal_age=%ss size=余额x%.1f%% x%d leverage "
        "tp=%.2f%% sl=%.2f%% profit_lock_after=%ss(min_pct=%.2f%%) max_concurrent=%d cooldown=%ss "
        "daily_loss_limit=余额x%.1f%%",
        settings.signal_poll_interval_seconds,
        settings.position_monitor_interval_seconds,
        settings.max_signal_age_seconds,
        settings.position_size_pct * 100,
        settings.leverage,
        settings.take_profit_pct * 100,
        settings.stop_loss_pct * 100,
        settings.profit_lock_after_seconds,
        settings.profit_lock_min_pct * 100,
        settings.max_concurrent_positions,
        settings.symbol_cooldown_seconds,
        settings.max_daily_loss_pct * 100,
    )

    # 两个节奏分开:盯仓(止盈止损)要快,因为这些都是行情变化很快的币种,
    # 拉信号要慢,因为 YBRadar 自己也就 3 分钟更新一次,拉太快只是徒增对方压力。
    # 用一个短 tick(position_monitor_interval_seconds)跑主循环,盯仓每个 tick 都做,
    # 拉信号只在累计够 signal_poll_interval_seconds 的时候才做一次。
    last_signal_fetch_at = 0.0

    while True:
        try:
            monitor_positions(exchange, settings, state)

            now = time.time()
            if now - last_signal_fetch_at >= settings.signal_poll_interval_seconds:
                last_signal_fetch_at = now

                raw_signals = fetch_signals(settings.ybradar_api_url, settings.ybradar_session_cookie)
                actionable = get_actionable_signals(
                    raw_signals, settings.trade_exchange, settings.max_signal_age_seconds
                )
                hot_count = sum(1 for s in raw_signals if s.get("signalKey") == "hot")
                log.info(
                    "本轮心跳: 总信号=%d 强信号(hot)=%d 可执行(active+方向明确)=%d 持仓中=%d",
                    len(raw_signals),
                    hot_count,
                    len(actionable),
                    state.open_position_count(),
                )

                if actionable:
                    # 余额这一轮只查一次,给这一轮所有候选信号共用(风控熔断判断和
                    # 仓位计算要用同一个快照,顺便少打一次 API,节省限流额度)。
                    # 代价是同一轮里如果连续进了好几笔仓,后面几笔用的余额没扣掉
                    # 前面几笔刚占用的保证金,仓位会略微偏大——这一轮撑死也就
                    # MAX_CONCURRENT_POSITIONS 笔,误差有限,不值得为此多打 API
                    try:
                        balance = exchange.get_account_balance()
                    except Exception:
                        log.exception("查询账户余额失败,这一轮跳过所有候选信号")
                        balance = 0.0

                    if balance <= 0:
                        log.warning("账户余额查询失败或为 0,本轮不评估任何信号")
                    else:
                        for sig in actionable:
                            key = signal_key(sig)
                            if state.already_seen(key):
                                continue
                            # 无论最终是否进场,这个强信号窗口都只评估一次,避免它在
                            # strongState=active 期间被每一轮循环重复触发/刷日志
                            state.mark_seen(key)

                            ok, reason = can_enter(sig, state, settings, balance)
                            if not ok:
                                log.info(
                                    "跳过信号 %s:%s score=%s -> %s",
                                    sig["exchange"],
                                    sig["symbol"],
                                    sig.get("score"),
                                    reason,
                                )
                                continue

                            enter_position(exchange, sig, settings, state, balance)

        except Exception:
            log.exception("主循环出现异常,记录后继续下一轮")

        # 不管这一轮有没有异常都写心跳,面板靠这个判断"进程是不是还活着",
        # 而不是靠 state.json(那个只在持仓/信号变化时才更新,长时间没有信号时不会动)
        write_status(
            last_tick_at=time.time(),
            started_at=started_at,
            mode=mode,
            pid=os.getpid(),
            rate_limit_remaining_seconds=exchange.get_rate_limit_remaining_seconds(),
        )

        time.sleep(settings.position_monitor_interval_seconds)


if __name__ == "__main__":
    run()
