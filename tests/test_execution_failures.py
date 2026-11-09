"""下單執行失敗：回報原因、當場重試、保護單補掛的回歸測試。

驗證使用者確認的規則：
- 跟下單執行有關的失敗，都要在視窗/日誌寫出交易所回的具體原因
- 交易員主動動作（移動止損／改單）失敗 → 當場最多重試 5 次，每次間隔
- 全平（全部止盈／止損／已平倉／套保出場）→ 一次性動作，失敗只報原因不重試
- 建倉失敗 → 只報原因，絕不重試、絕不補單
- 建倉成功但保護單（SL / TP）沒掛上 → 補掛，最多 5 次
- 重新開機不補做任何東西（本輪沒有任何開機重放路徑）
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from autotrade.execution.order_manager import OrderManager
from autotrade.execution.trigger_router import TriggerRouter
from tests.test_execution import RecordingNotifier, StubAdapter, make_signal

EXCHANGE_REASON = "Bybit retCode 10002: 餘額不足，無法下單"


class FailingAdapter(StubAdapter):
    """讓指定下單動作失敗若干次，其餘沿用 StubAdapter 的真實行為。"""

    def __init__(self, *, fail_entry=0, fail_protection=0, fail_amend=0,
                 position=None, reason=EXCHANGE_REASON, **kw):
        super().__init__(position=position, **kw)
        self.fail_entry = fail_entry
        self.fail_protection = fail_protection
        self.fail_amend = fail_amend
        self.reason = reason
        self.entry_attempts = 0
        self.protection_attempts = 0
        self.amend_attempts = 0

    async def create_order(self, o) -> str:
        if not o.reduce_only:
            self.entry_attempts += 1
            if self.entry_attempts <= self.fail_entry:
                raise RuntimeError(self.reason)
        else:
            self.protection_attempts += 1
            if self.protection_attempts <= self.fail_protection:
                raise RuntimeError(self.reason)
        return await super().create_order(o)

    async def amend_order(self, order_id: str, symbol: str, **kwargs) -> None:
        self.amend_attempts += 1
        if self.amend_attempts <= self.fail_amend:
            raise RuntimeError(self.reason)
        await super().amend_order(order_id, symbol, **kwargs)


class TestExecutionFailureReporting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(tmp.name)
        self.addCleanup(tmp.cleanup)
        patcher = patch("autotrade.logfile.paths.temp_dir", return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _router(self, adapter):
        notifier = RecordingNotifier()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier)
        return router, notifier

    def _no_wait(self):
        """把重試間隔的等待抽掉，讓測試不真的等 2 秒。"""
        return patch("asyncio.sleep", new_callable=AsyncMock)

    def _failed_actions_log(self) -> str:
        path = self.tmp / "failed_actions.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    async def test_manual_close_failure_is_one_shot_and_reports_reason(self):
        # 規則 2026-11-09：全平是一次性動作，失敗也不重試，只把原因說清楚
        adapter = FailingAdapter(
            fail_protection=99,
            position={"symbol": "LITUSDT", "side": "long", "qty": 10.0},
        )
        router, notifier = self._router(adapter)
        with self._no_wait() as sleep, self.assertLogs(level="ERROR") as logs:
            await router.on_event("216", "LITUSDT", "close")
        text = "\n".join(logs.output)
        self.assertIn(EXCHANGE_REASON, text)
        self.assertIn("請手動操作", text)
        self.assertNotIn("正在進行第", text)
        self.assertEqual(adapter.protection_attempts, 1)
        self.assertEqual(sleep.await_count, 0)
        self.assertTrue(any("請手動操作" in s for s in notifier.sent), notifier.sent)

    async def test_hedge_exit_failure_is_one_shot_and_prompts_manual_operation(self):
        adapter = FailingAdapter(
            fail_protection=99,
            position={"symbol": "LITUSDT", "side": "long", "qty": 10.0},
        )
        router, notifier = self._router(adapter)
        with self._no_wait() as sleep, self.assertLogs(level="ERROR") as logs:
            await router.on_event("216", "LITUSDT", "hedge_exit")
        text = "\n".join(logs.output)
        self.assertEqual(adapter.protection_attempts, 1)
        self.assertEqual(sleep.await_count, 0)
        self.assertIn("請手動操作", text)
        self.assertIn(EXCHANGE_REASON, text)
        self.assertTrue(any("請手動操作" in s for s in notifier.sent), notifier.sent)

    async def test_sl_hit_and_all_tp_notifications_are_not_retried(self):
        for action in ("sl_hit", "all_tp"):
            with self.subTest(action=action):
                adapter = FailingAdapter(
                    fail_protection=99,
                    position={"symbol": "LITUSDT", "side": "long", "qty": 10.0},
                )
                router, notifier = self._router(adapter)
                with self._no_wait() as sleep, self.assertLogs(level="ERROR") as logs:
                    await router.on_event("216", "LITUSDT", action, None, 4.91)
                text = "\n".join(logs.output)
                self.assertEqual(adapter.protection_attempts, 1)
                self.assertEqual(sleep.await_count, 0)
                self.assertNotIn("正在進行第", text)
                self.assertNotIn("請手動操作", text)
                self.assertIn(EXCHANGE_REASON, text)
                self.assertFalse(any("請手動操作" in s for s in notifier.sent))

    async def test_move_sl_failure_retries(self):
        adapter = FailingAdapter(
            fail_amend=2,
            position={"symbol": "LITUSDT", "side": "long", "qty": 10.0},
        )
        router, notifier = self._router(adapter)
        with self._no_wait(), self.assertLogs(level="INFO") as logs:
            await router.on_event("216", "LITUSDT", "hedge", None, 3.042)
        self.assertEqual(adapter.amend_attempts, 3)
        self.assertIn(EXCHANGE_REASON, "\n".join(logs.output))
        self.assertTrue(any("移動止損" in s for s in notifier.sent), notifier.sent)

    async def test_entry_failure_is_reported_and_never_retried(self):
        adapter = FailingAdapter(fail_entry=99)
        router, notifier = self._router(adapter)
        with self._no_wait() as sleep, self.assertLogs(level="ERROR") as logs:
            await router.on_signal(make_signal())
        text = "\n".join(logs.output)
        self.assertEqual(adapter.entry_attempts, 1)
        self.assertEqual(adapter.protection_attempts, 0)
        self.assertEqual(sleep.await_count, 0)
        self.assertIn("下單失敗", text)
        self.assertIn(EXCHANGE_REASON, text)
        self.assertFalse(any("已進場" in s for s in notifier.sent), notifier.sent)

    async def test_balance_lookup_failure_reports_reason(self):
        class NoEquityAdapter(FailingAdapter):
            async def fetch_equity(self) -> float:
                raise RuntimeError(EXCHANGE_REASON)

        router, _ = self._router(NoEquityAdapter())
        with self._no_wait(), self.assertLogs(level="ERROR") as logs:
            await router.on_signal(make_signal())
        text = "\n".join(logs.output)
        self.assertIn("下單失敗", text)
        self.assertIn(EXCHANGE_REASON, text)

    async def test_protection_orders_are_retried_after_entry_succeeds(self):
        adapter = FailingAdapter(fail_protection=2)
        with self._no_wait(), self.assertLogs(level="ERROR") as logs:
            result = await OrderManager(adapter).enter(make_signal(), 250.0)
        self.assertEqual(adapter.entry_attempts, 1)
        self.assertEqual(len(result["tp_order_ids"]), 4)
        self.assertEqual(result["protection_failures"], [])
        self.assertIn(EXCHANGE_REASON, "\n".join(logs.output))

    async def test_protection_failure_after_five_attempts_requires_manual_action(self):
        adapter = FailingAdapter(fail_protection=99)
        with self._no_wait(), self.assertLogs(level="ERROR") as logs:
            result = await OrderManager(adapter).enter(make_signal(), 250.0)
        self.assertEqual(adapter.entry_attempts, 1)
        self.assertEqual(result["tp_order_ids"], [])
        self.assertIn("TP1", result["protection_failures"])
        self.assertIn("請手動操作", "\n".join(logs.output))

    async def test_failure_reason_written_to_temp_log(self):
        adapter = FailingAdapter(fail_entry=99)
        router, _ = self._router(adapter)
        with self._no_wait(), self.assertLogs(level="ERROR"):
            await router.on_signal(make_signal())
        content = self._failed_actions_log()
        self.assertIn("下單失敗", content)
        self.assertIn(EXCHANGE_REASON, content)

    async def test_successful_action_writes_no_failure_record(self):
        router, _ = self._router(StubAdapter(position={"symbol": "LITUSDT",
                                                      "side": "long", "qty": 10.0}))
        with self._no_wait(), self.assertLogs(level="INFO"):
            await router.on_event("216", "LITUSDT", "close")
        self.assertEqual(self._failed_actions_log(), "")


if __name__ == "__main__":
    unittest.main()
