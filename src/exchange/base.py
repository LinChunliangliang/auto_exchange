from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    order_id: str
    avg_price: float
    executed_qty: float


@dataclass
class SymbolFilters:
    qty_step: float
    qty_precision: int
    price_tick: float
    price_precision: int
    min_notional: float


class RateLimitedError(RuntimeError):
    """交易所限流/封禁期间的短路异常。定义在这个抽象层而不是具体的
    binance_futures.py 里,这样 trader.py 只需要认识 exchange.base,不用管
    具体是哪个交易所实现抛出来的。"""


class Exchange(ABC):
    """交易所执行接口的抽象定义,交易主循环只依赖这一层,
    换交易所(比如以后接 OKX)只需要新实现一个子类。

    止盈止损不依赖交易所条件单(STOP_MARKET/TAKE_PROFIT_MARKET):实测这两种委托类型
    在币安合约账号被整体拒绝(-4120 Order type not supported ... Algo Order API),
    因此止盈/止损/超时强平统一由 trader.py 自己盯盘(对比 mark price)后调用
    close_position_market 发市价单完成,这里不需要挂单/撤单/查条件单状态的接口。"""

    @abstractmethod
    def get_symbol_filters(self, symbol: str) -> Optional[SymbolFilters]:
        """返回该交易对的精度/最小名义价值信息;交易对不存在则返回 None。"""

    @abstractmethod
    def get_mark_price(self, symbol: str) -> Optional[float]:
        """返回标记价格;查询失败或交易对不存在返回 None。"""

    @abstractmethod
    def get_account_balance(self, asset: str = "USDT") -> float:
        """返回账户可用余额,用于按余额百分比计算每笔仓位保证金。查询失败返回 0.0。"""

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> None:
        ...

    @abstractmethod
    def place_market_order(self, symbol: str, side: str, quantity: float) -> OrderResult:
        """side: 'BUY' | 'SELL'"""

    @abstractmethod
    def get_position_amt(self, symbol: str) -> float:
        """返回当前持仓数量(正数=多,负数=空,0=无仓位)。"""

    @abstractmethod
    def close_position_market(self, symbol: str, side: str, quantity: float) -> OrderResult:
        """side 是平仓方向(平多用 SELL,平空用 BUY)。"""

    def get_rate_limit_remaining_seconds(self) -> float:
        """交易所限流熔断的剩余秒数,0 表示当前没有被限流。给面板展示用,
        默认实现返回 0,只有真实会被限流的交易所(比如 BinanceFutures)需要覆盖。"""
        return 0.0
