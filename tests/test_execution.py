"""M2 執行層單測：風控 validate、PositionTracker、OrderManager、TriggerRouter、Watchdog。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from autotrade.exchange.noop import NoopAdapter
from autotrade.execution.order_manager import OrderManager
from autotrade.execution.position_tracker import PositionTracker, PositionState
from autotrade.execution.trigger_router import TriggerRouter
from autotrade.execution.watchdog import Watchdog
from autotrade.notify.notifier import Notifier
from autotrade.parser.entry_parser import Signal
from autotrade.risk.engine import validate


class StubAdapter(NoopAdapter):
    """可控假適配器：position/positions/open_orders 可注入，記錄撤單與下單動作。"""

    def __init__(self, position=None, positions=None, open_orders=None, ticker=1.0):
        super().__init__()
        self.position = position
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.ticker = ticker
        self.cancelled: list[str] = []
        self.cancelled_ids: list[str] = []
        self.created: list[dict] = []
        self.amended: list[tuple[str, float]] = []

    async def fetch_position(self, symbol: str) -> dict | None:
        return self.position

    async def fetch_all_positions(self) -> list[dict]:
        return self.positions

    async def fetch_ticker(self, symbol: str) -> float:
        return self.ticker

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict]:
        return self.open_orders

    async def cancel_all_for_symbol(self, symbol: str) -> None:
        self.cancelled.append(symbol)

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        self.cancelled_ids.append(order_id)

    async def amend_order(self, order_id: str, symbol: str, **kwargs) -> None:
        self.amended.append((symbol, kwargs.get("sl_price")))

    async def create_order(self, o) -> str:
        self.created.append({"symbol": o.symbol, "side": o.side, "qty": o.qty,
                             "reduce_only": o.reduce_only, "sl_price": o.sl_price,
                             "price": o.price, "order_type": o.order_type})
        return f"stub-{len(self.created)}"


def make_signal(**kw) -> Signal:
    base = dict(order_id="216", symbol="LITUSDT", side="long", entry=3.042,
                is_limit=False, leverage=10, sl=2.962,
                tps=[3.1213, 3.1788, 3.2411, 3.2856],
                margin_pct=7.61, risk_pct=2.00, stop_dist_pct=2.63, analyst="Lazycat")
    base.update(kw)
    return Signal(**base)


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__(dry_run=False)
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class TestRiskValidate(unittest.TestCase):
    def test_accept_and_qty(self):
        d = validate(make_signal(), 1000.0, 1.0)
        self.assertTrue(d.accepted)
        self.assertEqual(d.qty, 250)

    def test_zero_qty_not_rejected_by_risk(self):
        # 算出 0 手不再拒單，交給執行層的系統最低手數託底
        d = validate(make_signal(entry=100.0, sl=50.0), 1000.0, 1.0)
        self.assertTrue(d.accepted)
        self.assertEqual(d.qty, 0)

    def test_analyst_not_allowed(self):
        d = validate(make_signal(), 1000.0, 1.0,
                     analysts_cfg={"allow_all": False, "list": ["Trader X"]})
        self.assertFalse(d.accepted)
        self.assertIn("分析師", d.reason)

    def test_reverse_signal(self):
        positions = [{"symbol": "LITUSDT", "side": "short"}]
        d = validate(make_signal(), 1000.0, 1.0, open_positions=positions)
        self.assertFalse(d.accepted)
        self.assertIn("反向", d.reason)

    def test_no_concurrent_position_limit(self):
        # 規則 2026-11-09 移除：同時持有多少幣種不再設上限。
        # 連配置裡仍殘留 max_concurrent_positions 也不得再生效。
        positions = [
            {"symbol": "AUSDT", "side": "long"},
            {"symbol": "BUSDT", "side": "long"},
            {"symbol": "CUSDT", "side": "long"},
            {"symbol": "DUSDT", "side": "long"},
            {"symbol": "EUSDT", "side": "long"},
        ]
        d = validate(make_signal(), 1000.0, 1.0, open_positions=positions,
                     risk_cfg={"max_concurrent_positions": 3})
        self.assertTrue(d.accepted)

    def test_margin_mismatch_only_warns_and_still_accepts(self):
        # 報單數字對不上只提醒，不再把單子廢掉
        with self.assertLogs("autotrade.risk.engine", level="WARNING") as logs:
            d = validate(make_signal(margin_pct=50.0), 1000.0, 1.0)
        self.assertTrue(d.accepted)
        self.assertIn("保證金", "\n".join(logs.output))

    def test_small_notional_no_longer_rejected(self):
        # 規則 2026-11-09 移除：不再由程式自訂名義值下限拒單；
        # 交易所若不接受會自己回原因。連配置裡殘留 min_notional 也不得生效。
        d = validate(make_signal(), 100.0, 1.0, risk_cfg={"min_notional": 200.0})
        self.assertTrue(d.accepted)

    def test_daily_loss_cap_removed(self):
        # 規則 2026-11-09 移除：單日虧損上限是從沒接線的空殼，整套清掉。
        # 風控不再接受當日損益參數，配置裡殘留 daily_loss_pct 也不得生效。
        import inspect
        self.assertNotIn("daily_pnl", inspect.signature(validate).parameters)
        d = validate(make_signal(), 1000.0, 1.0,
                     risk_cfg={"daily_loss_pct": 0.0001})
        self.assertTrue(d.accepted)


class TestPositionTracker(unittest.TestCase):
    def test_lifecycle(self):
        t = PositionTracker()
        p = t.open("LITUSDT", "long", 100.0, 3.042, 2.962)
        self.assertEqual(p.state, PositionState.PENDING)
        t.mark_filled("LITUSDT")
        self.assertEqual(p.state, PositionState.FILLED)
        t.on_tp_hit("LITUSDT", 25.0)
        self.assertEqual(p.state, PositionState.PARTIAL_TP)
        self.assertEqual(p.tp_hit_count, 1)
        self.assertEqual(p.remaining_qty, 75.0)
        t.on_breakeven("LITUSDT", 3.0)
        self.assertEqual(p.state, PositionState.BREAKEVEN)
        t.on_close("LITUSDT")
        self.assertEqual(p.state, PositionState.CLOSED)
        self.assertEqual(p.remaining_qty, 0.0)

    def test_last_tp_closes(self):
        t = PositionTracker()
        t.open("XUSDT", "long", 10.0, 1.0)
        t.on_tp_hit("XUSDT", 10.0)
        self.assertEqual(t.get("XUSDT").state, PositionState.CLOSED)


class TestOrderManager(unittest.IsolatedAsyncioTestCase):
    async def test_enter_tiers(self):
        om = OrderManager(NoopAdapter())
        result = await om.enter(make_signal(), 250.0)
        self.assertEqual(result["qty"], 250.0)
        self.assertEqual(len(result["tp_order_ids"]), 4)

    async def test_enter_tp_ratios_only_tp1_full_close(self):
        # 驗收 1：只填 TP1 → 只掛 1 檔 TP（全清）
        om = OrderManager(NoopAdapter())
        result = await om.enter(make_signal(), 250.0, tp_ratios=[100, 0, 0, 0])
        self.assertEqual(len(result["tp_order_ids"]), 1)

    async def test_enter_tp_ratios_two_tiers(self):
        # 驗收 2：填 TP1+TP2 → 2 檔，各 125
        adapter = StubAdapter()
        om = OrderManager(adapter)
        result = await om.enter(make_signal(), 250.0, tp_ratios=[50, 50, 0, 0])
        self.assertEqual(len(result["tp_order_ids"]), 2)
        tp_orders = [c for c in adapter.created if c["reduce_only"]]
        self.assertEqual([o["qty"] for o in tp_orders], [125.0, 125.0])

    async def test_enter_three_tier_signal_full_close_on_last(self):
        # 驗收 6：報單 3 檔 + 預設比例 → TP1/TP2 各 25%、TP3 全清 50%
        adapter = StubAdapter()
        om = OrderManager(adapter)
        sig = make_signal(tps=[3.1213, 3.1788, 3.2411])
        result = await om.enter(sig, 250.0, tp_ratios=[25, 25, 25, 25])
        self.assertEqual(len(result["tp_order_ids"]), 3)
        tp_orders = [c for c in adapter.created if c["reduce_only"]]
        self.assertEqual([o["qty"] for o in tp_orders], [62.5, 62.5, 125.0])

    async def test_enter_tier_below_min_merged(self):
        # 低於交易所最低手數的前檔併入末檔（既有手數規則）
        class MinMarketStub(StubAdapter):
            async def fetch_market(self, symbol):
                m = await super().fetch_market(symbol)
                m["limits"] = {"amount": {"min": 0.1}}
                return m

        adapter = MinMarketStub()
        om = OrderManager(adapter)
        await om.enter(make_signal(), 0.4, tp_ratios=[30, 30, 30, 10])
        tp_orders = [c for c in adapter.created if c["reduce_only"]]
        self.assertEqual(len(tp_orders), 3)  # 末檔 0.04 併入上一檔
        self.assertAlmostEqual(sum(o["qty"] for o in tp_orders), 0.4)


class TestApplyModify(unittest.IsolatedAsyncioTestCase):
    """✏️ 訂單修改：跟隨分析師逐項修改，只動變化項（規則 2026-11-04）。"""

    OLD_TPS = [0.03776, 0.039226, 0.041059, 0.045825]

    def _adapter(self, open_orders=None, ticker=0.037):
        return StubAdapter(
            position={"symbol": "STARUSDT", "side": "long", "qty": 4.0},
            open_orders=open_orders or [],
            ticker=ticker,
        )

    async def test_changed_tier_replaced(self):
        # 只改 TP4：撤舊 0.045825 掛單、按新價 0.0608 重掛，數量不變
        adapter = self._adapter(open_orders=[
            {"id": "tp4", "type": "limit", "reduce_only": True, "price": 0.045825, "qty": 1.0},
        ], ticker=0.037)
        om = OrderManager(adapter)
        fields = {"entry": 0.03666,
                  "tps": [(1, 0.03776), (2, 0.039226), (3, 0.041059), (4, 0.0608)],
                  "sl": 0.03556}
        await om.apply_modify("STARUSDT", "long", fields, self.OLD_TPS, 0.03556)
        self.assertEqual(adapter.cancelled_ids, ["tp4"])
        reorders = [c for c in adapter.created if c["reduce_only"]]
        self.assertEqual(len(reorders), 1)
        self.assertAlmostEqual(reorders[0]["price"], 0.0608)
        self.assertAlmostEqual(reorders[0]["qty"], 1.0)
        self.assertEqual(adapter.amended, [])  # SL 未變

    async def test_unchanged_tiers_untouched(self):
        adapter = self._adapter(open_orders=[
            {"id": "tp4", "type": "limit", "reduce_only": True, "price": 0.0608, "qty": 1.0},
        ], ticker=0.037)
        om = OrderManager(adapter)
        fields = {"tps": [(1, 0.03776), (2, 0.039226), (3, 0.041059), (4, 0.0608)],
                  "sl": 0.03556}
        await om.apply_modify("STARUSDT", "long", fields, self.OLD_TPS, 0.03556)
        self.assertEqual(adapter.cancelled_ids, [])
        self.assertEqual([c for c in adapter.created if c["reduce_only"]], [])

    async def test_invalid_new_price_skipped(self):
        # 新 TP4 低於現價（做多）→ 立即成交風險，跳過重掛並告警
        adapter = self._adapter(open_orders=[
            {"id": "tp4", "type": "limit", "reduce_only": True, "price": 0.045825, "qty": 1.0},
        ], ticker=0.05)
        om = OrderManager(adapter)
        fields = {"tps": [(4, 0.048)], "sl": 0.03556}
        await om.apply_modify("STARUSDT", "long", fields, self.OLD_TPS, 0.03556)
        self.assertEqual([c for c in adapter.created if c["reduce_only"]], [])

    async def test_sl_changed_amended(self):
        adapter = self._adapter(open_orders=[], ticker=0.037)
        om = OrderManager(adapter)
        fields = {"tps": [(1, 0.03776)], "sl": 0.03700}
        await om.apply_modify("STARUSDT", "long", fields, self.OLD_TPS, 0.03556)
        self.assertEqual(adapter.amended, [("STARUSDT", 0.03700)])


class TestCloseAllResidual(unittest.IsolatedAsyncioTestCase):
    """規則 2026-11-04：全平後殘留 reduce-only 掛單必須撤。"""

    async def test_no_position_but_residual_orders_cancel(self):
        adapter = StubAdapter(
            position=None,
            open_orders=[{"id": "tp1", "reduce_only": True}, {"id": "tp2", "reduce_only": True}],
        )
        om = OrderManager(adapter)
        closed = await om.close_all("LITUSDT")
        self.assertFalse(closed)  # 無持倉可平
        self.assertEqual(adapter.cancelled, ["LITUSDT"])  # 但殘留掛單被撤
        self.assertEqual(adapter.created, [])  # 沒有市價平倉動作

    async def test_position_closed_and_cancelled(self):
        adapter = StubAdapter(position={"symbol": "LITUSDT", "side": "long", "qty": 10.0})
        om = OrderManager(adapter)
        closed = await om.close_all("LITUSDT")
        self.assertTrue(closed)
        self.assertEqual(adapter.created[0]["side"], "sell")
        self.assertEqual(adapter.created[0]["qty"], 10.0)
        self.assertEqual(adapter.created[0]["reduce_only"], True)
        self.assertEqual(adapter.cancelled, ["LITUSDT"])

    async def test_terminal_cleanup_cancels_even_when_query_returns_nothing(self):
        # 全平信號 = 終態：查不到掛單也要發撤單指令，不能因查詢盲區靜默跳過
        # （條件單可能不在一般掛單查詢結果裡，見 GRASSUSDT 事故）
        adapter = StubAdapter(position=None, open_orders=[])
        om = OrderManager(adapter)
        closed = await om.close_all("LITUSDT")
        self.assertFalse(closed)  # 無持倉可平
        self.assertEqual(adapter.cancelled, ["LITUSDT"])
        self.assertEqual(adapter.created, [])


class TestWatchdog(unittest.IsolatedAsyncioTestCase):
    """看護輪詢（無狀態）：無持倉 + reduce-only 殘留 → 撤；部分止盈/進場掛單不動。"""

    def _setup(self, adapter):
        notifier = RecordingNotifier()
        return Watchdog(adapter, notifier), notifier

    async def test_full_close_residual_cancelled(self):
        adapter = StubAdapter(
            positions=[],
            open_orders=[{"id": "tp3", "symbol": "LITUSDT", "reduce_only": True}],
        )
        wd, notifier = self._setup(adapter)
        await wd.tick()
        self.assertEqual(adapter.cancelled, ["LITUSDT"])
        self.assertTrue(any("殘留掛單" in s for s in notifier.sent), notifier.sent)

    async def test_position_alive_keeps_orders(self):
        # 持倉還在（部分止盈狀態）：同 symbol 的 reduce-only 掛單不撤
        adapter = StubAdapter(
            positions=[{"symbol": "LITUSDT", "side": "long", "qty": 6.0}],
            open_orders=[{"id": "tp3", "symbol": "LITUSDT", "reduce_only": True}],
        )
        wd, _ = self._setup(adapter)
        await wd.tick()
        self.assertEqual(adapter.cancelled, [])

    async def test_pending_entry_kept(self):
        # 無持倉 + 只有非 reduce-only 掛單 = 進場限價單還在等，不撤
        adapter = StubAdapter(
            positions=[],
            open_orders=[{"id": "entry", "symbol": "LITUSDT", "reduce_only": False}],
        )
        wd, _ = self._setup(adapter)
        await wd.tick()
        self.assertEqual(adapter.cancelled, [])

    async def test_other_symbol_residual(self):
        # A 幣有持倉、B 幣殘留掛單：只撤 B
        adapter = StubAdapter(
            positions=[{"symbol": "AUSDT", "side": "long", "qty": 1.0}],
            open_orders=[
                {"id": "tp_a", "symbol": "AUSDT", "reduce_only": True},
                {"id": "tp_b", "symbol": "BUSDT", "reduce_only": True},
            ],
        )
        wd, _ = self._setup(adapter)
        await wd.tick()
        self.assertEqual(adapter.cancelled, ["BUSDT"])


class TestTriggerRouter(unittest.IsolatedAsyncioTestCase):
    async def test_signal_enter(self):
        notifier = RecordingNotifier()
        adapter = StubAdapter()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier)
        await router.on_signal(make_signal())
        self.assertTrue(any("已進場" in s for s in notifier.sent), notifier.sent)
        # 開倉單 + 4 檔 TP 單
        self.assertEqual(len(adapter.created), 5)
        self.assertEqual(adapter.created[0]["side"], "buy")
        self.assertEqual(adapter.created[0]["sl_price"], 2.962)

    async def test_signal_rejected_by_risk(self):
        notifier = RecordingNotifier()
        adapter = StubAdapter()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               analysts_cfg={"allow_all": False, "list": ["Other"]})
        await router.on_signal(make_signal())
        self.assertTrue(any("拒單" in s for s in notifier.sent), notifier.sent)
        self.assertEqual(adapter.created, [])

    async def test_reverse_signal_uses_exchange_positions(self):
        # 交易所已有 LIT 空倉：做多報單應被反向信號風控拒掉（無本地狀態）
        notifier = RecordingNotifier()
        adapter = StubAdapter(positions=[{"symbol": "LITUSDT", "side": "short", "qty": 1.0}])
        router = TriggerRouter(adapter, OrderManager(adapter), notifier)
        await router.on_signal(make_signal())
        self.assertTrue(any("反向" in s for s in notifier.sent), notifier.sent)
        self.assertEqual(adapter.created, [])

    async def test_sl_hit_event_cancels_residual(self):
        # ❌ 止損 通知 = 全平場景：無持倉但有殘留掛單 → 撤單
        notifier = RecordingNotifier()
        adapter = StubAdapter(
            position=None,
            open_orders=[{"id": "tp2", "reduce_only": True}],
        )
        router = TriggerRouter(adapter, OrderManager(adapter), notifier)
        await router.on_event("352", "NEARUSDT", "sl_hit", None, 4.91)
        self.assertEqual(adapter.cancelled, ["NEARUSDT"])
        self.assertTrue(any("全平" in s for s in notifier.sent), notifier.sent)

    async def test_order_multiplier_scales_qty(self):
        # 下單倍數 0.5：R 減半，手數 250 → 125
        notifier = RecordingNotifier()
        adapter = StubAdapter()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               risk_cfg={"order_multiplier": 0.5})
        await router.on_signal(make_signal())
        entry = adapter.created[0]
        self.assertAlmostEqual(entry["qty"], 125.0)

    async def test_qty_below_min_uses_min_times_tiers(self):
        # 交易所真實返回 cost.min=5U → 交易所最低≈1.644，系統最低=1.644×4檔
        class LowEquityStub(StubAdapter):
            async def fetch_equity(self):
                return 4.0  # 風控手數 = floor(4×2% ÷ 0.08) = 1 < 系統最低

            async def fetch_market(self, symbol):
                m = await super().fetch_market(symbol)
                m["limits"] = {"amount": {"min": None, "max": None},
                               "cost": {"min": 5.0}}
                return m

        notifier = RecordingNotifier()
        adapter = LowEquityStub()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier)
        await router.on_signal(make_signal())
        self.assertAlmostEqual(adapter.created[0]["qty"], 1.644 * 4, delta=0.01)

    async def test_qty_above_max_capped(self):
        # 手數高於最高手數 → 按最高手數下單
        class MaxLimitedStub(StubAdapter):
            async def fetch_market(self, symbol):
                m = await super().fetch_market(symbol)
                m["limits"] = {"amount": {"min": None, "max": 100.0}, "cost": {"min": 5.0}}
                return m

        notifier = RecordingNotifier()
        adapter = MaxLimitedStub()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier)
        await router.on_signal(make_signal())
        self.assertEqual(adapter.created[0]["qty"], 100.0)


    async def test_decimal_multiplier_and_qty_rounded_down_to_step(self):
        # 倍數可填任意小數：250 × 1.3333 = 333.325，交易所步長 0.1 → 向下取整 333.3
        class Step01Stub(StubAdapter):
            async def fetch_market(self, symbol):
                m = await super().fetch_market(symbol)
                m["precision"]["amount"] = 0.1
                return m

        notifier = RecordingNotifier()
        adapter = Step01Stub()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               risk_cfg={"order_multiplier": 1.3333})
        await router.on_signal(make_signal())
        self.assertAlmostEqual(adapter.created[0]["qty"], 333.3)
        for o in adapter.created:
            self.assertAlmostEqual(o["qty"] / 0.1, round(o["qty"] / 0.1))

    async def test_qty_never_rounds_up(self):
        # 250 × 1.002 = 250.5，步長 1 → 只能 250，不能四捨五入成 251
        class Step1Stub(StubAdapter):
            async def fetch_market(self, symbol):
                m = await super().fetch_market(symbol)
                m["precision"]["amount"] = 1.0
                return m

        notifier = RecordingNotifier()
        adapter = Step1Stub()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               risk_cfg={"order_multiplier": 1.002})
        await router.on_signal(make_signal())
        self.assertEqual(adapter.created[0]["qty"], 250.0)

    async def test_invalid_multiplier_skips_order(self):
        notifier = RecordingNotifier()
        adapter = StubAdapter()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               risk_cfg={"order_multiplier": "abc"})
        await router.on_signal(make_signal())
        self.assertEqual(adapter.created, [])
        self.assertTrue(any("設定錯誤" in s for s in notifier.sent), notifier.sent)

    async def test_tp_ratios_passed_to_order_manager(self):
        # risk_cfg 的 tp_ratios 生效：[100,0,0,0] → 只掛 1 檔 TP（全清）
        notifier = RecordingNotifier()
        adapter = StubAdapter()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               risk_cfg={"tp_ratios": [100, 0, 0, 0]})
        await router.on_signal(make_signal())
        tp_orders = [c for c in adapter.created if c["reduce_only"]]
        self.assertEqual(len(tp_orders), 1)
        self.assertAlmostEqual(tp_orders[0]["qty"], 250.0)

    async def test_invalid_tp_ratios_falls_back_to_equal(self):
        # 非法比例（加總 120）不拒單：回退均分 4 檔
        notifier = RecordingNotifier()
        adapter = StubAdapter()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               risk_cfg={"tp_ratios": [30, 30, 30, 30]})
        await router.on_signal(make_signal())
        tp_orders = [c for c in adapter.created if c["reduce_only"]]
        self.assertEqual(len(tp_orders), 4)
        self.assertTrue(any("已進場" in s for s in notifier.sent), notifier.sent)

    async def test_system_min_tiers_follow_last_nonzero_ratio(self):
        # 只填 TP1（[100,0,0,0]）→ 實際掛檔數 1 → 系統最低 = 0.001×1
        adapter = StubAdapter()
        self._market_with_min(adapter)
        router = TriggerRouter(adapter, OrderManager(adapter), RecordingNotifier(),
                               risk_cfg={"order_multiplier": 1.0,
                                         "tp_ratios": [100, 0, 0, 0]})
        with patch("autotrade.execution.trigger_router.validate") as m:
            m.return_value = SimpleNamespace(accepted=True, qty=0.00256, reason="")
            await router.on_signal(make_signal())
        self.assertEqual(adapter.created[0]["qty"], 0.002)


    def _market_with_min(self, adapter, min_amount=0.001):
        adapter.fetch_market = AsyncMock(return_value={
            "symbol": "XUSDT", "contractSize": 1.0,
            "precision": {"amount": 0.001, "price": 0.0001},
            "limits": {"amount": {"min": min_amount, "max": None}, "cost": {}},
            "active": True,
        })

    async def test_system_min_is_exchange_min_times_tp_tiers(self):
        # 使用者規則：系統最低手數 = 交易所最低手數 × 實際掛檔數 n
        # （n = min(報單檔數, 最後一個非 0 的比例檔位)，上限 4）
        for n, expected in ((2, 0.002), (4, 0.004), (5, 0.004)):
            with self.subTest(tiers=n):
                adapter = StubAdapter()
                self._market_with_min(adapter)
                router = TriggerRouter(adapter, OrderManager(adapter),
                                       RecordingNotifier(),
                                       risk_cfg={"order_multiplier": 1.0})
                with patch("autotrade.execution.trigger_router.validate") as m:
                    m.return_value = SimpleNamespace(accepted=True, qty=0.00256,
                                                     reason="")
                    await router.on_signal(make_signal(tps=[3.1] * n))
                self.assertEqual(adapter.created[0]["qty"], expected)

    async def test_case_0_00256_with_four_tiers_orders_0_004(self):
        # 使用者案例：交易所最低 0.001、算出 0.00256、4 檔 TP → 下 0.004
        adapter = StubAdapter()
        self._market_with_min(adapter)
        router = TriggerRouter(adapter, OrderManager(adapter), RecordingNotifier(),
                               risk_cfg={"order_multiplier": 1.0})
        with patch("autotrade.execution.trigger_router.validate") as m:
            m.return_value = SimpleNamespace(accepted=True, qty=0.00256, reason="")
            await router.on_signal(make_signal())
        self.assertEqual(adapter.created[0]["qty"], 0.004)
        # 4 檔 TP 每檔恰好等於交易所最低手數，都能出場
        self.assertEqual([o["qty"] for o in adapter.created[1:]],
                         [0.001, 0.001, 0.001, 0.001])

    async def test_no_fake_min_cost_when_exchange_omits_it(self):
        # 交易所沒回傳 cost.min/amount.min 時，不再憑空造 5U 下限
        adapter = StubAdapter()  # limits={}
        router = TriggerRouter(adapter, OrderManager(adapter), RecordingNotifier(),
                               risk_cfg={"order_multiplier": 1.0})
        with patch("autotrade.execution.trigger_router.validate") as m:
            m.return_value = SimpleNamespace(accepted=True, qty=0.00256, reason="")
            await router.on_signal(make_signal())
        self.assertEqual(adapter.created[0]["qty"], 0.002)

    async def test_zero_qty_falls_back_to_system_min(self):
        # 風控算出 0 手時不拒單，按系統最低手數（0.001 × 4檔 = 0.004）託底
        adapter = StubAdapter()
        self._market_with_min(adapter)
        router = TriggerRouter(adapter, OrderManager(adapter), RecordingNotifier(),
                               risk_cfg={"order_multiplier": 0.01})
        sig = make_signal(entry=100.0, sl=50.0)  # 1000×2%/50 = 0.4 → floor 0
        await router.on_signal(sig)
        self.assertEqual(adapter.created[0]["qty"], 0.004)
        self.assertEqual([o["qty"] for o in adapter.created[1:]],
                         [0.001, 0.001, 0.001, 0.001])

    async def test_zero_qty_still_orders_when_exchange_gives_no_min(self):
        # 交易所連 amount.min / cost.min 都沒給時，算到 0 手也不能整單跳過：
        # 以交易所的手數精度步長當最低手數，按實際掛檔數 x4 下單
        adapter = StubAdapter()  # fetch_market 的 limits={}
        notifier = RecordingNotifier()
        router = TriggerRouter(adapter, OrderManager(adapter), notifier,
                               risk_cfg={"order_multiplier": 0.01})
        sig = make_signal(entry=100.0, sl=50.0)
        await router.on_signal(sig)
        self.assertTrue(adapter.created, notifier.sent)
        self.assertEqual(adapter.created[0]["qty"], 0.004)
        self.assertEqual([o["qty"] for o in adapter.created[1:]],
                         [0.001, 0.001, 0.001, 0.001])
        self.assertFalse(any("跳過" in s for s in notifier.sent), notifier.sent)


if __name__ == "__main__":
    unittest.main()
