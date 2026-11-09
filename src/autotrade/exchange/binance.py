"""Binance 適配器：ccxt.binanceusdm（USDT-M 永續）。

TP/SL 用獨立 reduce-only 單（STOP_MARKET / 限價 TP），隨單附掛不支持。
移動止損 = 撤舊 SL 觸發單 + 按新價重掛。
"""
from __future__ import annotations

import asyncio
import logging

import ccxt

from ..proxy import resolve_proxy
from .base import ExchangeAdapter, NewOrder

log = logging.getLogger("autotrade.exchange.binance")


class BinanceAdapter(ExchangeAdapter):
    def __init__(self, api_key: str, secret: str, proxy: dict | None = None) -> None:
        if not api_key or not secret:
            raise ValueError("Binance apiKey/secret 未設定")
        ccxt_cfg: dict = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
        }
        resolved = resolve_proxy(proxy, default_type="http")
        if resolved:
            ptype, addr, port = resolved
            url = f"{ptype}://{addr}:{port}"
            ccxt_cfg["proxies"] = {"http": url, "https": url}
        self._ex = ccxt.binanceusdm(ccxt_cfg)
        # 看護對賬需要拉「全部」掛單（不指定 symbol）；binanceusdm 預設會把這個
        # 限速提醒當作異常拋出，顯式確認後放行。
        self._ex.options.setdefault("fetchOpenOrders", {})["warnWithoutSymbol"] = False
        self._markets_loaded = False

    @property
    def capabilities(self) -> dict:
        # TP/SL 走獨立 reduce-only 單（OrderManager 非 attached 分支）
        return {"supports_attached_sl": False}

    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await asyncio.to_thread(self._ex.load_markets)
            self._markets_loaded = True

    @staticmethod
    def to_ccxt_symbol(symbol: str) -> str:
        if "/" in symbol:
            return symbol
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        return f"{base}/USDT:USDT"

    @staticmethod
    def from_ccxt_symbol(symbol: str) -> str:
        if "/" not in symbol:
            return symbol
        base, _, settle = symbol.partition("/")
        return f"{base}{settle.split(':')[0]}"

    # ---- 只讀 ----

    async def fetch_equity(self) -> float:
        await self._ensure_markets()
        bal = await asyncio.to_thread(self._ex.fetch_balance)
        usdt = bal.get("USDT") or {}
        return float(usdt.get("total") or 0)

    async def fetch_market(self, symbol: str) -> dict:
        await self._ensure_markets()
        s = self.to_ccxt_symbol(symbol)
        if s not in self._ex.markets:
            raise ValueError(f"Binance 無此交易對（或已下架）：{s}")
        m = self._ex.markets[s]
        return {
            "symbol": s,
            "contractSize": m.get("contractSize", 1.0),
            "precision": {
                "amount": m.get("precision", {}).get("amount"),
                "price": m.get("precision", {}).get("price"),
            },
            "limits": m.get("limits", {}),
            "active": m.get("active", True),
        }

    async def fetch_ticker(self, symbol: str) -> float:
        await self._ensure_markets()
        s = self.to_ccxt_symbol(symbol)
        t = await asyncio.to_thread(self._ex.fetch_ticker, s)
        return float(t.get("last") or 0)

    # ---- 交易 ----

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._ensure_markets()
        s = self.to_ccxt_symbol(symbol)
        # 全倉模式；逐倉需改 marginMode 為 ISOLATED（實測確認預設行為後調整）
        await asyncio.to_thread(
            self._ex.set_leverage, leverage, s, {"marginMode": "CROSS"}
        )

    async def create_order(self, o: NewOrder) -> str:
        await self._ensure_markets()
        s = self.to_ccxt_symbol(o.symbol)
        params: dict = {}
        if o.reduce_only:
            params["reduceOnly"] = True
        res = await asyncio.to_thread(
            self._ex.create_order, s, o.order_type, o.side, o.qty, o.price, params
        )
        return str(res.get("id") or res.get("orderId") or "")

    async def attach_sl_tp(self, order_id: str, symbol: str, sl, tps) -> None:
        """掛獨立 SL：STOP_MARKET reduce-only（數量=目前持倉）。"""
        if sl is None:
            return
        await self._ensure_markets()
        pos = await self.fetch_position(symbol)
        if not pos or pos.get("qty", 0) <= 0:
            raise RuntimeError(f"交易所查無 {symbol} 持倉，無法掛止損")
        close_side = "sell" if pos["side"] == "long" else "buy"
        await asyncio.to_thread(
            self._ex.create_order, self.to_ccxt_symbol(symbol),
            "market", close_side, pos["qty"], None,
            {"stopPrice": sl, "reduceOnly": True},
        )
        log.info("已掛獨立 SL：%s @%s（qty=%s）", symbol, sl, pos["qty"])

    async def amend_order(self, order_id: str, symbol: str, **kwargs) -> None:
        """移動止損 = 撤舊 SL 觸發單 + 按新價重掛（Binance 無改 SL 端點）。"""
        sl_price = kwargs.get("sl_price")
        if sl_price is None:
            raise ValueError("amend_order 需要 sl_price")
        await self._ensure_markets()
        s = self.to_ccxt_symbol(symbol)
        pos = await self.fetch_position(symbol)
        if not pos or pos.get("qty", 0) <= 0:
            raise RuntimeError(f"交易所查無 {symbol} 持倉，無法移動止損")
        # 撤掉舊的 SL 觸發單。條件單的 type 不保證叫 "market"，改用
        # 「減倉單 + 帶觸發價」識別；條件單還必須用 stop=True 才撤得掉。
        open_orders = await self.fetch_open_orders(symbol)
        for o in open_orders:
            if o.get("reduce_only") and o.get("stop_price"):
                await self.cancel_order(o["id"], symbol,
                                        conditional=o.get("conditional", False))
        close_side = "sell" if pos["side"] == "long" else "buy"
        await asyncio.to_thread(
            self._ex.create_order, s, "market", close_side, pos["qty"], None,
            {"stopPrice": sl_price, "reduceOnly": True},
        )
        log.info("已移動止損：%s SL=%s", symbol, sl_price)

    async def cancel_order(self, order_id: str, symbol: str,
                           *, conditional: bool = False) -> None:
        await self._ensure_markets()
        params = {"stop": True} if conditional else {}
        await asyncio.to_thread(
            self._ex.cancel_order, order_id, self.to_ccxt_symbol(symbol), params
        )

    async def cancel_all_for_symbol(self, symbol: str) -> None:
        """終態清理：一般掛單與條件掛單分屬兩個端點，兩邊都要撤。"""
        await self._ensure_markets()
        s = self.to_ccxt_symbol(symbol)
        await asyncio.to_thread(self._ex.cancel_all_orders, s)
        try:
            await asyncio.to_thread(self._ex.cancel_all_orders, s, {"stop": True})
        except Exception as exc:
            log.error("撤條件掛單失敗：%s %s；請手動確認是否仍有殘留止損單",
                      symbol, exc)
            raise

    async def fetch_position(self, symbol: str) -> dict | None:
        await self._ensure_markets()
        s = self.to_ccxt_symbol(symbol)
        positions = await asyncio.to_thread(self._ex.fetch_positions, [s])
        for p in positions:
            qty = float(p.get("contracts") or 0)
            if qty != 0:
                return {
                    "symbol": self.from_ccxt_symbol(s),
                    "side": p.get("side"),
                    "qty": qty,
                    "entry_price": p.get("entryPrice"),
                    "sl_price": None,
                }
        return None

    async def fetch_all_positions(self) -> list[dict]:
        await self._ensure_markets()
        positions = await asyncio.to_thread(self._ex.fetch_positions, None)
        out: list[dict] = []
        for p in positions:
            qty = float(p.get("contracts") or 0)
            if qty == 0:
                continue
            out.append({
                "symbol": self.from_ccxt_symbol(p.get("symbol") or ""),
                "side": p.get("side"),
                "qty": qty,
                "entry_price": p.get("entryPrice"),
                "sl_price": None,
            })
        return out

    def _normalize_open_order(self, o: dict, conditional: bool) -> dict:
        """統一掛單欄位；條件單的減倉標記與觸發價可能只存在於原始 info 裡。

        ccxt 對 algo/條件單的 parse 結果不保證有 reduceOnly、stopPrice，
        只信頂層欄位會把「該清的保護單」誤判成普通單而漏撤。
        """
        info = o.get("info") or {}
        reduce_only = o.get("reduceOnly")
        if reduce_only is None:
            reduce_only = info.get("reduceOnly")
        if reduce_only is None:
            reduce_only = info.get("closePosition")
        if isinstance(reduce_only, str):
            reduce_only = reduce_only.strip().lower() == "true"
        stop_price = (
            o.get("stopPrice") or o.get("triggerPrice")
            or info.get("stopPrice") or info.get("triggerPrice")
        )
        try:
            stop_price = float(stop_price) if stop_price is not None else None
        except (TypeError, ValueError):
            stop_price = None
        qty = o.get("amount")
        if qty is None:
            qty = info.get("quantity")
        return {
            "id": str(o.get("id") or info.get("algoId") or info.get("orderId") or ""),
            "symbol": self.from_ccxt_symbol(o.get("symbol") or ""),
            "side": o.get("side"),
            "type": o.get("type"),
            "qty": qty,
            "price": o.get("price"),
            "stop_price": stop_price,
            "reduce_only": bool(reduce_only),
            "conditional": conditional,
        }

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict]:
        """查未完成掛單：一般掛單與條件掛單（獨立 SL/TP 觸發單）分屬兩個端點。

        帶 stopPrice 建立的止損單會被 ccxt 送到 /fapi/v1/algoOrder，只查
        /fapi/v1/openOrders 看不到它，全平收尾會誤判「無掛單」而留下殘單。
        以 params.stop=True 切換到 /fapi/v1/openAlgoOrders 補齊。
        """
        await self._ensure_markets()
        s = self.to_ccxt_symbol(symbol) if symbol else None
        regular = await asyncio.to_thread(self._ex.fetch_open_orders, s)
        try:
            conditional = await asyncio.to_thread(
                self._ex.fetch_open_orders, s, None, None, {"stop": True}
            )
        except Exception as exc:
            # 查不到不能當作「沒有」：明確告警，終態收尾仍會發撤單指令兜底
            log.error("查詢條件掛單失敗：%s %s；本次只看得見一般掛單，"
                      "請手動確認是否仍有殘留止損單", symbol or "全部", exc)
            conditional = []
        out: list[dict] = []
        seen: set[tuple[str, bool]] = set()
        for orders, is_conditional in ((regular, False), (conditional, True)):
            for o in orders or []:
                item = self._normalize_open_order(o, is_conditional)
                key = (item["id"], is_conditional)
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
        return out
