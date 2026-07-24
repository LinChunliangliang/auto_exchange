import json
import os
import tempfile
import threading
from typing import List

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "controls.json"
)

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
            return data
        return {"trading_enabled": True, "symbol_blacklist": []}

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
