"""全平是一次性動作（使用者 2026-11-09 確認）。

四個條件會觸發全平：全部止盈、止損、已平倉、套保出場。
訊號收到的那一刻，把該品種手上所有頭寸與掛單清掉，就這一次：
不重試、不排待辦、不事後補做；之後若開新倉，那是新倉自己的事。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from autotrade.execution.order_manager import OrderManager
from autotrade.execution.trigger_router import TriggerRouter
from tests.test_execution import RecordingNotifier, StubAdapter, make_signal
from tests.test_execution_failures import EXCHANGE_REASON

TERMINAL_ACTIONS = ("all_tp", "sl_hit", "close", "hedge_exit")


class OneShotOrderManager(OrderManager):
    """記錄全平被呼叫幾次，可指定前幾次失敗。"""

    def __init__(self, adapter, fail_times=0):
        super().__init__(adapter)
        self.close_calls: list[str] = []
        self.fail_times = fail_times

    async def close_all(self, symbol: str) -> bool:
        self.close_calls.append(symbol)
        if len(self.close_calls) <= self.fail_times:
            raise RuntimeError(EXCHANGE_REASON)
        return False


class TestTerminalCloseIsOneShot(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch("autotrade.logfile.paths.temp_dir", return_value=Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _setup(self, fail_times=0):
        adapter = StubAdapter()
        om = OneShotOrderManager(adapter, fail_times=fail_times)
        notifier = RecordingNotifier()
        router = TriggerRouter(adapter, om, notifier)
        return router, adapter, om, notifier

    async def test_four_terminal_actions_each_close_exactly_once(self):
        for action in TERMINAL_ACTIONS:
            with self.subTest(action=action):
                router, _, om, _ = self._setup()
                with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
                    await router.on_event("225", "GRASSUSDT", action, None, 0.486675)
                self.assertEqual(om.close_calls, ["GRASSUSDT"])
                self.assertEqual(sleep.await_count, 0)

    async def test_failure_closes_once_reports_reason_and_never_retries(self):
        for action in TERMINAL_ACTIONS:
            with self.subTest(action=action):
                router, _, om, _ = self._setup(fail_times=99)
                with patch("asyncio.sleep", new_callable=AsyncMock) as sleep, \
                        self.assertLogs(level="ERROR") as logs:
                    await router.on_event("225", "GRASSUSDT", action, None, 0.486675)
                text = "\n".join(logs.output)
                self.assertEqual(len(om.close_calls), 1)
                self.assertEqual(sleep.await_count, 0)
                self.assertIn(EXCHANGE_REASON, text)
                self.assertNotIn("正在進行第", text)

    async def test_no_followup_close_when_new_entry_arrives_later(self):
        """全平失敗後不補做；之後的新報單照常進場，不被舊訊號牽連。"""
        router, adapter, om, _ = self._setup(fail_times=99)
        with self.assertLogs(level="ERROR"):
            await router.on_event("225", "GRASSUSDT", "all_tp")
        await router.on_signal(make_signal(order_id="226", symbol="GRASSUSDT"))
        self.assertEqual(len(om.close_calls), 1)
        self.assertTrue(adapter.created)


if __name__ == "__main__":
    unittest.main()
