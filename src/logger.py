import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")
# 服务器系统时区不一定是东八区(云主机默认经常是 UTC),日志时间固定按北京时间显示,
# 不依赖服务器自己的系统时区配置
_TZ = ZoneInfo("Asia/Shanghai")


class _ShanghaiFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')},{int(record.msecs):03d}"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = _ShanghaiFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    os.makedirs(_LOG_DIR, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, "trader.log"), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
