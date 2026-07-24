import hmac
import time
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, render_template_string, request

from config import Settings, load_settings
from exchange.base import Exchange
from logger import get_logger
from main import build_exchange
from state_store import StateStore
from status_file import read_status

log = get_logger("dashboard")

_TZ = ZoneInfo("Asia/Shanghai")

app = Flask(__name__)
_settings: Settings = load_settings()
_exchange: Exchange = build_exchange(_settings)
# 面板是独立进程,跟真正跑交易的 main.py 进程分开。StateStore 只在构造时读一次文件,
# 之后全靠内存,如果这里只建一次全局单例,面板就永远看不到交易进程后续写入的新数据——
# 所以每次请求都必须重新构造一个 StateStore,让它从磁盘上重新读最新内容。


def _check_auth(username: str, password: str) -> bool:
    return hmac.compare_digest(username, _settings.dashboard_username) and hmac.compare_digest(
        password, _settings.dashboard_password
    )


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username or "", auth.password or ""):
            return Response(
                "需要登录", 401, {"WWW-Authenticate": 'Basic realm="auto_ex dashboard"'}
            )
        return view(*args, **kwargs)

    return wrapped


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{seconds}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分"


def _mode_label(settings: Settings) -> str:
    if settings.dry_run:
        return "DRY_RUN(纯模拟)"
    return "测试网" if settings.binance_testnet else "实盘(真实资金)"


def _build_view_data() -> dict:
    now = time.time()
    state = StateStore()
    status = read_status() or {}
    last_tick_at = status.get("last_tick_at")
    heartbeat_age = (now - last_tick_at) if last_tick_at else None
    # 心跳超过盯仓间隔的 3 倍还没更新,大概率是主程序挂了或卡住了
    heartbeat_stale = heartbeat_age is None or heartbeat_age > _settings.position_monitor_interval_seconds * 3

    rate_limit_remaining = status.get("rate_limit_remaining_seconds", 0) or 0

    positions = []
    for symbol, pos in state.get_open_positions().items():
        mark_price = None
        unrealized_pnl = None
        try:
            mark_price = _exchange.get_mark_price(symbol)
        except Exception:
            log.exception("查询 %s 标记价格失败(面板展示用,不影响交易主程序)", symbol)
        if mark_price is not None:
            if pos["side"] == "long":
                unrealized_pnl = (mark_price - pos["entry_price"]) * pos["qty"]
            else:
                unrealized_pnl = (pos["entry_price"] - mark_price) * pos["qty"]
        tp_price = pos["tp_price"]
        tp_capped = tp_price in (float("inf"), 0.0)
        positions.append(
            {
                "symbol": symbol,
                "side": pos["side"],
                "entry_price": pos["entry_price"],
                "tp_price": tp_price,
                "tp_capped": tp_capped,
                "sl_price": pos["sl_price"],
                "qty": pos["qty"],
                "notional": pos["qty"] * pos["entry_price"],
                "mark_price": mark_price,
                "unrealized_pnl": unrealized_pnl,
                "held_for": _fmt_duration(now - pos["opened_at"]),
                "signal_score": pos.get("signal_score"),
                "ladder_level": pos.get("ladder_level", 0),
                "stop_loss_pct_used": pos.get("stop_loss_pct_used"),
            }
        )

    trades = []
    for t in state.get_recent_trades(limit=50):
        trades.append(
            {
                **t,
                "closed_at_fmt": _fmt_ts(t["closed_at"]),
                "held_for": _fmt_duration(t["closed_at"] - t["opened_at"]),
            }
        )

    recent = state.get_recent_trades(limit=200)
    wins = [t for t in recent if t["pnl"] > 0]
    losses = [t for t in recent if t["pnl"] <= 0]
    stats = {
        "count": len(recent),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": (len(wins) / len(recent) * 100) if recent else None,
        "avg_win": (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0,
        "net_pnl": sum(t["pnl"] for t in recent),
    }

    cooldowns = []
    now_ts = time.time()
    for symbol, last_closed_at in state.get_cooldowns().items():
        remaining = _settings.symbol_cooldown_seconds - (now_ts - last_closed_at)
        if remaining > 0:
            cooldowns.append({"symbol": symbol, "remaining": _fmt_duration(remaining)})

    today_pnl = state.get_today_pnl()

    try:
        balance = _exchange.get_account_balance()
    except Exception:
        log.exception("查询账户余额失败(面板展示用,不影响交易主程序)")
        balance = 0.0
    daily_loss_limit_usdt = balance * _settings.max_daily_loss_pct if balance > 0 else None

    return {
        "mode": _mode_label(_settings),
        "is_live": not _settings.dry_run and not _settings.binance_testnet,
        "last_tick_fmt": _fmt_ts(last_tick_at) if last_tick_at else "从未记录",
        "heartbeat_age": int(heartbeat_age) if heartbeat_age is not None else None,
        "heartbeat_stale": heartbeat_stale,
        "started_at_fmt": _fmt_ts(status.get("started_at")) if status.get("started_at") else "-",
        "rate_limited": rate_limit_remaining > 0,
        "rate_limit_remaining": int(rate_limit_remaining),
        "positions": positions,
        "open_count": len(positions),
        "trades": trades,
        "stats": stats,
        "cooldowns": cooldowns,
        "balance": balance if balance > 0 else None,
        "today_pnl": today_pnl,
        "daily_loss_limit": daily_loss_limit_usdt,
        "daily_loss_pct": (
            min(100, max(0, -today_pnl / daily_loss_limit_usdt * 100))
            if today_pnl < 0 and daily_loss_limit_usdt
            else 0
        ),
        "circuit_breaker_tripped": bool(daily_loss_limit_usdt) and today_pnl <= -abs(daily_loss_limit_usdt),
        "settings": _settings,
        "generated_at": _fmt_ts(now),
    }


_TEMPLATE = """
<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>auto_ex 监控面板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: #0f1115; color: #e6e6e6;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px; line-height: 1.5;
  }
  h1 { font-size: 18px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 24px 0 8px; color: #9fb3c8; }
  .sub { color: #7a8699; font-size: 12px; }
  .card {
    background: #171a21; border: 1px solid #262b36; border-radius: 8px;
    padding: 12px 14px; margin-bottom: 12px;
  }
  .row { display: flex; flex-wrap: wrap; gap: 10px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 12px; font-weight: 600;
  }
  .badge.live { background: #4a1414; color: #ff8080; }
  .badge.testnet { background: #143a1e; color: #6fd88a; }
  .badge.dryrun { background: #1b2a42; color: #7fb2ff; }
  .badge.ok { background: #143a1e; color: #6fd88a; }
  .badge.warn { background: #4a3a14; color: #ffcf70; }
  .badge.bad { background: #4a1414; color: #ff8080; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #262b36; white-space: nowrap; }
  th { color: #7a8699; font-weight: 600; }
  .pnl-pos { color: #6fd88a; }
  .pnl-neg { color: #ff8080; }
  .empty { color: #5a6472; padding: 8px 0; }
  .bar-bg { background: #262b36; border-radius: 4px; height: 8px; overflow: hidden; margin-top: 4px; }
  .bar-fill { height: 100%; background: #ffcf70; }
  .bar-fill.bad { background: #ff8080; }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
  .stat-label { color: #7a8699; font-size: 12px; }
  .stat-value { font-size: 18px; font-weight: 600; }
  .table-wrap { overflow-x: auto; }
  footer { color: #5a6472; font-size: 11px; margin-top: 24px; text-align: center; }
</style>
</head>
<body>

<h1>auto_ex 监控面板</h1>
<div class="sub">数据生成于 {{ d.generated_at }}(每次刷新页面重新拉取一次)</div>

<div class="card row" style="align-items:center;">
  <span class="badge {{ 'live' if d.is_live else ('testnet' if 'DRY_RUN' not in d.mode else 'dryrun') }}">{{ d.mode }}</span>
  {% if d.heartbeat_stale %}
    <span class="badge bad">⚠ 心跳异常,最后一次 {{ d.last_tick_fmt }}(可能已停止运行)</span>
  {% else %}
    <span class="badge ok">运行中 · 最后心跳 {{ d.heartbeat_age }} 秒前</span>
  {% endif %}
  {% if d.rate_limited %}
    <span class="badge bad">⚠ 交易所限流中,预计 {{ d.rate_limit_remaining }} 秒后恢复</span>
  {% endif %}
  {% if d.circuit_breaker_tripped %}
    <span class="badge bad">⚠ 当日亏损熔断已触发,已停止开新仓</span>
  {% endif %}
  <span class="sub">启动于 {{ d.started_at_fmt }}</span>
</div>

<h2>当前持仓({{ d.open_count }} / {{ d.settings.max_concurrent_positions }})</h2>
<div class="card table-wrap">
  {% if d.positions %}
  <table>
    <tr><th>币种</th><th>方向</th><th>开仓价</th><th>标记价</th><th>止盈</th><th>止损</th><th>阶梯档位</th><th>名义价值</th><th>持仓时长</th><th>浮动盈亏</th></tr>
    {% for p in d.positions %}
    <tr>
      <td>{{ p.symbol }}</td>
      <td>{{ '多' if p.side == 'long' else '空' }}</td>
      <td>{{ '%.6f'|format(p.entry_price) }}</td>
      <td>{{ '%.6f'|format(p.mark_price) if p.mark_price is not none else '查询失败' }}</td>
      <td>{{ '已封顶' if p.tp_capped else '%.6f'|format(p.tp_price) }}</td>
      <td>{{ '%.6f'|format(p.sl_price) }}{% if p.stop_loss_pct_used is not none %} <span class="sub">({{ '%.2f%%'|format(p.stop_loss_pct_used * 100) }})</span>{% endif %}</td>
      <td>{{ '第%d档'|format(p.ladder_level) if p.ladder_level else '-' }}</td>
      <td>{{ '%.2f'|format(p.notional) }} USDT</td>
      <td>{{ p.held_for }}</td>
      <td class="{{ 'pnl-pos' if (p.unrealized_pnl or 0) >= 0 else 'pnl-neg' }}">
        {{ '%.2f'|format(p.unrealized_pnl) if p.unrealized_pnl is not none else '-' }}
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="empty">当前没有持仓</div>
  {% endif %}
</div>

<h2>风控状态</h2>
<div class="card">
  <div class="grid2">
    <div>
      <div class="stat-label">当日盈亏</div>
      <div class="stat-value {{ 'pnl-pos' if d.today_pnl >= 0 else 'pnl-neg' }}">{{ '%.2f'|format(d.today_pnl) }} USDT</div>
    </div>
    <div>
      <div class="stat-label">日亏损熔断线(余额 × {{ '%.1f%%'|format(d.settings.max_daily_loss_pct * 100) }})</div>
      <div class="stat-value">{{ '%.2f USDT'|format(d.daily_loss_limit) if d.daily_loss_limit is not none else '余额查询失败' }}</div>
    </div>
    <div>
      <div class="stat-label">并发持仓</div>
      <div class="stat-value">{{ d.open_count }} / {{ d.settings.max_concurrent_positions }}</div>
    </div>
  </div>
  {% if d.today_pnl < 0 %}
  <div class="bar-bg"><div class="bar-fill {{ 'bad' if d.daily_loss_pct > 70 else '' }}" style="width: {{ d.daily_loss_pct }}%;"></div></div>
  {% endif %}

  <div style="margin-top:12px;">
    <div class="stat-label">冷却中的币种(平仓后暂不重复进场)</div>
    {% if d.cooldowns %}
      <div class="row" style="margin-top:6px;">
        {% for c in d.cooldowns %}
        <span class="badge warn">{{ c.symbol }} 还剩 {{ c.remaining }}</span>
        {% endfor %}
      </div>
    {% else %}
      <div class="empty">没有币种在冷却中</div>
    {% endif %}
  </div>
</div>

<h2>绩效统计(最近 {{ d.stats.count }} 笔已平仓交易)</h2>
<div class="card">
  <div class="grid2">
    <div>
      <div class="stat-label">胜率</div>
      <div class="stat-value">{{ '%.1f%%'|format(d.stats.win_rate) if d.stats.win_rate is not none else '-' }}</div>
    </div>
    <div>
      <div class="stat-label">止盈 / 止损笔数</div>
      <div class="stat-value">{{ d.stats.win_count }} / {{ d.stats.loss_count }}</div>
    </div>
    <div>
      <div class="stat-label">平均每笔止盈</div>
      <div class="stat-value pnl-pos">+{{ '%.2f'|format(d.stats.avg_win) }}</div>
    </div>
    <div>
      <div class="stat-label">平均每笔止损</div>
      <div class="stat-value pnl-neg">{{ '%.2f'|format(d.stats.avg_loss) }}</div>
    </div>
    <div>
      <div class="stat-label">净盈亏合计</div>
      <div class="stat-value {{ 'pnl-pos' if d.stats.net_pnl >= 0 else 'pnl-neg' }}">{{ '%.2f'|format(d.stats.net_pnl) }} USDT</div>
    </div>
  </div>
</div>

<h2>最近成交记录</h2>
<div class="card table-wrap">
  {% if d.trades %}
  <table>
    <tr><th>平仓时间</th><th>币种</th><th>方向</th><th>原因</th><th>开仓价</th><th>平仓价</th><th>持仓时长</th><th>盈亏</th></tr>
    {% for t in d.trades %}
    <tr>
      <td>{{ t.closed_at_fmt }}</td>
      <td>{{ t.symbol }}</td>
      <td>{{ '多' if t.side == 'long' else '空' }}</td>
      <td>{{ t.reason }}</td>
      <td>{{ '%.6f'|format(t.entry_price) }}</td>
      <td>{{ '%.6f'|format(t.exit_price) }}</td>
      <td>{{ t.held_for }}</td>
      <td class="{{ 'pnl-pos' if t.pnl >= 0 else 'pnl-neg' }}">{{ '%.2f'|format(t.pnl) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="empty">还没有成交记录</div>
  {% endif %}
</div>

<h2>当前生效参数</h2>
<div class="card table-wrap">
  <table>
    <tr><th>信号轮询间隔</th><td>{{ d.settings.signal_poll_interval_seconds }} 秒</td></tr>
    <tr><th>盯仓间隔</th><td>{{ d.settings.position_monitor_interval_seconds }} 秒</td></tr>
    <tr><th>信号新鲜度上限</th><td>{{ d.settings.max_signal_age_seconds }} 秒</td></tr>
    <tr><th>杠杆</th><td>{{ d.settings.leverage }}x</td></tr>
    <tr><th>每笔保证金</th><td>账户余额 × {{ '%.1f%%'|format(d.settings.position_size_pct * 100) }}</td></tr>
    <tr><th>止盈 / 止损</th><td>{{ '%.2f%%'|format(d.settings.take_profit_pct * 100) }} / {{ '%.2f%%'|format(d.settings.stop_loss_pct * 100) }}</td></tr>
    <tr><th>ATR 动态止损</th><td>
      {% if d.settings.atr_stop_loss_enabled %}
        开启:ATR({{ d.settings.atr_period }},{{ d.settings.atr_interval }}) × {{ d.settings.atr_multiplier }},夹在 [{{ '%.2f%%'|format(d.settings.atr_min_stop_pct * 100) }}, {{ '%.2f%%'|format(d.settings.stop_loss_pct * 100) }}]
      {% else %}
        关闭(统一用固定的 {{ '%.2f%%'|format(d.settings.stop_loss_pct * 100) }})
      {% endif %}
    </td></tr>
    <tr><th>超时锁盈阈值</th><td>{{ d.settings.profit_lock_after_seconds }} 秒,浮盈超过 {{ '%.2f%%'|format(d.settings.profit_lock_min_pct * 100) }} 才触发</td></tr>
    <tr><th>阶梯止盈</th><td>
      {% if d.settings.ladder_take_profit_enabled %}
        开启:第1档{{ '%.0f%%'|format(d.settings.ladder_first_close_pct * 100) }},之后每档{{ '%.0f%%'|format(d.settings.ladder_step_close_pct * 100) }},最多{{ d.settings.ladder_max_levels }}档,保本缓冲{{ '%.2f%%'|format(d.settings.ladder_breakeven_buffer_pct * 100) }}
      {% else %}
        关闭(碰到止盈线一次性全平)
      {% endif %}
    </td></tr>
    <tr><th>最大并发持仓</th><td>{{ d.settings.max_concurrent_positions }}</td></tr>
    <tr><th>币种冷却时间</th><td>{{ d.settings.symbol_cooldown_seconds }} 秒</td></tr>
    <tr><th>日亏损熔断线</th><td>账户余额 × {{ '%.1f%%'|format(d.settings.max_daily_loss_pct * 100) }}</td></tr>
    <tr><th>账户可用余额</th><td>{{ '%.2f USDT'|format(d.balance) if d.balance is not none else '查询失败' }}</td></tr>
  </table>
</div>

<footer>auto_ex dashboard · 只读展示,不提供任何操作入口</footer>

</body>
</html>
"""


@app.route("/")
@require_auth
def index():
    return render_template_string(_TEMPLATE, d=_build_view_data())


def run() -> None:
    if not _settings.dashboard_username or not _settings.dashboard_password:
        raise RuntimeError("DASHBOARD_USERNAME / DASHBOARD_PASSWORD 未配置,面板拒绝启动(不允许无认证暴露)")

    from waitress import serve

    log.info("面板启动: http://0.0.0.0:%d (Basic Auth 用户名=%s)", _settings.dashboard_port, _settings.dashboard_username)
    serve(app, host="0.0.0.0", port=_settings.dashboard_port)


if __name__ == "__main__":
    run()
