"""訂單管理：進場（市價/限價 + SL/TP 掛載）、改單、撤單、終態清理。"""
from __future__ import annotations

import logging

from ..exchange.base import ExchangeAdapter, NewOrder
from ..risk.precision import exchange_min_qty, plan_tp_tiers, round_down_qty, round_price
from .failure import run_with_retry

log = logging.getLogger("autotrade.execution.order_manager")


class OrderManager:
    """編排進場/平倉/移動止損，依賴 ExchangeAdapter 提供原子操作。

    - 進場：set_leverage → 開倉（隨單附 SL）→ 掛 n 檔 TP reduce-only 限價單
    - 終態：SL/最終 TP 觸發、手工平倉 → 市價全平剩餘 + cancel_all_for_symbol
    - 保護單掛失敗：當場補掛（最多 5 次、間隔 2 秒），不重開倉、不留到開機後
    """

    def __init__(self, adapter: ExchangeAdapter) -> None:
        self._adapter = adapter

    async def enter(self, signal, qty: float,
                    tp_ratios: list[float] | None = None) -> dict:
        """進場。signal 需含 symbol/side/is_limit/entry/sl/leverage/tps。

        tp_ratios：4 個百分比數字（加總 100，可含 0），決定各檔出場手數；
        None = 均分（向後兼容，selfcheck 直調亦安全）。

        開倉本身失敗時例外外溢，由觸發層回報「下單失敗」且不重試；
        保護單失敗則在這裡當場補掛，並把沒掛齊的檔位放進 protection_failures。
        """
        sym = signal.symbol
        side = "buy" if signal.side == "long" else "sell"
        close_side = "sell" if side == "buy" else "buy"

        market = await self._adapter.fetch_market(sym)
        amount_step = market["precision"]["amount"]
        price_step = market["precision"]["price"]

        qty = round_down_qty(qty, amount_step)
        if qty <= 0:
            raise ValueError(f"手數取整後為 0：{sym}")

        await self._adapter.set_leverage(sym, signal.leverage)

        # 開倉：attached 隨單附 SL（Bybit）；非 attached 開倉後獨立掛 SL
        attached = bool(self._adapter.capabilities.get("supports_attached_sl"))
        order_type = "limit" if signal.is_limit else "market"
        entry_price = signal.entry if signal.is_limit else None
        entry_order = NewOrder(
            symbol=sym, side=side, order_type=order_type, qty=qty,
            price=entry_price, sl_price=signal.sl if attached else None,
            leverage=signal.leverage,
        )
        entry_id = await self._adapter.create_order(entry_order)
        log.info("開倉已提交：%s %s qty=%s id=%s", sym, side, qty, entry_id)

        # 保護單：開倉成功才掛；掛失敗當場補掛（最多 5 次、每次隔 2 秒）
        protection_failures: list[str] = []
        if not attached:
            async def _attach_sl():
                await self._adapter.attach_sl_tp(entry_id, sym, signal.sl, None)

            if not await run_with_retry(
                _attach_sl, label=f"訂單{signal.order_id} {sym} 止損掛單"
            ):
                protection_failures.append("止損")

        # 掛 TP reduce-only 限價單：前 n-1 檔按止盈部位比例 floor，末檔全清剩餘
        # （全平檔位 = 用戶最後一個非 0 檔與報單最後一檔取靠前者）
        tps = signal.tps or []
        tp_ids: list[str] = []
        if tps:
            exchange_min = exchange_min_qty(market, signal.entry)
            plans = plan_tp_tiers(qty, tps, tp_ratios, amount_step, exchange_min)
            for index, (price, tq) in enumerate(plans, start=1):
                tp_order = NewOrder(
                    symbol=sym, side=close_side, order_type="limit", qty=tq,
                    price=round_price(price, price_step), reduce_only=True,
                )

                async def _place_tp(order=tp_order):
                    tp_ids.append(await self._adapter.create_order(order))

                if not await run_with_retry(
                    _place_tp, label=f"訂單{signal.order_id} {sym} TP{index} 掛單"
                ):
                    protection_failures.append(f"TP{index}")
            log.info("已掛 %s 檔 TP：%s", len(tp_ids), sym)

        return {
            "entry_order_id": entry_id,
            "tp_order_ids": tp_ids,
            "qty": qty,
            "protection_failures": protection_failures,
        }

    async def close_all(self, symbol: str) -> bool:
        """全平剩餘 + 撤該 symbol 殘留掛單（終態清理）。返回是否有持倉被平。

        全平場景（止盈末檔/止損/手工平倉）後交易所可能殘留 reduce-only 掛單：
        無持倉時若仍有殘留掛單也必須撤（規則 2026-11-04）。TP1-3 部分平倉不調用本方法。
        """
        pos = await self._adapter.fetch_position(symbol)
        if pos and pos.get("qty", 0) > 0:
            close_side = "sell" if pos["side"] == "long" else "buy"
            order = NewOrder(
                symbol=symbol, side=close_side, order_type="market",
                qty=pos["qty"], reduce_only=True,
            )
            await self._adapter.create_order(order)
            log.info("已市價全平：%s（qty=%s）", symbol, pos["qty"])
            await self._adapter.cancel_all_for_symbol(symbol)
            return True
        # 無持倉：收到全平信號就是終態，該 symbol 的掛單一律撤乾淨。
        # 不再以「查得到殘留」當前提——條件單（獨立 SL/TP）可能不在查詢結果裡，
        # 查詢端點失敗也會造成盲區；撤單本身冪等無害，查不到就跳過反而會漏。
        try:
            orders = await self._adapter.fetch_open_orders(symbol)
        except Exception as exc:
            log.warning("查詢 %s 掛單失敗，仍執行終態撤單：%s", symbol, exc)
            orders = []
        await self._adapter.cancel_all_for_symbol(symbol)
        log.info("無持倉，已執行終態撤單：%s（查到 %s 個掛單）", symbol, len(orders))
        return False

    async def amend_sl(self, symbol: str, sl_price: float,
                       amount: float | None = None) -> None:
        """移動止損（自動移損/套保 → amend SL）。

        查無持倉視為「沒辦成」，丟出異常讓上層重試與視窗回報接住，
        不能靜默當成功。
        """
        pos = await self._adapter.fetch_position(symbol)
        if not pos:
            raise RuntimeError(f"交易所查無 {symbol} 持倉，無法移動止損")
        amt = amount if amount is not None else pos["qty"]
        await self._adapter.amend_order(order_id="", symbol=symbol,
                                        sl_price=sl_price, amount=amt)
        log.info("已移動止損：%s SL=%s", symbol, sl_price)

    async def apply_modify(self, symbol: str, side: str, mod_fields: dict,
                           old_tps: list[float], old_sl: float | None) -> list[str]:
        """✏️ 訂單修改：跟隨分析師逐項修改，只動變化項（規則 2026-11-04）。

        回傳「沒辦成的檔位說明」清單，讓觸發層在視窗回報並提示手動處理。
        - 某檔 TP 價格變了：撤舊檔掛單、按新價重掛（數量 = 原檔數量）
        - SL 變了：amend SL
        - 沒變的檔不動；舊掛單已不在（該檔已成交）→ 跳過並告警，不重掛
        - 有效性守衛：新價必須仍在有效方向（做多高於現價/做空低於現價），
          防止重掛後立即成交、誤平倉位
        """
        failures: list[str] = []
        new_sl = mod_fields.get("sl")
        if new_sl is not None and old_sl is not None and abs(new_sl - old_sl) > 1e-12:
            await self.amend_sl(symbol, new_sl)

        last = await self._adapter.fetch_ticker(symbol)
        price_step = (await self._adapter.fetch_market(symbol))["precision"]["price"]
        open_orders = await self._adapter.fetch_open_orders(symbol)
        tp_orders = [o for o in open_orders
                     if o.get("reduce_only") and o.get("type") == "limit"]

        for idx, new_p in sorted(mod_fields.get("tps") or []):
            old_p = old_tps[idx - 1] if idx - 1 < len(old_tps) else None
            if old_p is not None and abs(new_p - old_p) < 1e-12:
                continue  # 未變化檔不動
            if old_p is None:
                failures.append(f"TP{idx} 無舊價可比對，未修改")
                continue
            # 撤舊檔（按價格匹配掛單）
            replaced_qty = None
            for o in tp_orders:
                if o.get("price") is not None and abs(float(o["price"]) - old_p) < 1e-9:
                    await self._adapter.cancel_order(o["id"], symbol)
                    replaced_qty = float(o.get("qty") or 0)
                    break
            if replaced_qty is None:
                failures.append(f"TP{idx} 舊掛單已不在（可能已成交），未重掛")
                continue
            # 有效性守衛
            valid = (side == "long" and new_p > last) or (side == "short" and new_p < last)
            if not valid:
                failures.append(f"TP{idx} 新價 {new_p} 已越過現價 {last}，未重掛")
                continue
            close_side = "sell" if side == "long" else "buy"
            tp_order = NewOrder(
                symbol=symbol, side=close_side, order_type="limit", qty=replaced_qty,
                price=round_price(new_p, price_step), reduce_only=True,
            )
            await self._adapter.create_order(tp_order)
            log.info("已重掛 TP%s：%s @%s（qty=%s）", idx, symbol, new_p, replaced_qty)
        return failures
