import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    ybradar_api_url: str
    ybradar_session_cookie: str
    signal_poll_interval_seconds: int
    position_monitor_interval_seconds: int
    max_signal_age_seconds: int
    trade_exchange: str

    dry_run: bool

    binance_api_key: str
    binance_api_secret: str
    binance_testnet: bool

    position_size_pct: float
    dry_run_balance_usdt: float
    leverage: int
    take_profit_pct: float
    stop_loss_pct: float
    profit_lock_after_seconds: int
    max_concurrent_positions: int
    symbol_cooldown_seconds: int
    max_daily_loss_pct: float

    dashboard_port: int
    dashboard_username: str
    dashboard_password: str


def load_settings() -> Settings:
    settings = Settings(
        ybradar_api_url=os.getenv("YBRADAR_API_URL", "https://ybradar.qianyuwing.com/api/signals"),
        ybradar_session_cookie=os.getenv("YBRADAR_SESSION_COOKIE", ""),
        signal_poll_interval_seconds=_int("SIGNAL_POLL_INTERVAL_SECONDS", 180),
        position_monitor_interval_seconds=_int("POSITION_MONITOR_INTERVAL_SECONDS", 5),
        max_signal_age_seconds=_int("MAX_SIGNAL_AGE_SECONDS", 300),
        trade_exchange=os.getenv("TRADE_EXCHANGE", "binance").strip().lower(),
        dry_run=_bool("DRY_RUN", True),
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        binance_testnet=_bool("BINANCE_TESTNET", True),
        position_size_pct=_float("POSITION_SIZE_PCT", 0.05),
        dry_run_balance_usdt=_float("DRY_RUN_BALANCE_USDT", 5000.0),
        leverage=_int("LEVERAGE", 2),
        take_profit_pct=_float("TAKE_PROFIT_PCT", 0.015),
        stop_loss_pct=_float("STOP_LOSS_PCT", 0.01),
        profit_lock_after_seconds=_int("PROFIT_LOCK_AFTER_SECONDS", 600),
        max_concurrent_positions=_int("MAX_CONCURRENT_POSITIONS", 3),
        symbol_cooldown_seconds=_int("SYMBOL_COOLDOWN_SECONDS", 1800),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 0.15),
        dashboard_port=_int("DASHBOARD_PORT", 8080),
        dashboard_username=os.getenv("DASHBOARD_USERNAME", ""),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
    )

    if not settings.ybradar_session_cookie:
        raise RuntimeError("YBRADAR_SESSION_COOKIE 未配置,请在 .env 中设置")

    if not settings.dry_run and settings.trade_exchange == "binance":
        if not settings.binance_api_key or not settings.binance_api_secret:
            raise RuntimeError("非 DRY_RUN 模式下必须配置 BINANCE_API_KEY / BINANCE_API_SECRET")

    if settings.position_size_pct <= 0 or settings.position_size_pct > 1:
        raise RuntimeError("POSITION_SIZE_PCT 必须是 0~1 之间的小数(比如 0.05 = 5%),当前值明显不对")

    if settings.max_daily_loss_pct <= 0 or settings.max_daily_loss_pct > 1:
        raise RuntimeError("MAX_DAILY_LOSS_PCT 必须是 0~1 之间的小数(比如 0.15 = 15%),当前值明显不对")

    return settings
