import hashlib
import hmac
import re
import time
from decimal import ROUND_DOWN, Decimal
from typing import Dict, Optional
from urllib.parse import urlencode

import requests

from exchange.base import Exchange, OrderResult, RateLimitedError, SymbolFilters
from logger import get_logger

log = get_logger("binance_futures")

_BANNED_UNTIL_RE = re.compile(r"banned until (\d+)")


class BinanceFutures(Exchange):
    """币安 USDⓈ-M 合约 REST 客户端(testnet.binancefuture.com / fapi.binance.com)。"""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": api_key})
        self._filters_cache: Dict[str, SymbolFilters] = {}
        # 429/418 限流熔断:记录"封禁解除时间",在此之前所有请求本地直接短路拒绝,
        # 不再真的打过去。一次限流是账号/IP 级别的,不区分具体接口,所以这里做成
        # 整个客户端共享的状态,而不是挂在某个方法上。
        self._banned_until: float = 0.0

    def get_rate_limit_remaining_seconds(self) -> float:
        return max(0.0, self._banned_until - time.time())

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
        now = time.time()
        if now < self._banned_until:
            raise RateLimitedError(
                f"仍在币安限流/封禁期内,预计还有 {self._banned_until - now:.0f} 秒解除,本次请求已跳过未发出"
            )

        params = dict(params or {})
        url = self._base_url + path
        if signed:
            params = self._sign(params)
        resp = self._session.request(method, url, params=params, timeout=10)

        if resp.status_code in (418, 429):
            match = _BANNED_UNTIL_RE.search(resp.text)
            if match:
                self._banned_until = int(match.group(1)) / 1000
            else:
                # 429 有时不带具体解除时间,保守退避一段时间,避免继续硬打导致升级成 418
                self._banned_until = now + 60
            log.error(
                "Binance API 限流 %s %s -> %s %s,本地熔断至 %.0f 秒后",
                method,
                path,
                resp.status_code,
                resp.text,
                self._banned_until - now,
            )
        elif resp.status_code >= 400:
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
            # 股票/大宗商品代币化合约(NVDA、TSLA、XAU 这类,contractType=TRADIFI_PERPETUAL)
            # 需要在币安网页/APP 上单独签一份 TradFi-Perps 协议才能交易(-4411),API 层面
            # 绕不过去。这类品种跟"抓加密货币异动"的策略也不是一回事,直接当成不可交易处理
            if s.get("contractType") != "PERPETUAL":
                log.info(
                    "%s 不是普通加密货币永续合约(contractType=%s),跳过",
                    symbol,
                    s.get("contractType"),
                )
                return None
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

    def get_account_balance(self, asset: str = "USDT") -> float:
        try:
            data = self._request("GET", "/fapi/v2/balance", signed=True)
        except requests.RequestException:
            log.exception("查询账户余额失败")
            return 0.0
        for entry in data:
            if entry.get("asset") == asset:
                return float(entry.get("availableBalance") or 0.0)
        return 0.0

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

    def _resolve_fill(self, symbol: str, order_id, fallback_qty: float) -> tuple:
        """市价单刚提交时,POST /fapi/v1/order 的响应经常还没回填真实成交均价/数量
        (avgPrice/executedQty 是异步生成的,常见返回 "0")。这里主动再查一次订单状态
        拿真实成交结果,而不是拿标记价格顶替——标记价格不是真实成交价,尤其这些
        快速波动的币,几秒的价格漂移就足以让盈亏记录跟交易所真实记录对不上,
        严重的话连盈亏的正负号都会算反。查询失败几次才退回标记价格当最后兜底。"""
        for attempt in range(3):
            try:
                order = self._request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)
            except Exception:
                log.exception("查询订单 %s(%s)真实成交结果失败,重试中", symbol, order_id)
                order = {}
            avg_price = float(order.get("avgPrice") or 0)
            executed_qty = float(order.get("executedQty") or 0)
            if avg_price > 0 and executed_qty > 0:
                return avg_price, executed_qty
            if attempt < 2:
                time.sleep(0.3)

        log.warning(
            "订单 %s(%s)查了 3 次还是拿不到真实成交均价,退回标记价格估算(可能跟真实成交价有偏差)",
            symbol,
            order_id,
        )
        return self.get_mark_price(symbol) or 0.0, fallback_qty

    # ---- trading ----
    def place_market_order(self, symbol: str, side: str, quantity: float) -> OrderResult:
        qty = self._round_qty(symbol, quantity)
        resp = self._request(
            "POST",
            "/fapi/v1/order",
            {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty},
            signed=True,
        )
        order_id = resp["orderId"]
        avg_price, executed_qty = self._resolve_fill(symbol, order_id, qty)
        return OrderResult(order_id=str(order_id), avg_price=avg_price, executed_qty=executed_qty)

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
        order_id = resp["orderId"]
        avg_price, executed_qty = self._resolve_fill(symbol, order_id, qty)
        return OrderResult(order_id=str(order_id), avg_price=avg_price, executed_qty=executed_qty)

    def get_realized_pnl(self, symbol: str, since_ts: float) -> Optional[float]:
        try:
            data = self._request(
                "GET",
                "/fapi/v1/income",
                {
                    "symbol": symbol,
                    "incomeType": "REALIZED_PNL",
                    "startTime": int(since_ts * 1000),
                    "limit": 1000,
                },
                signed=True,
            )
        except Exception:
            log.exception("查询 %s 真实已实现盈亏失败,调用方会退回到标记价格估算", symbol)
            return None
        return sum(float(entry["income"]) for entry in data)
