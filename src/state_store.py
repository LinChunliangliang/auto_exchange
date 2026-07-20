import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Optional

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "state.json"
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class StateStore:
    """JSON 文件持久化状态:已处理信号(去重)、币种冷却、当前持仓、当日盈亏。
    进程重启后能恢复,避免重复开仓或漏管已开的仓位。
    """

    def __init__(self, path: str = _DEFAULT_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "seen_signals": {},
            "symbol_cooldowns": {},
            "open_positions": {},
            "daily_pnl": {},
        }

    def _save_locked(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        dir_ = os.path.dirname(self._path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    # ---- 信号去重 ----
    def already_seen(self, key: str) -> bool:
        with self._lock:
            return key in self._data["seen_signals"]

    def mark_seen(self, key: str) -> None:
        with self._lock:
            self._data["seen_signals"][key] = time.time()
            self._save_locked()

    # ---- 币种冷却 ----
    def is_in_cooldown(self, symbol: str, cooldown_seconds: int) -> bool:
        with self._lock:
            last = self._data["symbol_cooldowns"].get(symbol)
            if last is None:
                return False
            return (time.time() - last) < cooldown_seconds

    def set_cooldown(self, symbol: str) -> None:
        with self._lock:
            self._data["symbol_cooldowns"][symbol] = time.time()
            self._save_locked()

    # ---- 持仓 ----
    def open_position_count(self) -> int:
        with self._lock:
            return len(self._data["open_positions"])

    def has_open_position(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._data["open_positions"]

    def get_open_positions(self) -> dict:
        with self._lock:
            return dict(self._data["open_positions"])

    def add_open_position(self, symbol: str, position: dict) -> None:
        with self._lock:
            self._data["open_positions"][symbol] = position
            self._save_locked()

    def remove_open_position(self, symbol: str) -> Optional[dict]:
        with self._lock:
            pos = self._data["open_positions"].pop(symbol, None)
            self._save_locked()
            return pos

    # ---- 当日盈亏(风控熔断用) ----
    def add_daily_pnl(self, pnl: float) -> None:
        with self._lock:
            day = _today()
            self._data["daily_pnl"][day] = self._data["daily_pnl"].get(day, 0.0) + pnl
            self._save_locked()

    def get_today_pnl(self) -> float:
        with self._lock:
            return self._data["daily_pnl"].get(_today(), 0.0)
