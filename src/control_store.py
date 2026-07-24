import dataclasses
import json
import os
import tempfile
import threading
from typing import List

from config import Settings, validate_settings

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "controls.json"
)

# 面板允许覆盖的策略/风控参数——只挑"跟交易策略行为有关"的字段开放,故意排除:
# API密钥/会话Cookie(凭证,绝不能经面板改)、DRY_RUN/BINANCE_TESTNET/TRADE_EXCHANGE/
# ALLOW_TRADIFI_PERPETUALS(这几个决定用哪个 Exchange 实例,只在 main.py 启动时
# build_exchange() 那一刻生效一次,改了不重启进程根本不会有任何效果,面板上放出来
# 只会造成"改了却没生效"的误解)、DASHBOARD_* (面板自己的账号密码/端口,不应该
# 由已登录的面板会话自己改,端口修改还牵扯服务器防火墙规则)。
# 剩下这些字段全部是 can_enter/enter_position/monitor_positions 每次调用时才读取
# 使用的,天然支持"面板写、主程序下一次检查就读到最新值"这种不需要重启的热更新。
OVERRIDABLE_FIELDS = {
    "position_size_pct": float,
    "leverage": int,
    "take_profit_pct": float,
    "stop_loss_pct": float,
    "atr_stop_loss_enabled": bool,
    "atr_period": int,
    "atr_interval": str,
    "atr_multiplier": float,
    "atr_min_stop_pct": float,
    "profit_lock_after_seconds": int,
    "profit_lock_min_pct": float,
    "ladder_take_profit_enabled": bool,
    "ladder_first_close_pct": float,
    "ladder_step_close_pct": float,
    "ladder_max_levels": int,
    "ladder_breakeven_buffer_pct": float,
    "max_concurrent_positions": int,
    "symbol_cooldown_seconds": int,
    "max_daily_loss_pct": float,
}


def _coerce(field: str, raw_value: str):
    py_type = OVERRIDABLE_FIELDS[field]
    if py_type is bool:
        return str(raw_value).strip().lower() in ("1", "true", "yes", "on")
    if py_type is str:
        return str(raw_value).strip()
    return py_type(raw_value)

# 面板(独立进程)是这个文件唯一的写入方,交易主程序(main.py)只读、每次检查都
# 重新从磁盘读、从不缓存在内存里——这样面板随时写入的改动,主程序下一次检查就能
# 看到,不需要重启主程序;反过来主程序也从不写这个文件,避免两个进程互相覆盖
# 对方的修改(这正是 state.json 只由 main.py 写、面板只读展示的镜像设计)。
_lock = threading.Lock()


def _normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if symbol and not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol


class ControlStore:
    """面板可写的运行时开关:是否允许开新仓、币种黑名单。只影响新开仓,不影响
    已有持仓的止盈止损/监控——跟当日亏损熔断"只停新仓不动老仓"是同一个原则。
    """

    def __init__(self, path: str = _DEFAULT_PATH):
        self._path = path

    def _load(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("trading_enabled", True)
            data.setdefault("symbol_blacklist", [])
            data.setdefault("settings_overrides", {})
            return data
        return {"trading_enabled": True, "symbol_blacklist": [], "settings_overrides": {}}

    def _save(self, data: dict) -> None:
        dir_ = os.path.dirname(self._path)
        os.makedirs(dir_, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".controls_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def is_trading_enabled(self) -> bool:
        return self._load()["trading_enabled"]

    def set_trading_enabled(self, enabled: bool) -> None:
        with _lock:
            data = self._load()
            data["trading_enabled"] = enabled
            self._save(data)

    def get_blacklist(self) -> List[str]:
        return list(self._load()["symbol_blacklist"])

    def add_to_blacklist(self, symbol: str) -> None:
        symbol = _normalize_symbol(symbol)
        if not symbol:
            return
        with _lock:
            data = self._load()
            if symbol not in data["symbol_blacklist"]:
                data["symbol_blacklist"].append(symbol)
            self._save(data)

    def remove_from_blacklist(self, symbol: str) -> None:
        symbol = _normalize_symbol(symbol)
        with _lock:
            data = self._load()
            if symbol in data["symbol_blacklist"]:
                data["symbol_blacklist"].remove(symbol)
            self._save(data)

    # ---- 策略参数热覆盖 ----
    def get_setting_overrides(self) -> dict:
        return dict(self._load()["settings_overrides"])

    def set_setting_override(self, field: str, raw_value: str, base_settings: Settings) -> None:
        """校验用的是"当前所有覆盖 + 这一次新改的这个字段"合成出来的完整效果,
        而不是只校验单个字段——比如改 ATR_MIN_STOP_PCT 是否合法要看当前生效的
        STOP_LOSS_PCT 是多少(可能也被覆盖过),不能只拿 .env 里的原始值比较。"""
        if field not in OVERRIDABLE_FIELDS:
            raise ValueError(f"{field} 不是允许通过面板修改的参数")
        value = _coerce(field, raw_value)
        with _lock:
            data = self._load()
            trial_overrides = dict(data["settings_overrides"])
            trial_overrides[field] = value
            trial_settings = dataclasses.replace(base_settings, **trial_overrides)
            validate_settings(trial_settings)  # 不合法会抛异常,这一步之后的保存不会执行

            data["settings_overrides"][field] = value
            self._save(data)

    def clear_setting_override(self, field: str) -> None:
        with _lock:
            data = self._load()
            data["settings_overrides"].pop(field, None)
            self._save(data)

    def resolve_effective_settings(self, base_settings: Settings) -> Settings:
        overrides = self.get_setting_overrides()
        if not overrides:
            return base_settings
        return dataclasses.replace(base_settings, **overrides)
