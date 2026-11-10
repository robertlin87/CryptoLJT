"""純命令行首次設定向導與後續啟動選單。

由 scripts/run.py 在 main.run() 之前調用：
  - 無 config.yaml        → run_first_time_setup（TG 登入驗群 → 選交易所填欄位 → 風險乘數 → 寫設定）
  - 有 config.yaml        → prompt_startup_menu（1 改 TG / 2 改交易所 / 3 改風險乘數 / 回車監聽）

所有密鑰（api_hash、API Key/Secret/Passphrase）一律不回顯；中文文案不出現任何本機網絡連線表述。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from . import paths
from .cli_io import (
    choose_from_list,
    is_interactive,
    read_menu_key,
    read_text,
)
from .config import load_config, parse_multiplier
from .config_writer import (
    SESSION_NAME,
    blank_exchange_fields,
    build_config,
    friendly_selfcheck_error,
    write_config,
)
from .exchange.factory import get_adapter
from .exchange.schemas import EXCHANGE_ORDER, EXCHANGE_SCHEMAS
from .multiplier_prompt import prompt_multiplier, read_multiplier, save_multiplier
from .signal_source.telegram_userbot import TelegramUserbotSource
from .tp_ratios_prompt import (
    format_tp_ratios,
    prompt_tp_ratios,
    read_tp_ratios,
    read_tp_ratios_input,
    save_tp_ratios,
)

logger = logging.getLogger("autotrade.setup")


# --------------------------------------------------------------------------- #
# 內部異步流程：TG 登入+驗群、交易所自檢
# --------------------------------------------------------------------------- #

def _make_source(api_id: int, api_hash: str) -> TelegramUserbotSource:
    return TelegramUserbotSource(
        handler=lambda raw: None,
        api_id=int(api_id),
        api_hash=str(api_hash),
        session_name=paths.session_path(SESSION_NAME),
        proxy=None,  # 自動適配本機網絡連線
        prompt=input,
    )


async def _tg_login_and_verify(api_id: int, api_hash: str) -> tuple[bool, str]:
    """同一次事件循環內完成 登入 + 信號群驗群（Telethon 客戶端禁止跨循環複用）。"""
    src = _make_source(api_id, api_hash)
    try:
        me = await src.login()
        ok, msg = await src.check_group_access()
        if not ok:
            return False, f"TG 已登入（{getattr(me, 'first_name', '')}），但{msg}"
        return True, f"TG 已登入：{getattr(me, 'first_name', '')}；信號群校驗通過"
    except Exception as e:  # noqa: BLE001
        return False, friendly_selfcheck_error("TG 登入/驗群失敗", e)
    finally:
        try:
            await src.stop()
        except Exception:  # noqa: BLE001
            pass


async def _exchange_selfcheck(name: str, credentials: dict) -> tuple[bool, str]:
    """用填入的憑據構造適配器並讀取權益，驗證交易所可用性。"""
    try:
        adapter = get_adapter(name, credentials, proxy=None)
        equity = await adapter.fetch_equity()
        if equity is None:
            return False, "交易所餘額讀取異常（返回為空）"
        return True, f"{EXCHANGE_SCHEMAS[name]['label']} 連線成功，權益 {equity:g} USDT"
    except Exception as e:  # noqa: BLE001
        return False, friendly_selfcheck_error("交易所自檢失敗", e)


def _remove_session() -> None:
    """改 TG（換帳號）時刪除舊 session，否則舊 auth_key 會讓新憑證登入失敗。"""
    sp = paths.session_path(SESSION_NAME)
    for suffix in ("", ".session", "-journal", "-wal"):
        p = Path(sp + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 欄位讀取
# --------------------------------------------------------------------------- #

def _read_exchange_credentials(name: str) -> dict | None:
    """展示注意事項並逐項引導填欄位。全部空白 + 回車 = 返回 None（取消）；ESC = None。"""
    schema = EXCHANGE_SCHEMAS[name]
    print(f"\n  正在設定交易所：{schema['label']}")
    notes = schema.get("notes") or []
    if notes:
        print("  注意事項：")
        for n in notes:
            print(f"   - {n}")
    creds: dict[str, str] = {}
    for f in schema["fields"]:
        val = read_text(f"  請輸入 {f['label']}：", secret=f.get("secret", False), allow_esc=True)
        if val is None:  # ESC
            return None
        creds[f["key"]] = val.strip()
    if all(v == "" for v in creds.values()):
        return None  # 欄位完全空白 = 取消修改
    return creds


# --------------------------------------------------------------------------- #
# 首次設定向導
# --------------------------------------------------------------------------- #

def run_first_time_setup(config_path: str | Path) -> None:
    config_path = Path(config_path)
    print("=" * 46)
    print("  首次設定向導")
    print("=" * 46)
    if not is_interactive():
        print("  目前不是交互終端，無法引導設定。請在本機雙擊 start_run.bat 啟動。")
        sys.exit(1)

    paths.ensure_dirs()

    # 第一步：Telegram
    api_id, api_hash = _setup_telegram()

    # 第二步：交易所
    exchange, creds = _setup_exchange()

    # 第三步：風險乘數（先寫預設 1，再用 prompt_multiplier 讓使用者確認/修改）
    cfg = build_config(
        api_id=api_id,
        api_hash=api_hash,
        exchange=exchange,
        credentials=creds,
        order_multiplier=1.0,
    )
    write_config(config_path, cfg)
    print("\n  設定已寫入，最後設定風險乘數 R（預設 1）…")
    prompt_multiplier(config_path)
    print("\n  最後設定止盈部位比例（預設 25% / 25% / 25% / 25%）…")
    prompt_tp_ratios(config_path)
    print("\n  設定完成，正在進入監聽…\n")


def _setup_telegram() -> tuple[int, str]:
    print("\n[第一步] 設定 Telegram")
    while True:
        api_id_raw = read_text(
            "  請輸入 TG 的 api_id（純數字，從 my.telegram.org 申請）：", allow_esc=False
        )
        if api_id_raw is None:
            continue
        api_id_raw = api_id_raw.strip()
        api_hash = read_text("  請輸入 TG 的 api_hash：", secret=True, allow_esc=False)
        if api_hash is None:
            continue
        api_hash = api_hash.strip()
        if not api_id_raw or not api_hash:
            print("  api_id 與 api_hash 都不能為空，請重新輸入。")
            continue
        try:
            api_id = int(api_id_raw)
        except ValueError:
            print("  api_id 必須是純數字，請重新輸入。")
            continue
        if api_id <= 0:
            print("  api_id 必須大於 0，請重新輸入。")
            continue

        print("  正在登入 Telegram（按提示輸入手機號、驗證碼；若開啟兩步驗證還需密碼）…")
        ok, msg = asyncio.run(_tg_login_and_verify(api_id, api_hash))
        print(f"  {msg}")
        if ok:
            return api_id, api_hash
        print("  驗群未通過：請先加入「加密林教頭 VIP 社群」後再重試（無須重新填寫 api_id / api_hash）。")


def _setup_exchange() -> tuple[str, dict]:
    print("\n[第二步] 設定交易所")
    labels = [EXCHANGE_SCHEMAS[n]["label"] for n in EXCHANGE_ORDER]
    while True:
        idx = choose_from_list(labels, title="  請選擇交易所（輸入數字）：")
        if idx is None:
            print("  已取消，請重新選擇交易所。")
            continue
        name = EXCHANGE_ORDER[idx]
        creds = _read_exchange_credentials(name)
        if creds is None:
            print("  已取消填寫，請重新選擇交易所。")
            continue
        ok, msg = asyncio.run(_exchange_selfcheck(name, creds))
        print(f"  {msg}")
        if ok:
            return name, creds
        print("  交易所連線失敗，請重新選擇或重新填寫。")


# --------------------------------------------------------------------------- #
# 後續啟動選單
# --------------------------------------------------------------------------- #

def prompt_startup_menu(config_path: str | Path) -> None:
    """有設定時的修改選單。不改動直接回車即進入監聽。"""
    config_path = Path(config_path)
    if not is_interactive():
        return  # 非交互：不詢問，直接監聽

    cfg = load_config(str(config_path))
    current = read_multiplier(config_path)
    tp_current = read_tp_ratios(config_path)
    ex = cfg.exchanges if isinstance(cfg.exchanges, dict) else {}
    active = ex.get("active", "")
    exchange_label = EXCHANGE_SCHEMAS[active]["label"] if active in EXCHANGE_SCHEMAS else None
    print("=" * 46)
    print("  設定管理選單")
    print("=" * 46)
    print("  按 1  改 Telegram")
    if exchange_label:
        print(f"  按 2  改交易所（目前交易所：{exchange_label}）")
    else:
        print("  按 2  改交易所（目前還未綁定交易所）")
    print(f"  按 3  改風險乘數（目前風險乘數：{current:g}）")
    print(f"  按 4  改止盈部位比例（目前：{format_tp_ratios(tp_current)}）")
    print("  直接回車  開始監聽")
    print("-" * 46)

    while True:
        key = read_menu_key()
        if key in (b"\r", b"\n", b"\x1b"):
            return  # 不修改，進入監聽
        if key == b"1":
            _menu_modify_telegram(config_path)
            return
        if key == b"2":
            _menu_modify_exchange(config_path)
            return
        if key == b"3":
            _menu_modify_multiplier(config_path)
            return
        if key == b"4":
            _menu_modify_tp_ratios(config_path)
            return
        # read_menu_key 已過濾，僅合法鍵會到此處，正常不會到達


def _menu_modify_telegram(config_path: Path) -> None:
    print("\n[改 Telegram] 輸入新的 api_id / api_hash（任一留空或按 ESC 則取消修改）")
    api_id_raw = read_text("  請輸入 TG 的 api_id（留空=不修改）：", allow_esc=True)
    if api_id_raw is None:
        print("  已取消修改 Telegram。")
        return
    api_hash = read_text("  請輸入 TG 的 api_hash（留空=不修改）：", secret=True, allow_esc=True)
    if api_hash is None:
        print("  已取消修改 Telegram。")
        return
    api_id_raw = api_id_raw.strip()
    api_hash = api_hash.strip()
    if not api_id_raw and not api_hash:
        print("  兩項都為空，未修改 Telegram。")
        return
    if not api_id_raw or not api_hash:
        print("  api_id 與 api_hash 需同時填寫，已取消修改。")
        return
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("  api_id 必須是純數字，已取消修改。")
        return
    if api_id <= 0:
        print("  api_id 必須大於 0，已取消修改。")
        return

    _remove_session()
    print("  正在重新登入 Telegram…")
    ok, msg = asyncio.run(_tg_login_and_verify(api_id, api_hash))
    print(f"  {msg}")
    if not ok:
        print("  登入失敗，未修改設定。")
        return
    write_config(
        config_path,
        {"telegram": {"session_name": SESSION_NAME, "api_id": api_id, "api_hash": api_hash}},
    )
    print("  Telegram 設定已更新。")


def _menu_modify_exchange(config_path: Path) -> None:
    cfg = load_config(str(config_path))
    ex = cfg.exchanges if isinstance(cfg.exchanges, dict) else {}
    active = ex.get("active", "")
    if active and active in EXCHANGE_SCHEMAS:
        print(f"\n[改交易所] 目前綁定的交易所：{EXCHANGE_SCHEMAS[active]['label']}")
    else:
        print("\n[改交易所] 目前還未綁定交易所")
    labels = [EXCHANGE_SCHEMAS[n]["label"] for n in EXCHANGE_ORDER]
    idx = choose_from_list(labels, title="[改交易所] 請選擇交易所（輸入數字，ESC 取消）：")
    if idx is None:
        print("  已取消修改交易所。")
        return
    name = EXCHANGE_ORDER[idx]
    creds = _read_exchange_credentials(name)
    if creds is None:
        print("  已取消修改交易所。")
        return
    ok, msg = asyncio.run(_exchange_selfcheck(name, creds))
    print(f"  {msg}")
    if not ok:
        print("  連線失敗，未修改交易所。")
        return
    overlay = {"exchanges": {"active": name, **blank_exchange_fields(name, creds)}}
    write_config(config_path, overlay)
    print(f"  交易所已切換為 {EXCHANGE_SCHEMAS[name]['label']}。")


def _menu_modify_multiplier(config_path: Path) -> None:
    current = read_multiplier(config_path)
    print(f"\n[改風險乘數] 目前風險乘數：{current:g}")
    raw = read_text("  請輸入新的風險乘數（直接回車=不修改，ESC 取消）：", allow_esc=True)
    if raw is None:
        print("  已取消修改風險乘數。")
        return
    raw = raw.strip()
    if not raw:
        print("  未輸入，風險乘數保持不變。")
        return
    try:
        value = parse_multiplier(raw)
    except ValueError as e:
        print(f"  輸入無效：{e}，未修改。")
        return
    save_multiplier(config_path, value)
    print(f"  風險乘數已儲存為 {value:g}。")


def _menu_modify_tp_ratios(config_path: Path) -> None:
    current = read_tp_ratios(config_path)
    print(f"\n[改止盈部位比例] 目前：{format_tp_ratios(current)}")
    vals = read_tp_ratios_input(current)
    if vals is None:
        print("  已取消修改止盈部位比例。")
        return
    if vals == current:
        print("  未輸入，止盈部位比例保持不變。")
        return
    save_tp_ratios(config_path, vals)
    print(f"  止盈部位比例已儲存為 {format_tp_ratios(vals)}。")
