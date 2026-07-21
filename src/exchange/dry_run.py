import uuid
from typing import Dict, Optional

from exchange.base import Exchange, OrderResult, SymbolFilters
from exchange.binance_futures import BinanceFutures
from logger import get_logger

log = get_logger("dry_run_exchange")


class DryRunExchange(Exchange):
    """纯模拟交易所:不发送任何真实下单请求,只用币安公开行情(mark price/exchangeInfo,
    这两个接口不需要 API Key)在内存里模拟开平仓。止盈/止损/超时的触发判断统一由
    trader.py 自己盯盘完成(和实盘走同一套逻辑),这里只需要如实模拟开仓和平仓两个动作。"""

    def __init__(self, testnet: bool = True, balance_usdt: float = 5000.0):
        self._public = BinanceFutures(api_key="", api_secret="", testnet=testnet)
        self._positions: Dict[str, dict] = {}
        # 模拟账户没有真实余额,用一个固定的参考余额算仓位百分比(不随模拟盈亏滚动,
        # 只是用来验证"按余额百分比开仓"这条逻辑本身对不对)
        self._balance_usdt = balance_usdt

    def get_symbol_filters(self, symbol: str) -> Optional[SymbolFilters]:
        return self._public.get_symbol_filters(symbol)

    def get_mark_price(self, symbol: str) -> Optional[float]:
        return self._public.get_mark_price(symbol)

    def get_account_balance(self, asset: str = "USDT") -> float:
        return self._balance_usdt

    def set_leverage(self, symbol: str, leverage: int) -> None:
        log.info("[DRY_RUN] 设置杠杆 %s -> %sx(模拟,不发送请求)", symbol, leverage)

    def place_market_order(self, symbol: str, side: str, quantity: float) -> OrderResult:
        price = self.get_mark_price(symbol) or 0.0
        pos_side = "long" if side == "BUY" else "short"
        self._positions[symbol] = {"side": pos_side, "qty": quantity, "entry_price": price}
        order_id = f"dry-{uuid.uuid4().hex[:12]}"
        log.info("[DRY_RUN] 模拟开仓 %s %s qty=%s price=%s", symbol, side, quantity, price)
        return OrderResult(order_id=order_id, avg_price=price, executed_qty=quantity)

    def get_position_amt(self, symbol: str) -> float:
        pos = self._positions.get(symbol)
        if not pos:
            return 0.0
        return pos["qty"] if pos["side"] == "long" else -pos["qty"]

    def close_position_market(self, symbol: str, side: str, quantity: float) -> OrderResult:
        price = self.get_mark_price(symbol) or 0.0
        self._positions.pop(symbol, None)
        order_id = f"dry-{uuid.uuid4().hex[:12]}"
        log.info("[DRY_RUN] 模拟平仓 %s %s qty=%s price=%s", symbol, side, quantity, price)
        return OrderResult(order_id=order_id, avg_price=price, executed_qty=quantity)
