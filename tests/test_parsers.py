"""解析器单测：7 条真实报单 + 2 条紧凑通知 + 各操作記錄行，全字段断言（M1 验收）。"""
import unittest
from pathlib import Path

from autotrade.parser.entry_parser import EntryParser, Signal
from autotrade.parser.notice_parser import NoticeParser
from autotrade.parser.op_parser import OpParser, op_line_hash

FIXTURES = Path(__file__).parent / "fixtures" / "messages"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestEntryParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = EntryParser()

    def _check(self, name, order_id, symbol, side, entry, margin, sl, tps,
               risk_pct, stop_dist_pct, analyst, leverage=10, is_limit=False):
        s = self.parser.parse(load(name))
        self.assertIsNotNone(s, f"{name} 解析失败")
        self.assertEqual(s.order_id, order_id)
        self.assertEqual(s.symbol, symbol)
        self.assertEqual(s.side, side)
        self.assertEqual(s.entry, entry)
        self.assertEqual(s.is_limit, is_limit)
        self.assertEqual(s.leverage, leverage)
        self.assertEqual(s.margin_pct, margin)
        self.assertEqual(s.sl, sl)
        self.assertEqual(s.tps, tps)
        self.assertEqual(s.risk_pct, risk_pct)
        self.assertEqual(s.stop_dist_pct, stop_dist_pct)
        self.assertEqual(s.analyst, analyst)
        self.assertEqual(self.parser.validate(s), [], f"{name} 语义校验应通过")

    def test_order216(self):
        self._check("order_216_lit_long.txt", "216", "LITUSDT", "long", 3.042,
                    7.61, 2.962, [3.1213, 3.1788, 3.2411, 3.2856], 2.00, 2.63, "Lazycat")

    def test_order213(self):
        self._check("order_213_atom_long.txt", "213", "ATOMUSDT", "long", 1.614,
                    15.00, 1.601, [1.6279, 1.6372, 1.65, 1.666], 1.21, 0.81, "Lazycat")

    def test_order196(self):
        self._check("order_196_c98_short.txt", "196", "C98USDT", "short", 0.0185,
                    10.28, 0.01886, [0.01812, 0.017575, 0.016659, 0.014808],
                    2.00, 1.95, "Trader X")

    def test_order359(self):
        self._check("order_359_dusk_long.txt", "359", "DUSKUSDT", "long", 0.10819,
                    15.00, 0.106761, [0.109639, 0.112518, 0.115763, 0.120279],
                    1.98, 1.32, "Trader X")

    def test_order353(self):
        self._check("order_353_aster_long.txt", "353", "ASTERUSDT", "long", 0.7029,
                    5.56, 0.6776, [0.7269, 0.7384, 0.7573, 0.78], 2.00, 3.60, "Lazycat")

    def test_order188(self):
        self._check("order_188_giggle_long.txt", "188", "GIGGLEUSDT", "long", 32.31,
                    7.89, 31.4909, [33.1393, 33.936, 34.9056, 36.5662], 2.00, 2.54, "Trader X")

    def test_order187(self):
        self._check("order_187_syn_short.txt", "187", "SYNUSDT", "short", 0.1299,
                    4.18, 0.13611, [0.12349, 0.11682, 0.11033, 0.095219],
                    2.00, 4.78, "Trader X")

    def test_notice_message_not_entry(self):
        self.assertIsNone(self.parser.parse(load("notice_187_close.txt")))

    def test_thousands_separator(self):
        # 订单 180 SNDK：进场/TP/SL 带千分位逗号（2026-11-04 真实样例）
        text = (
            "📌 訂單 180｜SNDKUSDT｜📈做多\n"
            "━━━━━━━━━━━━\n"
            "進場：1,358.97 現價\n"
            "槓桿：10x\n"
            "保證金：≤ 9.90% 總本金\n"
            "TP1：1,410.5\n"
            "TP2：1,450.25\n"
            "TP3：1,490.1\n"
            "TP4：1,560.8\n"
            "SL：1,320.44\n"
            "\n"
            "風控：止損距離 2.84% → 控倉維持 2.00% 風控\n"
            "\n"
            "分析師：Trader X\n"
        )
        s = self.parser.parse(text)
        self.assertIsNotNone(s)
        self.assertEqual(s.entry, 1358.97)
        self.assertEqual(s.sl, 1320.44)
        self.assertEqual(s.tps[0], 1410.5)
        self.assertEqual(s.tps[3], 1560.8)

    def test_validate_catches_bad_sl(self):
        s = Signal(order_id="1", symbol="XUSDT", side="long", entry=1.0,
                   is_limit=False, leverage=10, sl=1.5, tps=[1.2],
                   risk_pct=2.0, stop_dist_pct=2.0)
        self.assertIn("做多但 SL ≥ 進場價", self.parser.validate(s))


class TestNoticeParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = NoticeParser()

    def _check(self, text, expected):
        e = self.parser.parse(text)
        self.assertIsNotNone(e, f"通知解析失败：{text[:40]}")
        self.assertEqual((e.order_id, e.symbol, e.side, e.action_type, e.tp_index, e.price),
                         expected)

    def test_187_close(self):
        self._check(load("notice_187_close.txt"), ("187", "SYNUSDT", "short", "close", None, None))

    def test_188_close(self):
        self._check(load("notice_188_close.txt"), ("188", "GIGGLEUSDT", "long", "close", None, None))

    def test_close_with_analyst_comment(self):
        # 用户样例（2026-11-04）：已平倉后可跟分析师看法文字，且无 📌 前缀
        self._check("訂單 180 SNDKUSDT 做多 🟥 已平倉\n感觉太弱了还是先平仓吧",
                    ("180", "SNDKUSDT", "long", "close", None, None))

    def test_hedge_compact(self):
        # 用户样例（2026-11-04）：套保指令 → 跟着 amend SL
        self._check("訂單 353 ASTERUSDT 做多 🔒 套保（SL → 進場價 0.7029）",
                    ("353", "ASTERUSDT", "long", "hedge", None, 0.7029))

    def test_tp_hit_compact(self):
        # 真实采集样例（2026-11-04）：TP 达成纯信息
        self._check("📌訂單 217 XMRUSDT 做多 🎯 TP2 達成 ➜ 612.4477",
                    ("217", "XMRUSDT", "long", "tp_hit", 2, 612.4477))

    def test_all_tp_compact(self):
        self._check("📌訂單 213 ATOMUSDT 做多 ✅ 全部止盈",
                    ("213", "ATOMUSDT", "long", "all_tp", None, None))

    def test_sl_hit_compact(self):
        # 用户样例（2026-11-04）：止损触发通知 → 全平场景，撤残留挂单
        self._check("📌訂單 352 NEARUSDT 做多 ❌ 止損 ➜ 4.91",
                    ("352", "NEARUSDT", "long", "sl_hit", None, 4.91))

    def test_modify_compact(self):
        # 用户样例（2026-11-04）：訂單修改指令 → 跟随逐项修改
        text = ("📌訂單 339 STARUSDT 做多 ✏️ 訂單修改 (2026-10-05 12:00:21) · "
                "進場 0.03666 · TP1 0.03776 · TP2 0.039226 · TP3 0.041059 · "
                "TP4 0.0608 · 止損 0.03556")
        e = self.parser.parse(text)
        self.assertIsNotNone(e)
        self.assertEqual((e.order_id, e.symbol, e.side, e.action_type),
                         ("339", "STARUSDT", "long", "modify"))
        self.assertEqual(e.ts_text, "2026-10-05 12:00:21")
        self.assertEqual(e.mod_fields["entry"], 0.03666)
        self.assertEqual(e.mod_fields["sl"], 0.03556)
        self.assertEqual(e.mod_fields["tps"],
                         [(1, 0.03776), (2, 0.039226), (3, 0.041059), (4, 0.0608)])

    def test_entry_message_not_notice(self):
        self.assertIsNone(self.parser.parse(load("order_216_lit_long.txt")))


class TestOpParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = OpParser()

    def _check(self, name, expected):
        ops = self.parser.extract_ops(load(name))
        got = [(o.action_type, o.tp_index, o.price) for o in ops]
        self.assertEqual(got, expected, name)

    def test_187(self):
        self._check("order_187_syn_short.txt", [
            ("tp_hit", 1, 0.12349), ("tp_hit", 2, 0.11682), ("tp_hit", 3, 0.11033),
            ("close", None, None), ("ignore", None, None),
        ])

    def test_216(self):
        self._check("order_216_lit_long.txt", [("tp_hit", 1, 3.1213)])

    def test_188(self):
        self._check("order_188_giggle_long.txt", [("close", None, None)])

    def test_196(self):
        self._check("order_196_c98_short.txt", [
            ("tp_hit", 1, 0.01812), ("move_sl", None, 0.0185), ("hedge_exit", None, 0.0185),
        ])

    def test_213(self):
        self._check("order_213_atom_long.txt", [
            ("tp_hit", 1, 1.6279), ("tp_hit", 2, 1.6372), ("tp_hit", 3, 1.65),
            ("tp_hit", 4, 1.666), ("all_tp", None, None),
        ])

    def test_353(self):
        self._check("order_353_aster_long.txt", [
            ("tp_hit", 1, 0.7269), ("hedge", None, 0.7029), ("hedge_exit", None, 0.7029),
        ])

    def test_359(self):
        self._check("order_359_dusk_long.txt", [
            ("tp_hit", 1, 0.109639), ("tp_hit", 2, 0.112518), ("tp_hit", 3, 0.115763),
        ])

    def test_sl_hit_op_line(self):
        # 编辑通道的操作記錄行同样识别止损（与通知通道幂等去重）
        ops = self.parser.extract_ops(
            "━━━ 操作記錄 ━━━\n  → ❌ 止損 ➜ 4.91  (2026-11-02 03:00:00)"
        )
        self.assertEqual(ops[0].action_type, "sl_hit")
        self.assertEqual(ops[0].price, 4.91)

    def test_modify_block(self):
        # 用户样例（2026-11-04）：编辑通道的多行修改块，后续行无 → 前缀
        text = (
            "━━━ 操作記錄 ━━━\n"
            "  → 🎯 TP3 達成 ➜ 0.041059  (2026-10-05 04:15:44)\n"
            "  → ✏️ 訂單修改 (2026-10-05 12:00:21)\n"
            "進場 0.03666\n"
            "TP1 0.03776\n"
            "TP2 0.039226\n"
            "TP3 0.041059\n"
            "TP4 0.0608\n"
            "止損 0.03556  (2026-10-05 12:00:21)\n"
            "  → 🟥 已平倉"
        )
        ops = self.parser.extract_ops(text)
        got = [(o.action_type, o.tp_index, o.price) for o in ops]
        self.assertEqual(got, [("tp_hit", 3, 0.041059), ("modify", None, None),
                               ("close", None, None)])
        modify_op = ops[1]
        self.assertEqual(modify_op.ts_text, "2026-10-05 12:00:21")
        self.assertEqual(modify_op.mod_fields["sl"], 0.03556)
        self.assertEqual(modify_op.mod_fields["tps"],
                         [(1, 0.03776), (2, 0.039226), (3, 0.041059), (4, 0.0608)])

    def test_modify_hash_cross_channel(self):
        from autotrade.parser.op_parser import modify_hash, parse_modify_fields
        compact = ("📌訂單 339 STARUSDT 做多 ✏️ 訂單修改 (2026-10-05 12:00:21) · "
                   "進場 0.03666 · TP1 0.03776 · TP2 0.039226 · TP3 0.041059 · "
                   "TP4 0.0608 · 止損 0.03556")
        edit = ("━━━ 操作記錄 ━━━\n  → ✏️ 訂單修改 (2026-10-05 12:00:21)\n"
                "進場 0.03666\nTP1 0.03776\nTP2 0.039226\nTP3 0.041059\n"
                "TP4 0.0608\n止損 0.03556")
        h1 = modify_hash(parse_modify_fields(compact), "2026-10-05 12:00:21")
        h2 = modify_hash(parse_modify_fields(edit), "2026-10-05 12:00:21")
        self.assertEqual(h1, h2)

    def test_ts_extracted(self):
        ops = self.parser.extract_ops(load("order_216_lit_long.txt"))
        self.assertEqual(ops[0].ts_text, "2026-10-28 01:43:44")

    def test_no_ops_section(self):
        self.assertEqual(self.parser.extract_ops("随便一段文字"), [])

    def test_unknown_line(self):
        text = "━━━ 操作記錄 ━━━\n  → 🆕 某种新操作  (2026-11-01 00:00:00)"
        ops = self.parser.extract_ops(text)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].action_type, "unknown")
        self.assertEqual(ops[0].ts_text, "2026-11-01 00:00:00")

    def test_plain_close_word_not_treated_as_terminal_close(self):
        # 「減倉 50%（剩餘 50%）」这類講解若寫成「平倉」二字，也不可觸發全平
        text = ("━━━ 操作記錄 ━━━\n"
                "  → 🎯 TP1 達成 ➜ 0.01352\n"
                "  → 平倉 50%（剩餘 50%）")
        ops = self.parser.extract_ops(text)
        self.assertNotIn("close", [o.action_type for o in ops])
        self.assertEqual([o.action_type for o in ops],
                         ["tp_hit", "unknown"])

    def test_explicit_red_close_marker_still_terminal_close(self):
        # 明確的「🟥 已平倉」仍須觸發全平（不可連這條一起拿掉）
        ops = self.parser.extract_ops(
            "━━━ 操作記錄 ━━━\n  → 🟥 已平倉  (2026-11-01 00:00:00)"
        )
        self.assertEqual([o.action_type for o in ops], ["close"])

    def test_hash_semantic_normalization(self):
        # 同一操作跨通道归一：编辑行与通知消息的 close 事件同键
        self.assertEqual(op_line_hash("close", None, None),
                         op_line_hash("close", None, None))
        self.assertNotEqual(op_line_hash("close", None, None),
                            op_line_hash("tp_hit", 1, 0.12349))
        self.assertNotEqual(op_line_hash("tp_hit", 1, 0.12349),
                            op_line_hash("tp_hit", 2, 0.12349))


if __name__ == "__main__":
    unittest.main()
