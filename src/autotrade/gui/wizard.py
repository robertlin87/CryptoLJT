"""設定精靈主視窗：分步收集設定 → TG 登入 → 固定群驗證 → 交易所 → 儲存自檢。

訊號群由開發者鎖定（constants.GROUP_CHAT_TITLE），不允許自訂——防止
軟體被用於監聽其他群。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import tkinter as tk
from tkinter import ttk

from .. import paths
from ..config import parse_multiplier, parse_tp_ratios
from ..config_writer import build_config, write_config, friendly_selfcheck_error
from ..constants import GROUP_CHAT_TITLE
from ..exchange.schemas import EXCHANGE_ORDER, EXCHANGE_SCHEMAS
from ..signal_source.telegram_userbot import TelegramUserbotSource
from .dialogs import GuiPrompter, show_error, show_info


class Wizard:
    """Tkinter 多步設定精靈。run() 進入主循環，儲存成功後銷毀視窗。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("加密林教頭 VIP跟單助手 — 首次設定")
        self.root.geometry("600x560")
        self.root.minsize(560, 480)
        self.prompter = GuiPrompter(self.root)

        # 收集的設定
        self.api_id = tk.StringVar()
        self.api_hash = tk.StringVar()
        self.exchange_name = tk.StringVar(value="bybit")  # 所選交易所（schema key）
        self.exchange_fields: dict[str, tk.StringVar] = {}  # 動態欄位 key -> var
        self.ex_notes = tk.StringVar()
        self.order_multiplier = tk.StringVar(value="1")  # 下單倍數（R 值縮放）
        self.tp_ratio_vars: list[tk.StringVar] = [
            tk.StringVar(value="25") for _ in range(4)
        ]  # 止盈部位比例（%，四檔合計 100，可填 0）
        self.tg_status = tk.StringVar(value="未登入")
        self.group_status = tk.StringVar(value=f"訊號群（固定）：{GROUP_CHAT_TITLE}")
        self.group_ok = False

        self.saved = False
        self._build_ui()
        self.root.after(100, self._poll_prompt)

    # ---- UI 構建 ----

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        nb.add(self._tab_welcome(nb), text="1 歡迎")
        nb.add(self._tab_telegram(nb), text="2 Telegram")
        nb.add(self._tab_exchange(nb), text="3 交易所")
        nb.add(self._tab_finish(nb), text="4 完成")

    def _tab_welcome(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=20)
        ttk.Label(f, text="歡迎使用 加密林教頭 VIP跟單助手",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(f, wraplength=520, justify="left", text=(
            "本工具會在你自己的電腦上登入你自己的 Telegram 帳號，監聽私密訊號群的報單，"
            "並自動在你的交易所帳戶跟單（下單、掛停損停利、移損、平倉）。\n\n"
            "開始前請先準備好：\n"
            "  1. TG 的 api_id 與 api_hash（到 https://my.telegram.org 申請）\n"
            "  2. 交易所的 API Key 與 Secret（Bybit，僅開啟「讀寫 + 合約交易」權限，請勿開啟提領）\n\n"
            "所有設定只儲存在你自己的電腦本機，不會上傳。"
        )).pack(anchor="w")
        return f

    def _tab_telegram(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=20)
        ttk.Label(f, text="填寫 Telegram API 並登入", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 10))

        ttk.Label(f, text="api_id").pack(anchor="w")
        ttk.Entry(f, textvariable=self.api_id, width=40).pack(anchor="w", pady=(0, 8))

        ttk.Label(f, text="api_hash").pack(anchor="w")
        ttk.Entry(f, textvariable=self.api_hash, width=40).pack(anchor="w", pady=(0, 12))

        ttk.Button(f, text="登入 Telegram", command=self._login_tg).pack(anchor="w")
        ttk.Label(f, textvariable=self.tg_status, foreground="#2563EB").pack(anchor="w", pady=(8, 12))

        ttk.Label(f, textvariable=self.group_status,
                  foreground="#2563EB").pack(anchor="w")
        ttk.Label(f, foreground="#64748B", justify="left", wraplength=520, text=(
            "訊號群由開發者固定，不可修改。登入後自動驗證你的帳號是否已加入該群；"
            "未加入或被移出群後，軟體將無法使用。"
        )).pack(anchor="w", pady=(6, 0))
        return f

    def _tab_exchange(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=20)
        ttk.Label(f, text="選擇你的交易所並填寫 API（你自己的帳戶）",
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 10))

        ttk.Label(f, text="交易所").pack(anchor="w")
        combo = ttk.Combobox(f, textvariable=self.exchange_name, state="readonly",
                             values=EXCHANGE_ORDER, width=40)
        combo.pack(anchor="w", pady=(0, 10))
        combo.bind("<<ComboboxSelected>>", lambda e: self._render_exchange_fields())

        # 動態欄位區 + 注意事項區（隨所選交易所重建）
        self._fields_frame = ttk.Frame(f)
        self._fields_frame.pack(anchor="w", fill="x", pady=(0, 6))
        self._notes_label = ttk.Label(f, textvariable=self.ex_notes,
                                      foreground="#B45309", wraplength=520, justify="left")
        self._notes_label.pack(anchor="w", pady=(6, 0))

        self._render_exchange_fields()
        return f

    def _render_exchange_fields(self) -> None:
        """依 EXCHANGE_SCHEMAS 重建所選交易所的欄位與注意事項。"""
        for w in self._fields_frame.winfo_children():
            w.destroy()
        schema = EXCHANGE_SCHEMAS[self.exchange_name.get()]
        self.exchange_fields = {}
        for field in schema["fields"]:
            row = ttk.Frame(self._fields_frame)
            row.pack(anchor="w", fill="x", pady=(0, 6))
            ttk.Label(row, text=field["label"], width=22).pack(side="left")
            var = tk.StringVar()
            ttk.Entry(row, textvariable=var, width=40,
                      show="*" if field["secret"] else "").pack(side="left")
            self.exchange_fields[field["key"]] = var
        self.ex_notes.set("注意事項：\n" + "\n".join(
            f"• {n}" for n in schema["notes"]
        ))

    def _tab_finish(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=20)
        ttk.Label(f, text="風控與儲存", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 10))

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(0, 10))
        ttk.Label(row, text="下單倍數").pack(side="left")
        ttk.Entry(row, textvariable=self.order_multiplier, width=12).pack(side="left", padx=(8, 0))
        ttk.Label(row, text="（只能填數字：整數或小數，如 1 / 0.5 / 1.25）",
                  foreground="#64748B").pack(side="left", padx=(8, 0))

        tp_row = ttk.Frame(f)
        tp_row.pack(anchor="w", pady=(0, 6))
        ttk.Label(tp_row, text="止盈部位比例（%）").pack(side="left")
        for i, var in enumerate(self.tp_ratio_vars, 1):
            ttk.Label(tp_row, text=f"TP{i}").pack(side="left", padx=(8, 2))
            ttk.Entry(tp_row, textvariable=var, width=6).pack(side="left", padx=(0, 4))
        ttk.Label(f, foreground="#64748B", wraplength=520, justify="left", text=(
            "四檔比例合計須為 100%（可填 0 = 該檔不掛單）；\n"
            "最後一個非 0 的檔位會全出剩餘部位；報單多出的檔位不掛單。"
        )).pack(anchor="w", pady=(0, 10))

        ttk.Label(f, foreground="#475569", wraplength=520, justify="left", text=(
            "報單「控倉維持 x% 風控」的 R 值乘以下單倍數：\n"
            "1 倍 = 依報單原值（如 2%）；0.5 倍 = 減半（1%）；2 倍 = 加倍（4%）。\n"
            "實際張數由公式算出（不可手填），並依交易所要求的張數小數位向下取整；\n"
            "張數低於交易所最低張數時，自動依最低張數 × TP 檔數下單（確保每檔都能出場）；\n"
            "高於最高張數時依最高張數下單。\n"
            "同時持有的幣種數不設上限。\n"
            "儲存前會自動檢測 TG 登入與交易所連線狀態。"
        )).pack(anchor="w", pady=(10, 0))
        ttk.Button(f, text="儲存並自檢", command=self._save_and_verify).pack(anchor="w", pady=20)
        return f

    # ---- 輪詢 ----

    def _poll_prompt(self) -> None:
        self.prompter.poll()
        self.root.after(100, self._poll_prompt)

    # ---- TG 登入（背景 asyncio）----

    def _make_source(self) -> TelegramUserbotSource:
        return TelegramUserbotSource(
            handler=lambda raw: None,
            api_id=int(self.api_id.get() or 0),
            api_hash=self.api_hash.get().strip(),
            session_name=paths.session_path("autotrade_session"),
            proxy=None,  # 自動適配本機網路連線
            prompt=self.prompter.prompt,
        )

    def _login_tg(self) -> None:
        if int(self.api_id.get() or 0) <= 0 or not self.api_hash.get().strip():
            show_error(self.root, "錯誤", "請先填寫 TG 的 api_id 和 api_hash")
            return
        self.tg_status.set("登入中…")

        def _run():
            try:
                src = self._make_source()

                async def _flow():
                    """同一事件循環內完成 登入 + 群權限驗證（Telethon 客戶端禁止跨循環複用）。"""
                    me = await src.login()
                    ok, msg = await src.check_group_access()
                    return me, ok, msg

                me, ok, msg = asyncio.run(_flow())
                name = f"{me.first_name or ''} (@{me.username or ''})".strip()
                self.root.after(0, lambda: self.tg_status.set(f"已登入：{name}"))
                self.group_ok = ok
                self.root.after(0, lambda: self.group_status.set(
                    f"✅ {msg}" if ok else f"❌ {msg}"
                ))
            except Exception as e:  # noqa: BLE001
                logging.getLogger("autotrade").exception("TG 登入流程失敗")
                self.root.after(0, lambda: self.tg_status.set(f"登入失敗：{e}"))

        threading.Thread(target=_run, daemon=True).start()

    # ---- 儲存 + 自檢 ----

    def _save_and_verify(self) -> None:
        if not self.api_id.get().strip() or not self.api_hash.get().strip():
            show_error(self.root, "錯誤", "請填寫 TG 的 api_id 和 api_hash")
            return
        try:
            parse_multiplier(self.order_multiplier.get())
        except ValueError as e:
            show_error(self.root, "錯誤", str(e))
            return
        try:
            # 空輸入框視為 0（與 CLI 的「未填 = 0」語義一致）
            parse_tp_ratios([v.get() or "0" for v in self.tp_ratio_vars])
        except ValueError as e:
            show_error(self.root, "錯誤", str(e))
            return
        schema = EXCHANGE_SCHEMAS[self.exchange_name.get()]
        missing = [f["label"] for f in schema["fields"]
                   if not (self.exchange_fields.get(f["key"]) or tk.StringVar(value="")).get().strip()]
        if missing:
            show_error(self.root, "錯誤", "請填寫交易所的：" + "、".join(missing))
            return

        def _run():
            errors = []
            # TG 自檢 + 固定群權限驗證（同一事件循環內完成，避免跨循環複用客戶端）
            try:
                src = self._make_source()

                async def _tg_check():
                    if not await src.check_authorized():
                        return "TG 未登入：請先在「Telegram」頁完成登入"
                    ok, msg = await src.check_group_access()
                    if not ok:
                        return msg
                    return ""

                err = asyncio.run(_tg_check())
                if err:
                    errors.append(err)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("autotrade").exception("TG 自檢失敗")
                errors.append(friendly_selfcheck_error("TG 自檢失敗", e))

            # 交易所自檢（動態：依所選交易所建構配接器）
            try:
                from ..exchange.factory import get_adapter
                api_cfg = {k: v.get().strip() for k, v in self.exchange_fields.items()}
                adapter = get_adapter(self.exchange_name.get(), api_cfg)
                equity = asyncio.run(adapter.fetch_equity())
                if equity is None:
                    errors.append("交易所餘額讀取異常")
            except Exception as e:  # noqa: BLE001
                errors.append(friendly_selfcheck_error("交易所自檢失敗", e))

            if errors:
                self.root.after(0, lambda: show_error(self.root, "自檢未通過", "\n".join(errors)))
                return

            try:
                self._write_config()
                self.saved = True
                self.root.after(0, self._on_saved)
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda: show_error(self.root, "儲存失敗", str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _write_config(self) -> None:
        paths.ensure_dirs()
        credentials = {k: v.get().strip() for k, v in self.exchange_fields.items()}
        cfg = build_config(
            api_id=int(self.api_id.get() or 0),
            api_hash=self.api_hash.get().strip(),
            exchange=self.exchange_name.get(),
            credentials=credentials,
            order_multiplier=parse_multiplier(self.order_multiplier.get()),
            tp_ratios=parse_tp_ratios([v.get() or "0" for v in self.tp_ratio_vars]),
        )
        write_config(paths.config_path(), cfg)

    def _on_saved(self) -> None:
        show_info(self.root, "設定完成", f"設定已儲存到：\n{paths.config_path()}\n\n現在可以啟動跟單了。")
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
