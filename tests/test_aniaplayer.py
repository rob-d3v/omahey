# SPDX-License-Identifier: Apache-2.0
"""Ania Player coexistence, the starter config and what status reports. Stdlib only."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omahey import aniaplayer as A
from omahey import cli
from omahey import models as M
from omahey import wakeconf as W

HAVE = {"ok_ania": Path("/x/ok_ania"), "hey_jarvis": Path("/x/hey_jarvis")}


def player(tmp: Path, wake: dict, install_path: bool = False) -> Path:
    va = tmp / ("custom" if install_path else "plugins/voice_assistant")
    va.mkdir(parents=True, exist_ok=True)
    (va / "voice_config.json").write_text(json.dumps({"wake_word": wake}))
    if install_path:
        (tmp / "plugins_state.json").write_text(json.dumps({"voice_assistant": {"install_path": str(va)}}))
    return tmp


class PlayerConfigTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_enabled_primary_and_extras(self):
        base = player(self.tmp, {"wake_word_enabled": True, "model_name": "ok_ania",
                                 "extra_wake_words": ["ok_niva"]})
        self.assertEqual(A.configured_words(base), {"ok_ania", "ok_niva"})

    def test_legacy_and_mode_ignores_extras(self):
        base = player(self.tmp, {"wake_word_enabled": True, "model_name": "ok_ania",
                                 "model_name_2": "hey_brother", "extra_wake_words": ["ok_niva"]})
        self.assertEqual(A.configured_words(base), {"ok_ania", "hey_brother"})

    def test_disabled_or_keyboard_only(self):
        base = player(self.tmp, {"wake_word_enabled": False, "model_name": "ok_ania"})
        self.assertEqual(A.configured_words(base), set())
        base = player(self.tmp, {"mode": "keyboard", "model_name": "ok_ania"})
        self.assertEqual(A.configured_words(base), set())

    def test_old_mode_key_and_default_model(self):
        base = player(self.tmp, {"mode": "both"})
        self.assertEqual(A.configured_words(base), {"hey_brother"})

    def test_install_path_from_plugins_state(self):
        base = player(self.tmp, {"wake_word_enabled": True, "model_name": "ok_ania"}, install_path=True)
        self.assertEqual(A.configured_words(base), {"ok_ania"})

    def test_no_player(self):
        self.assertEqual(A.configured_words(self.tmp / "nope"), set())

    def test_words_in_use_needs_running_player_and_respects_opt_out(self):
        base = player(self.tmp, {"wake_word_enabled": True, "model_name": "ok_ania"})
        self.assertEqual(A.words_in_use({}, base, lambda: True), {"ok_ania"})
        self.assertEqual(A.words_in_use({}, base, lambda: False), set())
        self.assertEqual(A.words_in_use({"skip_ania_player_words": False}, base, lambda: True), set())


class EffectiveWordsTests(unittest.TestCase):
    def run_eff(self, wc, skip=(), have=HAVE, cfg=None):
        cfg = cfg or {"wake_word": "hey_omarchy"}
        with mock.patch.object(M, "installed", return_value=dict(have)), \
                mock.patch.object(M, "listing", return_value=[]):
            return M.effective_words(cfg, wc, skip)

    def test_legacy_fallback_reports_real_and_configured(self):
        wc, words = self.run_eff(W.legacy({"wake_word": "hey_omarchy"}))
        self.assertEqual(words[0]["model"], "hey_jarvis")      # sorted: hey_jarvis < ok_ania
        self.assertEqual(words[0]["configured"], "hey_omarchy")
        self.assertTrue(words[0]["active"])

    def test_legacy_fallback_skips_player_word(self):
        wc, words = self.run_eff(W.legacy({}), skip={"hey_jarvis"})
        self.assertEqual(words[0]["model"], "ok_ania")

    def test_legacy_configured_player_word_moves_to_another(self):
        cfg = {"wake_word": "ok_ania"}
        wc, words = self.run_eff(W.legacy(cfg), skip={"ok_ania"}, cfg=cfg)
        self.assertEqual((words[0]["model"], words[0]["configured"]), ("hey_jarvis", "ok_ania"))

    def test_only_word_is_players(self):
        cfg = {"wake_word": "ok_ania"}
        wc, words = self.run_eff(W.legacy(cfg), skip={"ok_ania"}, have={"ok_ania": Path("/x")}, cfg=cfg)
        self.assertEqual(words[0]["model"], "ok_ania")
        self.assertFalse(words[0]["active"])
        self.assertTrue(words[0]["ania_player"])

    def test_toml_word_used_by_player_is_inactive(self):
        wc = W.parse('[[wake]]\nmodel = "ok_ania"\n[[wake]]\nmodel = "hey_jarvis"\n')
        _, words = self.run_eff(wc, skip={"ok_ania"})
        self.assertEqual([(w["model"], w["active"], w["ania_player"]) for w in words],
                         [("ok_ania", False, True), ("hey_jarvis", True, False)])


class StarterConfigTests(unittest.TestCase):
    def test_prefers_hey_omarchy_when_installed(self):
        self.assertEqual(W.starter_model({"hey_omarchy": 1, "ok_ania": 1}), ("hey_omarchy", True))

    def test_installed_word_not_used_by_player(self):
        self.assertEqual(W.starter_model(HAVE, set(), ["hey_omarchy", "hey_jarvis"]), ("hey_jarvis", True))
        self.assertEqual(W.starter_model(HAVE, {"hey_jarvis"}), ("ok_ania", True))

    def test_nothing_usable_falls_back_to_downloadable(self):
        self.assertEqual(W.starter_model({"ok_ania": 1}, {"ok_ania"}), ("hey_jarvis", False))
        self.assertEqual(W.starter_model({}), ("hey_jarvis", False))

    def test_template_is_valid_and_hey_omarchy_commented(self):
        for model, inst in (("ok_ania", True), ("hey_jarvis", False)):
            text = W.render_template(model, inst)
            wc = W.parse(text, have_program=lambda p: True)
            self.assertEqual(wc.errors, [])
            self.assertEqual([s.model for s in wc.specs], [model])
            self.assertIn('# model = "hey_omarchy"', text)
            self.assertNotIn("{model", text)
            self.assertNotIn("{example}", text)
        self.assertIn("not installed yet", W.render_template("hey_jarvis", False))

    def test_write_template(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            self.assertTrue(W.write_template(p, "ok_ania", True))
            self.assertIn('model = "ok_ania"', p.read_text())
            self.assertFalse(W.write_template(p, "hey_jarvis", False))


class StatusTests(unittest.TestCase):
    def test_offline_status_shows_loaded_word_not_configured(self):
        ready = {"voxtype": True, "venv": True, "model": "ok_ania", "service": True}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(cli, "request", return_value=None), \
                mock.patch("omahey.setup.readiness", return_value=ready), \
                mock.patch.object(cli.C, "load_config", return_value={
                    "mode": "ask", "sensitivity": "normal", "wake_word": "hey_omarchy",
                    "conversation": True}), \
                mock.patch.object(W, "TOML_FILE", Path(d) / "none.toml"), \
                mock.patch.object(A, "words_in_use", return_value=set()), \
                mock.patch.object(M, "installed", return_value={"ok_ania": Path("/x")}), \
                mock.patch.object(M, "listing", return_value=[]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.cmd_status(mock.Mock(json=True))
            out = json.loads(buf.getvalue())
            self.assertEqual(out["wake_word"], "ok_ania")
            self.assertEqual(out["configured_wake_word"], "hey_omarchy")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.cmd_status(mock.Mock(json=False))
            self.assertIn("wake word ok_ania (configured hey_omarchy", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
