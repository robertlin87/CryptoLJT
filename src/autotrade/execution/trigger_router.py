"""觸發路由器。

主觸發（價格）= 交易所掛單成交，不依賴 TG；本層是備份核對與人為指令通道：
- 新報單（Signal）→ 查交易所實態 → 風控 validate → OrderManager.enter 建立主觸發掛單
- 新事件（signal_ops 新行）→ 備份核對/指令（動作前查交易所實態兜底，杜絕雙賣）

失敗處理（使用者 2026-11-06 確認）：
- 建倉失敗 → 只回報交易所原因，不重試、不補單
- 建倉成功但保護單沒掛齊 → 由 OrderManager 當場補掛，這裡彙總提示手動處理
- 交易員主動動作失敗 → 當場最多重試 5 次、每次隔 2 秒，第 5 次仍失敗提示手動操作
- 結果通知（❌ 止損 / ✅ 全部止盈）不納入重試，只執行一次
- 重新開機不補做任何動作
"""
from __future__ import annotations

import logging

from ..config import DEFAULT_TP_RATIOS, parse_multiplier, parse_tp_ratios
from ..risk.engine import validate
from ..risk.precision import exchange_min_qty, last_positive_tier, round_down_qty
from .failure import describe_error, report_failure, run_with_retry

log = logging.getLogger("autotrade.execution.trigger_router")


class TriggerRouter:
    def __init__(self, adapter, order_manager, notifier,
                 risk_cfg: dict | None = None, analysts_cfg: dict | None = None) -> None:
        self._adapter = adapter
        self._om = order_manager
        self._notifier = notifier
        self._risk_cfg = risk_cfg or {}
        self._analysts_cfg = analysts_cfg or {}

    async def on_signal(self, signal) -> None:
        """新報單：查交易所實態 → 風控 → 進場（主觸發通道建立）。

        整段執行鏈（查餘額、查持倉、風控、開倉）的失敗都回報交易所原因；
        建倉失敗不重試、不補單，重新開機也不補做。
        """
        try:
            await self._on_signal_impl(signal)
        except Exception as exc:  # noqa: BLE001 - 交易所錯誤種類繁多，需原樣回報
            reason = describe_error(exc)
            report_failure(
                f"下單失敗：訂單{signal.order_id} {signal.symbol}，原因：{reason}"
            )
            await self._notifier.send(
                f"❌ 下單失敗 訂單{signal.order_id} {signal.symbol}：{reason}"
            )

    async def _on_signal_impl(self, signal) -> None:
        try:
            market = await self._adapter.fetch_market(signal.symbol)
        except ValueError as e:
            reason = describe_error(e)
            report_failure(f"幣種不可交易，跳過：{signal.symbol}，原因：{reason}")
            await self._notifier.send(f"⚠️ 幣種不可交易，跳過：{signal.symbol}（{reason}）")
            return

        contract_size = market["contractSize"]
        equity = await self._adapter.fetch_equity()
        open_positions = await self._adapter.fetch_all_positions()
        decision = validate(signal, equity, contract_size, open_positions,
                            self._risk_cfg, self._analysts_cfg)
        if not decision.accepted:
            detail = f"風控拒單：訂單{signal.order_id} {signal.symbol}，原因：{decision.reason}"
            report_failure(detail)
            await self._notifier.send(f"❌ {detail}")
            return

        # 下單倍數（使用者設定，預設 1）：報單「控倉維持 x%」的 R 值縮放，
        # 手數 = floor(equity × R×倍數 ÷ |進場−止損| ÷ 合約倍數) 隨之縮放。
        # 只接受大於 0 的整數或小數（嚮導與 config.yaml 已校驗，此處兜底）
        try:
            multiplier = parse_multiplier(self._risk_cfg.get("order_multiplier", 1.0))
        except ValueError as e:
            report_failure(f"下單倍數設定錯誤，報單未執行：{describe_error(e)}")
            await self._notifier.send(f"❌ 設定錯誤，跳過報單：{e}")
            return

        # 止盈部位比例（使用者設定，預設 25/25/25/25）：非法不拒單，回退均分
        # （均分 = 舊行為，安全預設）
        try:
            tp_ratios = parse_tp_ratios(
                self._risk_cfg.get("tp_ratios", DEFAULT_TP_RATIOS)
            )
        except ValueError as e:
            log.warning("止盈部位比例設定無效，回退均分 25/25/25/25：%s", e)
            tp_ratios = list(DEFAULT_TP_RATIOS)

        amount_step = market.get("precision", {}).get("amount")
        raw_qty = decision.qty * multiplier
        # 算出的手數按交易所要求的手數精度向下取整（不四捨五入），
        # 保證小數位合規、且不超過風控額度
        qty = round_down_qty(raw_qty, amount_step)

        # 手數上下限（使用者規則 2026-11-04，同日修正判斷閾值）：
        # - 交易所最低手數：amount.min 與交易所真實返回的 cost.min 折算取大者
        #   （交易所沒回傳 cost.min 時不再憑空用 5.0 兜底）
        # - 系統最低手數 = 交易所最低手數 × 實際掛檔數 n，保證每檔都能以交易所
        #   最低手數出場；n = min(報單檔數, 最後一個非 0 的比例檔位)，上限 4
        # - 算出的手數（已按精度向下取整）只要 < 系統最低手數，就按系統最低手數下單
        # - 高於最高手數 → 按最高手數交易
        limits = market.get("limits", {})
        amount_limits = limits.get("amount") or {}
        exchange_min = exchange_min_qty(market, signal.entry)
        n_tiers = max(1, min(len(signal.tps), last_positive_tier(tp_ratios)))
        system_min = round_down_qty(exchange_min * n_tiers, amount_step)
        if system_min > 0 and qty < system_min:
            qty = system_min
            log.info("低於系統最低手數：按交易所最低手數 x%d 下單 qty=%s（交易所最低=%s）",
                     n_tiers, qty, exchange_min)
        max_qty = amount_limits.get("max")
        if max_qty and qty > float(max_qty):
            qty = round_down_qty(float(max_qty), amount_step)
            log.info("高於最高手數：按最高手數下單 qty=%s", qty)
        if qty <= 0:
            report_failure(
                f"訂單{signal.order_id} {signal.symbol} 未執行："
                f"手數按交易所精度取整後為 0（原始 {raw_qty}），請手動確認"
            )
            await self._notifier.send(
                f"⚠️ 手數按交易所精度取整後為 0，跳過：{signal.symbol}（原始 {raw_qty}）"
            )
            return
        log.info("下單倍數 x%s：風控手數 %s → 按步長 %s 向下取整後實際手數 %s",
                 multiplier, raw_qty, amount_step, qty)

        # 開倉失敗由外層 on_signal 統一回報；這裡不捕捉、不重試、不補單
        result = await self._om.enter(signal, qty, tp_ratios)

        await self._notifier.send(
            f"✅ 已進場 訂單{signal.order_id} {signal.symbol} "
            f"{'做多' if signal.side == 'long' else '做空'} qty={result['qty']} SL={signal.sl}"
        )
        failures = result.get("protection_failures") or []
        if failures:
            detail = "、".join(failures)
            report_failure(
                f"訂單{signal.order_id} {signal.symbol} 已進場，但保護單未掛齊："
                f"{detail}，請手動處理"
            )
            await self._notifier.send(
                f"⚠️ 訂單{signal.order_id} {signal.symbol} 保護單未掛齊："
                f"{detail}，請手動處理"
            )

    async def on_event(self, order_id: str, symbol: str | None, action_type: str,
                       tp_index: int | None = None, price: float | None = None,
                       mod_fields: dict | None = None, old_signal=None) -> None:
        """備份觸發：事件核對/指令（規則與使用者確認 2026-11-04）。

        - sl_hit / all_tp / close / hedge_exit：全平四條件，一次性動作
          （訊號當下平掉該 symbol 全部頭寸與掛單，不重試、不事後補做；
           之後開新倉與本次無關）。人為指令失敗額外提示手動處理
        - move_sl / hedge：amend SL 至 price（人為指令，雙通道任一出現即執行）
        - modify：跟隨分析師逐項修改（撤舊 TP 重掛新價、SL 變更 amend）
        - tp_hit：純信息（主觸發掛單已自動止盈，TP1-3 部分平倉不撤單）
        """
        if symbol is None:
            report_failure(
                f"訂單{order_id} {action_type} 未執行：認不出是哪個幣，請手動處理"
            )
            await self._notifier.send(
                f"⚠️ 訂單{order_id} {action_type} 未執行：認不出是哪個幣，請手動處理"
            )
            return

        if action_type in ("sl_hit", "all_tp", "close", "hedge_exit"):
            # 全平是一次性動作（規則 2026-11-09）：訊號收到那一刻，把該 symbol
            # 手上所有頭寸與掛單清一次，就這一次。不重試、不排待辦、不事後補做；
            # 之後若開新倉，那是新倉自己的事，與本次訊號無關。
            # 人為指令（已平倉／套保出場）失敗時額外提示手動處理；結果通知不提示。
            is_manual = action_type in ("close", "hedge_exit")
            try:
                closed = await self._om.close_all(symbol)
            except Exception as exc:  # noqa: BLE001 - 交易所錯誤需原樣回報
                report_failure(
                    f"訂單{order_id} {symbol} {action_type} 清理失敗，"
                    f"原因：{describe_error(exc)}"
                    + ("，請手動操作" if is_manual else "")
                )
                if is_manual:
                    await self._notifier.send(
                        f"⚠️ 訂單{order_id} {symbol} 全平失敗，請手動操作"
                    )
                return
            await self._notifier.send(
                f"🟥 全平/撤單 {symbol}（{action_type}）"
                f"{'已執行' if closed else '無持倉僅記錄'}"
            )
        elif action_type in ("move_sl", "hedge") and price is not None:
            async def _amend_sl():
                await self._om.amend_sl(symbol, price)

            if not await run_with_retry(
                _amend_sl, label=f"訂單{order_id} {symbol} 移動止損"
            ):
                await self._notifier.send(
                    f"⚠️ 訂單{order_id} {symbol} 移動止損失敗，請手動操作"
                )
                return
            await self._notifier.send(f"🔒 移動止損 {symbol} SL={price}")
        elif action_type == "modify" and mod_fields is not None:
            if old_signal is None:
                report_failure(
                    f"訂單{order_id} {symbol} 訂單修改未執行：查無原報單存檔，"
                    f"無法比對舊價格，請手動處理"
                )
                await self._notifier.send(
                    f"⚠️ 訂單{order_id} {symbol} 訂單修改未執行：查無原報單存檔，請手動處理"
                )
                return

            state = {"failures": []}

            async def _apply_modify():
                state["failures"] = await self._om.apply_modify(
                    symbol, old_signal.side,
                    mod_fields, list(old_signal.tps), old_signal.sl,
                )

            if not await run_with_retry(
                _apply_modify, label=f"訂單{order_id} {symbol} 訂單修改"
            ):
                await self._notifier.send(
                    f"⚠️ 訂單{order_id} {symbol} 訂單修改失敗，請手動操作"
                )
                return
            if state["failures"]:
                detail = "、".join(state["failures"])
                report_failure(
                    f"訂單{order_id} {symbol} 訂單修改部分未辦成：{detail}，請手動處理"
                )
                await self._notifier.send(
                    f"⚠️ 訂單{order_id} {symbol} 訂單修改部分未辦成：{detail}，請手動處理"
                )
                return
            await self._notifier.send(
                f"✏️ 已跟隨修改 {symbol}：{mod_fields.get('tps')} SL={mod_fields.get('sl')}"
            )
        elif action_type == "tp_hit":
            # 純信息：TP1-3 部分平倉，剩餘檔位掛單保留；不做任何補單核對
            await self._notifier.send(f"🎯 TP{tp_index} 達成 {symbol} @{price}（信息，主觸發已止盈）")
        else:
            report_failure(
                f"訂單{order_id} {symbol} {action_type} 未執行："
                f"缺少必要資訊（如新價格）或無法識別，請手動處理"
            )
            await self._notifier.send(
                f"⚠️ 訂單{order_id} {symbol} {action_type} 未執行，請手動處理"
            )
