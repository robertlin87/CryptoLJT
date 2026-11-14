"""架構 A 信號源：Telethon userbot。

同時監聽兩個事件通道：
- NewMessage：報單新消息 + 手工操作緊湊通知（如「📌訂單 188 … 🟥 已平倉」）
- MessageEdited：原報單被編輯（操作記錄追加行）

被踢出私密群后 Telegram 停止推送，軟件自然失效（群資格即授權）。
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import datetime

from telethon import TelegramClient, events

from ..constants import GROUP_CHAT_ID, GROUP_CHAT_TITLE, GROUP_NOT_JOINED_MSG
from ..proxy import resolve_proxy
from .base import MessageHandler, RawMessage, SignalSource

log = logging.getLogger("autotrade.signal_source.userbot")


def _build_proxy(proxy_cfg: dict | None):
    """把 config 的 proxy 段解析成 Telethon 代理參數。

    enabled=true 顯式用 config 的 type/addr/port；enabled=false 自動探測本機
    可用端口（端口變化零設定）；都無則 None（直連）。
    Telethon 1.45+ 的 proxy_type 用字符串 "http"/"socks5"，代理建立需要 python_socks。
    """
    return resolve_proxy(proxy_cfg, default_type="socks5")


def _patch_keepalive_interval(interval: float = 30.0) -> None:
    """把 Telethon 的 keepalive ping 間隔從預設 60s 降到 interval 秒。

    經本機網絡連線連 TG 時，長連線若空閒約 60s 會被中斷，
    而 Telethon 預設 60s 才 ping 一次，兩者撞車導致連線每 60s 被代理斷開
    （日誌刷 "Server closed the connection"）。把 ping 間隔降到 30s 保持連線
    活躍，避免被代理斷開。只 patch 一次，重複調用無副作用。
    """
    if getattr(TelegramClient, "_autotrade_patched_keepalive", False):
        return

    async def _keepalive_loop(self):
        rnd = lambda: random.randrange(-2**63, 2**63)
        while self.is_connected():
            try:
                await asyncio.wait_for(self.disconnected, timeout=interval)
                continue
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            except Exception:
                continue
            await self._clean_exported_senders()
            if not self._sender._transport_connected():
                continue
            try:
                self._sender._keepalive_ping(rnd())
            except (ConnectionError, asyncio.CancelledError):
                return

    TelegramClient._keepalive_loop = _keepalive_loop
    TelegramClient._autotrade_patched_keepalive = True


class TelegramUserbotSource(SignalSource):
    """Telethon userbot 信號源。

    信號群由開發者鎖定（constants.GROUP_CHAT_ID/TITLE），不接受自定義——
    防止軟件被用於監聽其他群。被踢出私密群后 Telegram 停止推送，
    軟件自然失效（群資格即授權）。
    """

    def __init__(
        self,
        handler: MessageHandler,
        api_id: int,
        api_hash: str,
        session_name: str = "autotrade_session",
        proxy: dict | None = None,
        prompt: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(handler)
        _patch_keepalive_interval()
        if api_id <= 0 or not api_hash:
            raise ValueError(
                "api_id/api_hash 未設定：請到 https://my.telegram.org 申請，"
                "填入 config/config.yaml 的 telegram 段"
            )
        self._client = TelegramClient(
            session_name, api_id, api_hash, proxy=_build_proxy(proxy)
        )
        self._prompt = prompt or input  # 可注入的提示回調（GUI 彈窗 / 命令行 input）
        self._chat_title = GROUP_CHAT_TITLE
        self._chat_id = GROUP_CHAT_ID
        self._target = None

    # ---- 公開入口 ----

    async def login(self):
        """首次交互登入（中文引導：手機號+驗證碼+2FA），成功後會話儲存在本地 session 檔案。

        驗證碼會發到你的 Telegram（app 通知或短信）；若帳號開了兩步驗證（2FA），
        還需輸入一次密碼。登入過一次後 session 檔案持久有效，無需重複登入。
        返回 me 信息（已登入或登入成功）供 GUI 顯示帳號名。
        """
        from telethon.errors import SessionPasswordNeededError

        try:
            await asyncio.wait_for(self._client.connect(), timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "連線 Telegram 超時，請檢查網絡連線後重試"
            ) from None
        if await self._client.is_user_authorized():
            me = await self._client.get_me()
            log.info("已登入過：%s (@%s)，無需重複登入", me.first_name, me.username)
            await self._client.disconnect()
            return me
        phone = (await asyncio.to_thread(
            self._prompt, "請輸入 TG 手機號（國際格式帶國家區號，如 +852 12345678）："
        )).strip()
        await self._client.send_code_request(phone)
        code = (await asyncio.to_thread(
            self._prompt, "請輸入 Telegram 收到的登入驗證碼："
        )).strip()
        try:
            await self._client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pwd = await asyncio.to_thread(
                self._prompt, "帳號開啟了兩步驗證（2FA），請輸入密碼："
            )
            await self._client.sign_in(password=pwd)
        me = await self._client.get_me()
        log.info("登入成功：%s (@%s)，會話已儲存", me.first_name, me.username)
        await self._client.disconnect()
        return me

    async def check_authorized(self) -> bool:
        """檢查 TG 是否已登入（connect → is_user_authorized → disconnect）。"""
        await self._client.connect()
        try:
            return await self._client.is_user_authorized()
        finally:
            await self._client.disconnect()

    async def check_group_access(self) -> tuple[bool, str]:
        """校驗目前帳號能否訪問鎖定的信號群（群資格即授權）。

        返回 (True, 群名) 或 (False, 失敗原因)。未加入私密群時 Telegram
        拒絕訪問（CHANNEL_PRIVATE），即為授權失敗——這也是"被踢即失效"
        的校驗點。
        """
        from telethon.errors import ChannelPrivateError, UserNotParticipantError

        await self._client.start()
        try:
            try:
                target = await self._resolve_chat()
            except (ChannelPrivateError, UserNotParticipantError):
                return False, GROUP_NOT_JOINED_MSG
            except Exception as e:  # noqa: BLE001
                return False, f"無法訪問信號群：{e}"
            self._check_group_privacy(target)
            return True, getattr(target, "title", GROUP_CHAT_TITLE)
        finally:
            # 必須釋放 session：Telethon 的 SQLiteSession.process_entities() 寫
            # entities 表不 commit，且 SQLiteSession 沒有 __del__；只有
            # disconnect() 內部的 session.close() 會 commit + 關閉連線。
            # 不關的話，嚮導裡下一個 TelegramClient 打開同一個 .session 檔案
            # 就會報 "database is locked"（自檢必失敗）。
            await self._client.disconnect()

    async def list_dialogs(self, limit: int = 50) -> list[tuple[str, int]]:
        """列出最近的群/頻道（群名, chat id），用於定位信號群。"""
        await self._client.start()
        try:
            out: list[tuple[str, int]] = []
            async for d in self._client.iter_dialogs(limit=limit):
                if d.is_group or d.is_channel:
                    out.append((d.name or "", d.id))
            return out
        finally:
            await self._client.disconnect()

    async def start(self) -> None:
        await self._client.start()
        me = await self._client.get_me()
        log.info("已登入 TG：%s (id=%s, @%s)", me.first_name, me.id, me.username)

        self._target = await self._resolve_chat()
        self._check_group_privacy(self._target)
        self._register_handlers()
        log.info("開始監聽群「%s」，關閉視窗即停止", getattr(self._target, "title", self._target))
        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        await self._client.disconnect()

    # ---- 內部 ----

    async def _resolve_chat(self):
        if self._chat_id:
            try:
                return await self._client.get_entity(int(self._chat_id))
            except Exception:
                # 未加入私密群時 get_entity 拋 CHANNEL_PRIVATE；按群名在會話列表兜底一次
                pass
        if self._chat_title:
            async for d in self._client.iter_dialogs():
                if d.name == self._chat_title:
                    return d.entity
        raise RuntimeError(GROUP_NOT_JOINED_MSG)

    def _check_group_privacy(self, target) -> None:
        # 私密群前提：公開群被踢後仍可輪詢歷史，群資格授權會被繞過
        username = getattr(target, "username", None)
        if username:
            log.warning(
                "⚠️ 目標群是公開群（@%s）：被踢出後仍可讀到歷史消息，授權模型不成立，建議改用私密群",
                username,
            )

    def _register_handlers(self) -> None:
        target = self._target

        @self._client.on(events.NewMessage(chats=target))
        async def _on_new(event):
            if event.message.text:
                await self._dispatch(event, is_edit=False)

        @self._client.on(events.MessageEdited(chats=target))
        async def _on_edit(event):
            if event.message.text:
                await self._dispatch(event, is_edit=True)

    async def _dispatch(self, event, is_edit: bool) -> None:
        msg = event.message
        raw = RawMessage(
            chat_id=event.chat_id or 0,
            chat_title=getattr(event.chat, "title", ""),
            sender_id=msg.sender_id,
            msg_id=msg.id,
            text=msg.text,
            ts=msg.date.isoformat(timespec="seconds") if msg.date else datetime.now().isoformat(timespec="seconds"),
            is_edit=is_edit,
        )
        await self._handler(raw)
