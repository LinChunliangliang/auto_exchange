"""对比机器人实际拿到的收益 vs 信号出现后价格实际能摸到的最大有利幅度(MFE),
用来判断当前止盈线是不是设得偏保守——统计口径故意跟 YBRadar 战绩页(15m/1h/4h 峰值)对齐,
方便直接比较。只读 data/state.json 里的 closed_trades 和币安公开K线,不会下任何真实订单。

用法: PYTHONPATH=src python3 scripts/analyze_trades.py
"""
import time

from config import load_settings
from exchange.binance_futures import BinanceFutures
from state_store import StateStore

WINDOWS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}


def fetch_mfe(exchange: BinanceFutures, symbol: str, side: str, entry_time: float, entry_price: float) -> dict:
    """拉entry_time之后4小时内的1m K线,算出15m/1h/4h各窗口内朝仓位方向的最大有利幅度(百分比)。"""
    start_ms = int(entry_time * 1000)
    end_ms = int((entry_time + WINDOWS["4h"]) * 1000)
    try:
        klines = exchange._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        )
    except Exception as exc:
        return {"error": str(exc)}

    result = {}
    for label, seconds in WINDOWS.items():
        cutoff_ms = start_ms + seconds * 1000
        window_klines = [k for k in klines if k[0] <= cutoff_ms]
        if not window_klines:
            result[label] = None
            continue
        if side.upper() == "LONG":
            best = max(float(k[2]) for k in window_klines)  # high
            result[label] = (best - entry_price) / entry_price * 100
        else:
            best = min(float(k[3]) for k in window_klines)  # low
            result[label] = (entry_price - best) / entry_price * 100
    return result


def fmt_pct(v):
    return f"{v:>7.2f}%" if v is not None else f"{'N/A':>8}"


def main():
    settings = load_settings()
    if settings.dry_run:
        print("!! 当前 .env 是 DRY_RUN=true,这个脚本要对着有真实成交记录的账户跑,"
              "请确认是不是在服务器上、对着真实 .env 执行的\n")

    exchange = BinanceFutures(settings.binance_api_key, settings.binance_api_secret, testnet=settings.binance_testnet)
    state = StateStore()
    trades = state.get_recent_trades(limit=200)
    if not trades:
        print("data/state.json 里没有任何 closed_trades 记录")
        return

    print(f"共 {len(trades)} 笔已平仓记录\n")
    header = f"{'symbol':<12}{'side':<6}{'reason':<16}{'实际拿到':>9}{'峰值15m':>9}{'峰值1h':>9}{'峰值4h':>9}{'pnl(U)':>9}"
    print(header)
    print("-" * len(header))

    total_pnl = 0.0
    wins = 0
    rows = []
    for t in trades:
        symbol, side = t["symbol"], t["side"]
        entry, exit_p = t["entry_price"], t["exit_price"]
        pnl = t["pnl"]
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        captured_pct = (exit_p - entry) / entry * 100 * (1 if side.upper() == "LONG" else -1)

        mfe = fetch_mfe(exchange, symbol, side, t["opened_at"], entry)
        if "error" in mfe:
            print(f"{symbol:<12}{side:<6}{t['reason']:<16}{captured_pct:>8.2f}%  MFE查询失败: {mfe['error']}")
            continue

        rows.append((captured_pct, mfe))
        print(
            f"{symbol:<12}{side:<6}{t['reason']:<16}{captured_pct:>8.2f}% "
            f"{fmt_pct(mfe.get('15m'))} {fmt_pct(mfe.get('1h'))} {fmt_pct(mfe.get('4h'))} {pnl:>9.4f}"
        )
        time.sleep(0.15)  # 公开K线接口权重不高,但笔数多时稍微留点间隔,别一次性打太猛

    print(f"\n共 {len(trades)} 笔, 胜率 {wins / len(trades) * 100:.1f}%, 累计已实现盈亏 {total_pnl:.4f} USDT")

    valid = [(c, m) for c, m in rows if m.get("15m") is not None]
    if valid:
        avg_captured = sum(c for c, _ in valid) / len(valid)
        avg_mfe_15m = sum(m["15m"] for _, m in valid) / len(valid)
        avg_mfe_1h = sum(m["1h"] for _, m in valid if m.get("1h") is not None) / len(valid)
        avg_mfe_4h = sum(m["4h"] for _, m in valid if m.get("4h") is not None) / len(valid)
        print(
            f"平均实际拿到 {avg_captured:.2f}% | 平均峰值 15m={avg_mfe_15m:.2f}% "
            f"1h={avg_mfe_1h:.2f}% 4h={avg_mfe_4h:.2f}%"
        )
        never_hit_tp = sum(1 for c, m in valid if m["15m"] < abs(settings.take_profit_pct) * 100)
        print(
            f"15分钟内峰值都没到过当前止盈线({settings.take_profit_pct * 100:.1f}%)的笔数: "
            f"{never_hit_tp}/{len(valid)} ({never_hit_tp / len(valid) * 100:.1f}%)"
        )


if __name__ == "__main__":
    main()
