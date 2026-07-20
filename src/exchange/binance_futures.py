import hashlib
import hmac
import time
from decimal import ROUND_DOWN, Decimal
from typing import Dict, Optional
from urllib.parse import urlencode

import requests

from exchange.base import Exchange, OrderResult, SymbolFilters
from logger import get_logger

log = get_logger("binance_futures")


class BinanceFutures(Exchange):
    """币安 USDⓈ-M 合约 REST 客户端(testnet.binancefuture.com / fapi.binance.com)。"""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": api_key})
        self._filters_cache: Dict[str, SymbolFilters] = {}

    # ---- low level ----
    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = urlencode(params, doseq=True)
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False):
        params = dict(params or {})
        url = self._base_url + path
        if signed:
            params = self._sign(params)
        resp = self._session.request(method, url, params=params, timeout=10)
        if resp.status_code >= 400:
            log.error("Binance API 错误 %s %s -> %s %s", method, path, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    # ---- public data ----
    def get_symbol_filters(self, symbol: str) -> Optional[SymbolFilters]:
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        data = self._request("GET", "/fapi/v1/exchangeInfo")
        for s in data.get("symbols", []):
            if s["symbol"] != symbol:
                continue
            qty_step = 1.0
            price_tick = 0.01
            min_notional = 5.0
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    qty_step = float(f["stepSize"])
                elif f["filterType"] == "PRICE_FILTER":
                    price_tick = float(f["tickSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    min_notional = float(f.get("notional", f.get("minNotional", 5.0)))
            filters = SymbolFilters(
                qty_step=qty_step,
                qty_precision=int(s.get("quantityPrecision", 0)),
                price_tick=price_tick,
                price_precision=int(s.get("pricePrecision", 2)),
                min_notional=min_notional,
            )
            self._filters_cache[symbol] = filters
            return filters
        return None

    def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            data = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        except requests.RequestException:
            return None
        price = data.get("markPrice")
        return float(price) if price is not None else None

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    # ---- rounding helpers ----
    def _round_qty(self, symbol: str, qty: float) -> float:
        filters = self.get_symbol_filters(symbol)
        if not filters or filters.qty_step <= 0:
            return qty
        step = Decimal(str(filters.qty_step))
        value = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(value)

    def _round_price(self, symbol: str, price: float) -> float:
        filters = self.get_symbol_filters(symbol)
        if not filters or filters.price_tick <= 0:
            return price
        tick = Decimal(str(filters.price_tick))
        value = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return float(value)

    # ---- trading ----
    def place_market_order(self, symbol: str, side: str, quantity: float) -> OrderResult:
        qty = self._round_qty(symbol, quantity)
        resp = self._request(
            "POST",
            "/fapi/v1/order",
            {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty},
            signed=True,
        )
        avg_price = float(resp.get("avgPrice") or 0) or self.get_mark_price(symbol) or 0.0
        # 市价单刚提交时交易所常常还没回填 executedQty(返回字符串 "0"),
        # 必须先转成 float 再判断是否需要用委托数量兜底,否则非空字符串 "0" 恒为真,fallback 永远不会触发
        executed_qty = float(resp.get("executedQty") or 0) or qty
        return OrderResult(order_id=str(resp["orderId"]), avg_price=avg_price, executed_qty=executed_qty)

    def get_position_amt(self, symbol: str) -> float:
        data = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for pos in data:
            if pos["symbol"] == symbol:
                return float(pos["positionAmt"])
        return 0.0

    def close_position_market(self, symbol: str, side: str, quantity: float) -> OrderResult:
        qty = self._round_qty(symbol, abs(quantity))
        resp = self._request(
            "POST",
            "/fapi/v1/order",
            {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty, "reduceOnly": "true"},
            signed=True,
        )
        avg_price = float(resp.get("avgPrice") or 0) or self.get_mark_price(symbol) or 0.0
        executed_qty = float(resp.get("executedQty") or 0) or qty
        return OrderResult(order_id=str(resp["orderId"]), avg_price=avg_price, executed_qty=executed_qty)
