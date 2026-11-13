"""幣種別名回歸測試：同一個幣在不同交易所有不同合約名時的雙向兜底。

真實場景：報單寫 PLAYUSDT，交易所上架的是 PLAYSOUTUSDT（反之亦然），
以及既有的 PUMPUSDT / PUMPFUNUSDT。找不到報單原名時應自動改用另一個名字，
讓進場、全平、撤單都用同一個正確的幣種。
"""
import unittest

from autotrade.exchange.noop import NoopAdapter
from autotrade.exchange.symbol_aliases import alias_candidates, resolve_symbol
from autotrade.execution.order_manager import OrderManager
from autotrade.execution.trigger_router import TriggerRouter
from tests.test_execution import RecordingNotifier, StubAdapter, make_signal


class MarketOnlyAdapter(NoopAdapter):
    """只認得 available 裡的交易對；其餘比照各家適配器拋 ValueError。"""

    def __init__(self, available):
        super().__init__()
        self.available = set(available)
        self.queried: list[str] = []

    async def fetch_market(self, symbol: str) -> dict:
        self.queried.append(symbol)
        if symbol not in self.available:
            raise ValueError(f"無此交易對：{symbol}")
        return await super().fetch_market(symbol)


class AliasStubAdapter(StubAdapter):
    """可控假交易所：只讓 available 內的幣通過 fetch_market。"""

    def __init__(self, available, **kw):
        super().__init__(**kw)
        self.available = set(available)

    async def fetch_market(self, symbol: str) -> dict:
        if symbol not in self.available:
            raise ValueError(f"無此交易對：{symbol}")
        return await super().fetch_market(symbol)


class TestAliasCandidates(unittest.TestCase):
    def test_play_both_directions(self):
        self.assertEqual(alias_candidates("PLAYUSDT"),
                         ["PLAYUSDT", "PLAYSOUTUSDT"])
        self.assertEqual(alias_candidates("PLAYSOUTUSDT"),
                         ["PLAYSOUTUSDT", "PLAYUSDT"])

    def test_pump_both_directions(self):
        self.assertEqual(alias_candidates("PUMPUSDT"),
                         ["PUMPUSDT", "PUMPFUNUSDT"])
        self.assertEqual(alias_candidates("PUMPFUNUSDT"),
                         ["PUMPFUNUSDT", "PUMPUSDT"])

    def test_unrelated_symbol_has_no_alias(self):
        self.assertEqual(alias_candidates("LITUSDT"), ["LITUSDT"])


class TestResolveSymbol(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_when_reported_name_missing(self):
        a = MarketOnlyAdapter({"PLAYSOUTUSDT"})
        self.assertEqual(await resolve_symbol(a, "PLAYUSDT"), "PLAYSOUTUSDT")
        self.assertEqual(a.queried, ["PLAYUSDT", "PLAYSOUTUSDT"])

    async def test_falls_back_in_reverse_direction(self):
        a = MarketOnlyAdapter({"PLAYUSDT"})
        self.assertEqual(await resolve_symbol(a, "PLAYSOUTUSDT"), "PLAYUSDT")

    async def test_original_name_wins_when_available(self):
        a = MarketOnlyAdapter({"PLAYUSDT", "PLAYSOUTUSDT"})
        self.assertEqual(await resolve_symbol(a, "PLAYUSDT"), "PLAYUSDT")
        self.assertEqual(a.queried, ["PLAYUSDT"])

    async def test_returns_original_when_no_candidate_exists(self):
        a = MarketOnlyAdapter(set())
        self.assertEqual(await resolve_symbol(a, "PLAYUSDT"), "PLAYUSDT")

    async def test_unrelated_symbol_causes_no_extra_lookup(self):
        a = MarketOnlyAdapter({"LITUSDT"})
        self.assertEqual(await resolve_symbol(a, "LITUSDT"), "LITUSDT")
        self.assertEqual(a.queried, [])

    async def test_pump_alias_still_works(self):
        a = MarketOnlyAdapter({"PUMPFUNUSDT"})
        self.assertEqual(await resolve_symbol(a, "PUMPUSDT"), "PUMPFUNUSDT")

    async def test_non_value_error_propagates(self):
        class BrokenAdapter(MarketOnlyAdapter):
            async def fetch_market(self, symbol: str) -> dict:
                raise RuntimeError("network down")

        with self.assertRaises(RuntimeError):
            await resolve_symbol(BrokenAdapter({"PLAYSOUTUSDT"}), "PLAYUSDT")


class TestBybitAliasCoexistence(unittest.TestCase):
    """Bybit 既有的單向映射（PUMP→PUMPFUN）不因新機制改變。"""

    def test_both_pump_names_reach_same_contract(self):
        from autotrade.exchange.bybit import BybitAdapter

        self.assertEqual(BybitAdapter.to_ccxt_symbol("PUMPUSDT"),
                         "PUMPFUN/USDT:USDT")
        self.assertEqual(BybitAdapter.to_ccxt_symbol("PUMPFUNUSDT"),
                         "PUMPFUN/USDT:USDT")

    def test_play_aliases_are_not_rewritten_by_bybit(self):
        from autotrade.exchange.bybit import BybitAdapter

        self.assertEqual(BybitAdapter.to_ccxt_symbol("PLAYUSDT"),
                         "PLAY/USDT:USDT")
        self.assertEqual(BybitAdapter.to_ccxt_symbol("PLAYSOUTUSDT"),
                         "PLAYSOUT/USDT:USDT")


class TestRouterUsesResolvedSymbol(unittest.IsolatedAsyncioTestCase):
    def _router(self, adapter):
        notifier = RecordingNotifier()
        return TriggerRouter(adapter, OrderManager(adapter), notifier), notifier

    async def test_entry_uses_exchange_symbol(self):
        adapter = AliasStubAdapter(available={"PLAYSOUTUSDT"})
        router, notifier = self._router(adapter)
        await router.on_signal(make_signal(symbol="PLAYUSDT"))
        self.assertTrue(adapter.created, "進場單應送出")
        self.assertEqual(adapter.created[0]["symbol"], "PLAYSOUTUSDT")
        self.assertFalse(any("下單失敗" in s or "幣種不可交易" in s
                             for s in notifier.sent), notifier.sent)

    async def test_terminal_close_uses_exchange_symbol(self):
        adapter = AliasStubAdapter(available={"PLAYSOUTUSDT"})
        router, _ = self._router(adapter)
        await router.on_event("216", "PLAYUSDT", "close")
        self.assertEqual(adapter.cancelled, ["PLAYSOUTUSDT"])

    async def test_reported_name_untouched_when_exchange_has_it(self):
        adapter = AliasStubAdapter(available={"PLAYUSDT", "PLAYSOUTUSDT"})
        router, _ = self._router(adapter)
        await router.on_signal(make_signal(symbol="PLAYUSDT"))
        self.assertEqual(adapter.created[0]["symbol"], "PLAYUSDT")


if __name__ == "__main__":
    unittest.main()
