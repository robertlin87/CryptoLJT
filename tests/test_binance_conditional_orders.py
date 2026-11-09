"""Binance 條件掛單（獨立 SL/TP）查詢與撤單的回歸測試。

真實事故：訂單 225 GRASSUSDT 全部止盈後，獨立掛的 STOP_MARKET 止損單仍留在
交易所。原因是 ccxt 把帶 stopPrice 的單歸到「條件單」端點，而適配器只查／只撤
「普通掛單」端點，收尾時誤判「無掛單」而跳過清理。
"""
import unittest
from unittest.mock import MagicMock

from autotrade.execution.order_manager import OrderManager
from autotrade.execution.watchdog import Watchdog
from autotrade.exchange.binance import BinanceAdapter
from tests.test_execution import RecordingNotifier


def _ccxt_symbol(symbol: str) -> str:
    return f"{symbol[:-4]}/USDT:USDT"


def _conditional_sl(symbol="GRASSUSDT", trigger=0.5098, qty=474.0,
                    reduce_only=True, raw_reduce="true"):
    """ccxt parse_order 後的條件單結構（對應 Binance /fapi/v1/openAlgoOrders）。"""
    return {
        "id": "3358",
        "symbol": _ccxt_symbol(symbol),
        "side": "buy",
        "type": "market",
        "amount": qty,
        "price": None,
        "stopPrice": None,
        "triggerPrice": trigger,
        "reduceOnly": reduce_only,
        "info": {
            "algoId": 3358,
            "orderType": "STOP",
            "reduceOnly": raw_reduce,
            "closePosition": False,
            "triggerPrice": str(trigger),
            "quantity": str(qty),
        },
    }


def _limit_tp(symbol="GRASSUSDT", price=0.486675, qty=47.4):
    return {
        "id": "4444",
        "symbol": _ccxt_symbol(symbol),
        "side": "buy",
        "type": "limit",
        "amount": qty,
        "price": price,
        "stopPrice": None,
        "reduceOnly": True,
        "info": {"orderId": 4444, "reduceOnly": True},
    }


def _fake_adapter(regular=None, conditional=None, positions=None):
    """造一個不觸網的 BinanceAdapter：只換掉 ccxt 實例的網路方法。"""
    a = BinanceAdapter("k", "s")
    a._markets_loaded = True  # 跳過 load_markets（避免測試觸網）
    calls = {"fetch": [], "cancel_all": [], "cancel": [], "create": []}

    def _fetch(symbol=None, since=None, limit=None, params=None):
        p = dict(params or {})
        calls["fetch"].append((symbol, p))
        return list(conditional or []) if p.get("stop") else list(regular or [])

    def _cancel_all(symbol=None, params=None):
        calls["cancel_all"].append((symbol, dict(params or {})))
        return []

    a._ex.fetch_open_orders = MagicMock(side_effect=_fetch)
    a._ex.cancel_all_orders = MagicMock(side_effect=_cancel_all)
    a._ex.cancel_order = MagicMock(return_value={})
    a._ex.create_order = MagicMock(return_value={"id": "new"})
    a._ex.fetch_positions = MagicMock(return_value=positions or [])
    return a, calls


class TestConditionalOrderVisibility(unittest.IsolatedAsyncioTestCase):
    """治本：查掛單要看得到條件單，並認得出來是該清的減倉單。"""

    async def test_fetch_open_orders_includes_conditional_sl_and_marks_it_clearable(self):
        a, calls = _fake_adapter(regular=[_limit_tp()], conditional=[_conditional_sl()])
        orders = await a.fetch_open_orders("GRASSUSDT")

        self.assertEqual(len(orders), 2)
        sl = next(o for o in orders if o["conditional"])
        self.assertEqual(sl["symbol"], "GRASSUSDT")
        self.assertTrue(sl["reduce_only"])
        self.assertAlmostEqual(sl["stop_price"], 0.5098)
        self.assertEqual(len(calls["fetch"]), 2)
        self.assertTrue(calls["fetch"][1][1].get("stop"))

    async def test_conditional_sl_recognized_when_ccxt_reduce_only_missing(self):
        a, _ = _fake_adapter(conditional=[_conditional_sl(reduce_only=None)])
        orders = await a.fetch_open_orders("GRASSUSDT")
        self.assertTrue(orders[0]["reduce_only"])

    async def test_cancel_all_for_symbol_cancels_regular_and_conditional(self):
        a, calls = _fake_adapter()
        await a.cancel_all_for_symbol("GRASSUSDT")
        self.assertEqual(len(calls["cancel_all"]), 2)
        self.assertTrue(calls["cancel_all"][1][1].get("stop"))


class TestTerminalCleanupSeesConditionalOrders(unittest.IsolatedAsyncioTestCase):
    """查詢修好後，全平收尾、移動止損、30 秒巡邏都要能清掉條件單。"""

    async def test_close_all_cancels_when_only_conditional_sl_remains(self):
        a, calls = _fake_adapter(regular=[], conditional=[_conditional_sl()])
        closed = await OrderManager(a).close_all("GRASSUSDT")
        self.assertFalse(closed)  # 無持倉可平
        self.assertEqual(len(calls["cancel_all"]), 2)

    async def test_watchdog_cleans_conditional_sl_after_position_closed(self):
        a, calls = _fake_adapter(regular=[], conditional=[_conditional_sl()])
        notifier = RecordingNotifier()
        await Watchdog(a, notifier).tick()
        self.assertEqual(len(calls["cancel_all"]), 2)
        self.assertTrue(any("殘留掛單" in s for s in notifier.sent), notifier.sent)

    async def test_amend_sl_cancels_old_conditional_sl_with_stop_param(self):
        pos = [{"symbol": _ccxt_symbol("GRASSUSDT"), "side": "short",
                "contracts": 474.0, "entryPrice": 0.5028}]
        a, _ = _fake_adapter(regular=[], conditional=[_conditional_sl()], positions=pos)
        await a.amend_order("", "GRASSUSDT", sl_price=0.5)
        cancelled = a._ex.cancel_order.call_args_list
        self.assertTrue(any(len(c.args) >= 3 and (c.args[2] or {}).get("stop")
                            for c in cancelled), cancelled)

    async def test_close_all_cancels_pending_entry_limit_order(self):
        """全部止盈後連建倉用的限價單（非減倉單）也要撤乾淨。"""
        entry = {**_limit_tp(), "id": "5555", "reduceOnly": False,
                 "info": {"orderId": 5555, "reduceOnly": False}}
        a, calls = _fake_adapter(regular=[entry])
        await OrderManager(a).close_all("GRASSUSDT")
        self.assertEqual(len(calls["cancel_all"]), 2)

    async def test_close_all_still_cancels_when_conditional_query_fails(self):
        """條件單端點查不到時，收尾仍要發出撤單指令，不能靜默跳過。"""
        a, calls = _fake_adapter()

        def _fetch(symbol=None, since=None, limit=None, params=None):
            if (params or {}).get("stop"):
                raise RuntimeError("openAlgoOrders unavailable")
            return []

        a._ex.fetch_open_orders = MagicMock(side_effect=_fetch)
        await OrderManager(a).close_all("GRASSUSDT")
        self.assertEqual(len(calls["cancel_all"]), 2)


if __name__ == "__main__":
    unittest.main()
