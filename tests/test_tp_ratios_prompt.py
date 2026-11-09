"""止盈部位比例提示單測：讀寫回退、格式化、輸入流程（空回車=0、全空保留、ESC 取消、
加總判定）、prompt 按鍵分支與非交互跳過。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autotrade.tp_ratios_prompt import (
    DEFAULT,
    format_tp_ratios,
    prompt_tp_ratios,
    read_tp_ratios,
    read_tp_ratios_input,
    save_tp_ratios,
)

CFG = """\
# 頂部註釋
risk:
  margin_formula_tol_pct: 0.1
  tp_ratios: [25, 25, 25, 25]      # 保留這行註釋
exchanges:
  active: bybit
"""


class _CfgMixin:
    def _cfg(self):
        d = tempfile.TemporaryDirectory()
        p = Path(d.name) / "config.yaml"
        p.write_text(CFG, encoding="utf-8")
        return d, p


class TestReadSave(_CfgMixin, unittest.TestCase):
    def test_default_when_config_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(read_tp_ratios(Path(d) / "nope.yaml"), list(DEFAULT))

    def test_read_existing(self):
        d, p = self._cfg()
        try:
            self.assertEqual(read_tp_ratios(p), [25.0, 25.0, 25.0, 25.0])
        finally:
            d.cleanup()

    def test_equal_value_falls_back_to_default(self):
        d, p = self._cfg()
        try:
            p.write_text("risk:\n  tp_ratios: equal\n", encoding="utf-8")
            self.assertEqual(read_tp_ratios(p), list(DEFAULT))
        finally:
            d.cleanup()

    def test_invalid_value_falls_back_to_default(self):
        d, p = self._cfg()
        try:
            p.write_text("risk:\n  tp_ratios: [30, 30, 30, 30]\n", encoding="utf-8")
            self.assertEqual(read_tp_ratios(p), list(DEFAULT))
        finally:
            d.cleanup()

    def test_save_preserves_comments_and_other_fields(self):
        d, p = self._cfg()
        try:
            save_tp_ratios(p, [30.0, 70.0, 0.0, 0.0])
            text = p.read_text(encoding="utf-8")
            self.assertIn("# 頂部註釋", text)
            self.assertIn("# 保留這行註釋", text)
            self.assertIn("margin_formula_tol_pct: 0.1", text)
            self.assertIn("active: bybit", text)
            self.assertEqual(read_tp_ratios(p), [30.0, 70.0, 0.0, 0.0])
        finally:
            d.cleanup()


class TestFormat(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_tp_ratios([25.0, 25.0, 25.0, 25.0]),
                         "25% / 25% / 25% / 25%")
        self.assertEqual(format_tp_ratios([30.0, 70.0, 0.0, 0.0]),
                         "30% / 70% / 0% / 0%")
        self.assertEqual(format_tp_ratios([33.3, 33.3, 33.4, 0.0]),
                         "33.3% / 33.3% / 33.4% / 0%")


class TestReadInput(unittest.TestCase):
    def _run(self, values, current=None):
        current = current or list(DEFAULT)
        with mock.patch("builtins.print"), \
             mock.patch("autotrade.tp_ratios_prompt._read_text",
                        side_effect=values):
            return read_tp_ratios_input(current)

    def test_all_empty_keeps_current(self):
        # 4 檔全空回車 = 等同取消修改，保留原值
        self.assertEqual(self._run(["", "", "", ""]), list(DEFAULT))

    def test_first_tier_empty_allowed(self):
        # 第一檔空回車 = TP1 為 0（允許），繼續問後續檔
        self.assertEqual(self._run(["", "30", "70", ""]),
                         [0.0, 30.0, 70.0, 0.0])

    def test_partial_fill_with_valid_sum(self):
        # 填了任意數字即啟動修改；未填視為 0
        self.assertEqual(self._run(["30", "70", "", ""]),
                         [30.0, 70.0, 0.0, 0.0])

    def test_sum_mismatch_reprompts(self):
        # 10+20+30+30=90 ≠ 100 → 重填 → 40/20/20/20 通過
        self.assertEqual(self._run(["10", "20", "30", "30",
                                    "40", "20", "20", "20"]),
                         [40.0, 20.0, 20.0, 20.0])

    def test_non_numeric_reprompts(self):
        # 非法字符 → 重填 → 有效值通過
        self.assertEqual(self._run(["abc", "25", "25", "25",
                                    "25", "25", "25", "25"]),
                         [25.0, 25.0, 25.0, 25.0])

    def test_esc_cancels(self):
        # 任一檔 ESC → None（取消修改）
        self.assertIsNone(self._run(["30", None]))

    def test_single_tier_full_close(self):
        # 只填 TP1=100，其餘空回車 → 保存 [100,0,0,0]
        self.assertEqual(self._run(["100", "", "", ""]),
                         [100.0, 0.0, 0.0, 0.0])


class TestPrompt(_CfgMixin, unittest.TestCase):
    def _run(self, p, keys, values=None):
        with mock.patch("builtins.print"), \
             mock.patch("autotrade.tp_ratios_prompt._is_interactive", return_value=True), \
             mock.patch("autotrade.tp_ratios_prompt._read_key", side_effect=keys), \
             mock.patch("autotrade.tp_ratios_prompt._read_text",
                        side_effect=values or []):
            return prompt_tp_ratios(p)

    def test_enter_keeps_current(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"\r"]), [25.0, 25.0, 25.0, 25.0])
            self.assertIn("tp_ratios: [25, 25, 25, 25]",
                          p.read_text(encoding="utf-8"))
        finally:
            d.cleanup()

    def test_y_keeps_current(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"Y"]), [25.0, 25.0, 25.0, 25.0])
        finally:
            d.cleanup()

    def test_n_saves_new_value(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"n"], ["30", "70", "", ""]),
                             [30.0, 70.0, 0.0, 0.0])
            self.assertEqual(read_tp_ratios(p), [30.0, 70.0, 0.0, 0.0])
            self.assertIn("# 保留這行註釋", p.read_text(encoding="utf-8"))
        finally:
            d.cleanup()

    def test_esc_enters_input_then_cancels_keeps_current(self):
        # N/ESC 進輸入 → 輸入中 ESC → 取消，沿用原值
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"\x1b"], ["30", None]),
                             [25.0, 25.0, 25.0, 25.0])
        finally:
            d.cleanup()

    def test_unknown_key_reprompts(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"x", b"\r"]),
                             [25.0, 25.0, 25.0, 25.0])
        finally:
            d.cleanup()

    def test_non_interactive_skips_prompt(self):
        d, p = self._cfg()
        try:
            with mock.patch("builtins.print"), \
                 mock.patch("autotrade.tp_ratios_prompt._is_interactive", return_value=False), \
                 mock.patch("autotrade.tp_ratios_prompt._read_key") as key:
                self.assertEqual(prompt_tp_ratios(p), [25.0, 25.0, 25.0, 25.0])
                key.assert_not_called()
        finally:
            d.cleanup()

    def test_next_startup_shows_last_value(self):
        d, p = self._cfg()
        try:
            self._run(p, [b"n"], ["30", "70", "", ""])
            # 下次啟動按回車，預設值應是上次填的 30/70/0/0
            self.assertEqual(self._run(p, [b"\r"]), [30.0, 70.0, 0.0, 0.0])
        finally:
            d.cleanup()


if __name__ == "__main__":
    unittest.main()
