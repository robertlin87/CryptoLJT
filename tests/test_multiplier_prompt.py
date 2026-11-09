"""啟動詢問風險乘數單測：預設值記憶、按鍵分支、設定寫回保留註釋。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autotrade.multiplier_prompt import (DEFAULT, prompt_multiplier,
                                         read_multiplier, save_multiplier)

CFG = """\
# 頂部註釋
risk:
  margin_formula_tol_pct: 0.1
  order_multiplier: 1.0      # 保留這行註釋
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
            self.assertEqual(read_multiplier(Path(d) / "nope.yaml"), DEFAULT)

    def test_read_existing(self):
        d, p = self._cfg()
        try:
            self.assertEqual(read_multiplier(p), 1.0)
        finally:
            d.cleanup()

    def test_save_preserves_comments_and_other_fields(self):
        d, p = self._cfg()
        try:
            save_multiplier(p, 0.2)
            text = p.read_text(encoding="utf-8")
            self.assertIn("# 頂部註釋", text)
            self.assertIn("# 保留這行註釋", text)
            self.assertIn("margin_formula_tol_pct: 0.1", text)
            self.assertIn("active: bybit", text)
            self.assertEqual(read_multiplier(p), 0.2)
        finally:
            d.cleanup()

    def test_invalid_existing_value_falls_back_to_default(self):
        d, p = self._cfg()
        try:
            p.write_text("risk:\n  order_multiplier: abc\n", encoding="utf-8")
            self.assertEqual(read_multiplier(p), DEFAULT)
        finally:
            d.cleanup()


class TestPrompt(_CfgMixin, unittest.TestCase):
    def _run(self, p, keys, lines=None):
        with mock.patch("builtins.print"), \
             mock.patch("autotrade.multiplier_prompt._is_interactive", return_value=True), \
             mock.patch("autotrade.multiplier_prompt._read_key", side_effect=keys), \
             mock.patch("autotrade.multiplier_prompt._read_line",
                        side_effect=lines or []):
            return prompt_multiplier(p)

    def test_enter_keeps_current(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"\r"]), 1.0)
            self.assertIn("order_multiplier: 1.0", p.read_text(encoding="utf-8"))
        finally:
            d.cleanup()

    def test_y_keeps_current(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"Y"]), 1.0)
        finally:
            d.cleanup()

    def test_n_saves_new_value(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"n"], ["0.2"]), 0.2)
            self.assertEqual(read_multiplier(p), 0.2)
            self.assertIn("# 保留這行註釋", p.read_text(encoding="utf-8"))
        finally:
            d.cleanup()

    def test_esc_saves_new_value(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"\x1b"], ["1.5"]), 1.5)
            self.assertEqual(read_multiplier(p), 1.5)
        finally:
            d.cleanup()

    def test_invalid_input_reprompts(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"N"], ["abc", "0.3"]), 0.3)
            self.assertEqual(read_multiplier(p), 0.3)
        finally:
            d.cleanup()

    def test_unknown_key_reprompts(self):
        d, p = self._cfg()
        try:
            self.assertEqual(self._run(p, [b"x", b"\r"]), 1.0)
        finally:
            d.cleanup()

    def test_non_interactive_skips_prompt(self):
        d, p = self._cfg()
        try:
            with mock.patch("builtins.print"), \
                 mock.patch("autotrade.multiplier_prompt._is_interactive", return_value=False), \
                 mock.patch("autotrade.multiplier_prompt._read_key") as key:
                self.assertEqual(prompt_multiplier(p), 1.0)
                key.assert_not_called()
        finally:
            d.cleanup()

    def test_next_startup_shows_last_value(self):
        d, p = self._cfg()
        try:
            self._run(p, [b"n"], ["0.2"])
            # 下次啟動按回車，預設值應是上次填的 0.2
            self.assertEqual(self._run(p, [b"\r"]), 0.2)
        finally:
            d.cleanup()


if __name__ == "__main__":
    unittest.main()
