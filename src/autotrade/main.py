"""主程序裝配：雙通道消息 → 三類解析器 → SQLite 統一事件層 → TriggerRouter → 執行。

主觸發（價格）由 M2 交易所掛單承擔；本層是備份核對與人為指令通道。
數據流：Userbot(NewMessage+MessageEdited) → Entry/Notice/Op 解析器 →
signal_ops 冪等事件層 → TriggerRouter → OrderManager。
dry_run=true 時用 NoopAdapter（全鏈路不碰交易所），只記錄通知。
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import paths
from .config import load_config
from .exchange.factory import get_adapter
from .exchange.noop import NoopAdapter
from .execution.failure import describe_error, report_failure
from .execution.order_manager import OrderManager
from .execution.trigger_router import TriggerRouter
from .logfile import install_file_logging
from .notify.notifier import Notifier
from .parser.entry_parser import EntryParser
from .parser.notice_parser import NoticeParser
from .parser.op_parser import OpParser, modify_hash, op_line_hash
from .signal_source.base import RawMessage
from .signal_source.telegram_userbot import TelegramUserbotSource
from .storage.db import (add_signal_op, get_signal, init_db,
                         update_signal_fields, upsert_signal)

log = logging.getLogger("autotrade")


class MessagePipeline:
    """消息 → 事件：三類消息解析 + 統一事件層（冪等）→ router 觸發動作。"""

    def __init__(self, db, notifier: Notifier, router=None) -> None:
        self._db = db
        self._notifier = notifier
        self._router = router  # TriggerRouter；None 表示純 dry-run 記錄
        self._entry = EntryParser()
        self._notice = NoticeParser()
        self._op = OpParser()

    async def handle(self, raw: RawMessage) -> None:
        """信號源入口。單條消息異常不得中斷監聽。"""
        try:
            if raw.is_edit:
                await self._handle_edit(raw)
            else:
                await self._handle_new(raw)
        except Exception as exc:  # noqa: BLE001 - 任何沒辦成的動作都要在視窗說清原因
            report_failure(
                f"訊息處理失敗（msg_id={raw.msg_id}）：{describe_error(exc)}；"
                f"原文：{raw.text[:80]}"
            )

    # ---- 新消息通道：報單 or 緊湊通知 ----

    async def _handle_new(self, raw: RawMessage) -> None:
        signal = self._entry.parse(raw.text)
        if signal:
            await self._log_signal(raw, signal)
            return
        notice = self._notice.parse(raw.text)
        if notice:
            old_signal = None
            if notice.action_type == "modify":
                h = modify_hash(notice.mod_fields, notice.ts_text)
                old_signal = await get_signal(self._db, raw.chat_id, notice.order_id)
            else:
                h = op_line_hash(notice.action_type, notice.tp_index, notice.price)
            inserted = await add_signal_op(
                self._db, raw.chat_id, notice.order_id, h, notice.raw_text,
                source="notice", action_type=notice.action_type,
                tp_index=notice.tp_index, price=notice.price,
                ts_text=notice.ts_text,
            )
            if inserted:
                await self._notifier.send(
                    f"📌訂單 {notice.order_id} {notice.symbol} "
                    f"{'做多' if notice.side == 'long' else '做空'} {notice.action_type}（notice 通道）"
                )
                if self._router:
                    await self._router.on_event(
                        notice.order_id, notice.symbol, notice.action_type,
                        notice.tp_index, notice.price,
                        mod_fields=notice.mod_fields, old_signal=old_signal,
                    )
                if notice.action_type == "modify":
                    await self._apply_modify_db(raw.chat_id, notice.order_id,
                                                old_signal, notice.mod_fields)
            else:
                log.info("重複事件（冪等忽略）：%s", notice.raw_text[:60])
            return
        report_failure(
            f"訊息未識別、未執行（msg_id={raw.msg_id}）："
            f"{raw.text[:80].replace(chr(10), ' ⏎ ')}，請手動確認"
        )

    # ---- 編輯通道：報單 upsert + 操作行差量事件 ----

    async def _handle_edit(self, raw: RawMessage) -> None:
        signal = self._entry.parse(raw.text)
        if signal:
            sig_inserted = await upsert_signal(self._db, raw.chat_id, signal)
            if sig_inserted and self._router:
                await self._router.on_signal(signal)
        order_id = signal.order_id if signal else self._entry.extract_order_id(raw.text)
        for op in self._op.extract_ops(raw.text):
            if op.action_type == "ignore":
                continue
            if op.action_type == "unknown":
                # 未匹配行落庫供模板補齊；每次編輯重複出現只記一次
                log.info("未匹配操作行（供模板補齊）：%s", op.line_text.strip()[:80])
                if order_id:
                    await add_signal_op(
                        self._db, raw.chat_id, order_id,
                        op_line_hash("unknown", None, None, op.line_text),
                        op.line_text, source="edit", action_type="unknown",
                        ts_text=op.ts_text,
                    )
                continue
            if not order_id:
                report_failure(
                    f"操作行未執行：認不出訂單號，{op.line_text.strip()[:80]}，請手動處理"
                )
                continue
            old_signal = None
            if op.action_type == "modify":
                h = modify_hash(op.mod_fields, op.ts_text)
                old_signal = await get_signal(self._db, raw.chat_id, order_id)
            else:
                h = op_line_hash(op.action_type, op.tp_index, op.price)
            inserted = await add_signal_op(
                self._db, raw.chat_id, order_id, h, op.line_text,
                source="edit", action_type=op.action_type,
                tp_index=op.tp_index, price=op.price, ts_text=op.ts_text,
            )
            if inserted:
                detail = f" TP{op.tp_index}" if op.tp_index else ""
                price = f" @{op.price}" if op.price else ""
                await self._notifier.send(
                    f"✏️訂單 {order_id} {op.action_type}{detail}{price}（edit 通道）"
                )
                if self._router:
                    symbol = signal.symbol if signal else None
                    await self._router.on_event(
                        order_id, symbol, op.action_type, op.tp_index, op.price,
                        mod_fields=op.mod_fields, old_signal=old_signal,
                    )
                if op.action_type == "modify":
                    await self._apply_modify_db(raw.chat_id, order_id,
                                                old_signal, op.mod_fields)
            else:
                log.info("重複事件（冪等忽略）：%s", op.line_text.strip()[:60])

    async def _apply_modify_db(self, chat_id: int, order_id: str, old_signal, mod_fields) -> None:
        """改單後只同步「訊息裡明確寫出」的價格，沒提到的一律保持原值。

        TP 依檔位對號入座覆蓋，不是整串換掉：只改 TP3/TP4 時，
        TP1/TP2、SL、進場價動都不動。沒有舊報單可對照時不猜、不補 0。
        """
        fields = mod_fields or {}
        new_sl = fields.get("sl")
        new_entry = fields.get("entry")
        changes = sorted(fields.get("tps") or [])

        if changes and old_signal is None:
            log.warning("訂單%s 查無原報單存檔，未提及的價格不猜、不寫入", order_id)
            await update_signal_fields(self._db, chat_id, order_id,
                                       sl=new_sl, entry=new_entry)
            return

        new_tp_prices = None
        if changes:
            new_tp_prices = list(old_signal.tps)
            for idx, price in changes:
                if 1 <= idx <= len(new_tp_prices):
                    new_tp_prices[idx - 1] = price
                else:
                    log.warning("訂單%s 改單檔位 TP%s 超出原有檔數，略過", order_id, idx)
        await update_signal_fields(self._db, chat_id, order_id,
                                   new_tp_prices, new_sl, new_entry)

    async def _log_signal(self, raw: RawMessage, signal) -> None:
        warnings = self._entry.validate(signal)
        inserted = await upsert_signal(self._db, raw.chat_id, signal)
        if not inserted:
            log.info("已知報單更新 raw_text：訂單%s", signal.order_id)
            return
        text = (
            f"🆕新報單 訂單{signal.order_id} {signal.symbol} "
            f"{'做多' if signal.side == 'long' else '做空'} entry={signal.entry} "
            f"{'掛單' if signal.is_limit else '現價'} SL={signal.sl} "
            f"TP={'/'.join(map(str, signal.tps))} 控倉{signal.risk_pct}% "
            f"分析師:{signal.analyst}"
        )
        if warnings:
            text += " ⚠️" + "; ".join(warnings)
        await self._notifier.send(text)
        if self._router:
            await self._router.on_signal(signal)


async def run(config_path: str | None = None) -> None:
    """啟動跟單主程序。config_path 缺省時用外部目錄的 config.yaml。"""
    paths.ensure_dirs()
    paths.cleanup_temp_dir()  # 每日清理：只保留當天的日誌/報告
    install_file_logging()  # 日誌落盤 temp/autotrade.log（問題報告的數據來源）
    config_path = config_path or str(paths.config_path())
    cfg = load_config(config_path)
    db_path = Path(cfg.storage.get("db_path", str(paths.db_path())))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await init_db(str(db_path))
    notifier = Notifier(dry_run=cfg.dry_run, send_to=cfg.notify.get("send_to"))

    # M2 執行層：dry_run 用 NoopAdapter（不碰交易所），否則接真實交易所
    if cfg.dry_run:
        adapter = NoopAdapter()
    else:
        ex = cfg.exchanges
        active = ex.get("active", "bybit")
        adapter = get_adapter(active, ex.get(active, {}), proxy=ex.get("proxy"))
    order_manager = OrderManager(adapter)
    router = TriggerRouter(adapter, order_manager, notifier, cfg.risk, cfg.analysts)

    pipeline = MessagePipeline(db, notifier, router)

    tg = cfg.telegram
    # 信號群由 constants 鎖定（開發者固定），忽略設定檔案裡的 chat_title/chat_id
    source = TelegramUserbotSource(
        handler=pipeline.handle,
        api_id=int(tg.get("api_id", 0)),
        api_hash=tg.get("api_hash", ""),
        session_name=paths.session_path(tg.get("session_name", "autotrade_session")),
        proxy=tg.get("proxy"),
    )
    try:
        await source.start()
    finally:
        await db.close()
