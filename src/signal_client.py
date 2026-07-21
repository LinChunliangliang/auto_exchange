import time
from typing import List, TypedDict

import requests

from logger import get_logger

log = get_logger("signal_client")


class Signal(TypedDict):
    exchange: str
    symbol: str
    price: float
    score: float
    signalKey: str
    strongState: str
    strongSince: int
    recDir: str


def fetch_signals(api_url: str, session_cookie: str, timeout: float = 10.0) -> List[Signal]:
    """拉取 YBRadar 实时雷达信号列表,失败时返回空列表并记录日志(不抛异常中断主循环)。"""
    headers = {
        "accept": "*/*",
        "referer": "https://ybradar.qianyuwing.com/",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
    }
    cookies = {"ybr_session": session_cookie}

    try:
        resp = requests.get(api_url, headers=headers, cookies=cookies, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.warning("拉取 YBRadar 信号失败: %s", exc)
        return []
    except ValueError as exc:
        log.warning("YBRadar 返回内容不是合法 JSON: %s", exc)
        return []

    if resp.status_code == 200 and isinstance(data, dict) and "signals" not in data:
        log.warning("YBRadar 返回结构异常(可能 session 已过期,需要重新登录复制新 cookie): %s", str(data)[:200])
        return []

    return data.get("signals", [])


def get_actionable_signals(
    signals: List[Signal], only_exchange: str, max_signal_age_seconds: int
) -> List[Signal]:
    """筛选'强信号 + 当前处于强信号窗口 + 有明确方向 + 刚进入强信号窗口不久'的条目,
    并只保留可执行交易所的品种。

    不检查新鲜度的话,一个已经 active 了很久的信号(比如机器人刚重启时第一次看到它)
    也会被当成"新信号"进场——这其实是追一个可能已经走完的行情,而不是抓早期异动。
    """
    now = time.time()
    actionable = []
    for sig in signals:
        if sig.get("exchange") != only_exchange:
            continue
        if sig.get("signalKey") != "hot":
            continue
        if sig.get("strongState") != "active":
            continue
        if sig.get("recDir") not in ("long", "short"):
            continue

        strong_since = sig.get("strongSince") or 0
        age = now - strong_since
        if strong_since <= 0 or age < 0 or age > max_signal_age_seconds:
            continue

        actionable.append(sig)
    return actionable


def signal_key(sig: Signal) -> str:
    """同一个'强信号窗口'的唯一标识,用于去重(避免同一波强信号被反复进场)。"""
    return f"{sig['exchange']}:{sig['symbol']}:{sig.get('strongSince', 0)}"


def to_exchange_symbol(ybradar_symbol: str) -> str:
    """YBRadar 的裸币种名 -> 交易所合约符号。风控检查(持仓/冷却)和实际下单
    必须用同一个转换结果做 key,否则会出现同一币种被重复开仓的漏洞。"""
    return f"{ybradar_symbol}USDT"
