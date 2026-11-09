"""下單執行失敗的統一處理：把交易所回的原因說清楚，並當場重試。

規則（使用者 2026-11-06 確認）：
- 跟下單執行有關的失敗，一律在視窗與日誌寫出交易所回的具體原因
- 需要重試的動作（移動止損／訂單修改／保護單補掛）：最多 5 次，每次間隔 2 秒
- 全平（規則 2026-11-09）是一次性動作，不經本模組重試：失敗只報原因
- 建倉失敗不重試；重新開機也不補做任何動作（此模組沒有開機重放路徑）
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .. import logfile

log = logging.getLogger("autotrade.execution.failure")

MAX_ATTEMPTS = 5
RETRY_INTERVAL_SEC = 2.0
_MAX_REASON_LEN = 500


def describe_error(exc: BaseException) -> str:
    """把交易所回的錯誤原文整理成一行，保留下來供 debug。"""
    text = str(exc).strip()
    if not text:
        text = type(exc).__name__
    return " ".join(text.split())[:_MAX_REASON_LEN]


def report_failure(message: str) -> None:
    """寫到 BAT 視窗 / temp 日誌，並把完整原因記進 temp/failed_actions.log。"""
    log.error("%s", message)
    logfile.record_failure(message)


def report_success(message: str) -> None:
    log.info("%s", message)


async def run_with_retry(
    action: Callable[[], Awaitable[object]],
    *,
    label: str,
    attempts: int | None = None,
    interval: float | None = None,
) -> bool:
    """執行單一動作；失敗當場重試。回傳 True=成功、False=已用盡次數。

    只捕捉「這一次動作」的例外，不外溢到訊息處理管線，避免整條監聽被中斷。
    """
    total = MAX_ATTEMPTS if attempts is None else attempts
    wait = RETRY_INTERVAL_SEC if interval is None else interval
    for n in range(1, total + 1):
        try:
            await action()
        except Exception as exc:  # noqa: BLE001 - 交易所錯誤種類繁多，需原樣回報
            reason = describe_error(exc)
            if n >= total:
                report_failure(
                    f"{label}：連續 {total} 次執行未成功，請手動操作。原因：{reason}"
                )
                return False
            report_failure(
                f"{label}：因為 {reason}，執行未成功，正在進行第 {n} 次嘗試"
            )
            if wait > 0:
                await asyncio.sleep(wait)
        else:
            report_success(f"{label}：執行成功")
            return True
    return False
