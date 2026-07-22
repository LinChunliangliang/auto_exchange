import json
import os
import tempfile
from typing import Optional

_STATUS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "status.json"
)


def write_status(**fields) -> None:
    """轻量的心跳状态文件,给面板判断"程序是不是还活着"用,跟 state.json(交易状态)
    分开存,避免每 5 秒一次的心跳写入跟持仓/信号这些更重要的数据抢锁/混在一起。"""
    os.makedirs(os.path.dirname(_STATUS_PATH), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(_STATUS_PATH), prefix=".status_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False)
        os.replace(tmp_path, _STATUS_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_status() -> Optional[dict]:
    if not os.path.exists(_STATUS_PATH):
        return None
    with open(_STATUS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
