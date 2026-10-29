# SPDX-License-Identifier: Apache-2.0
"""Daemon: words a running Ania Player uses are skipped and explained once. Needs numpy."""
import unittest
from pathlib import Path
from unittest import mock

try:
    from omawake import daemon as Dm
except ImportError:                       # numpy / onnxruntime missing
    Dm = None

CFG = {"mode": "ask", "sensitivity": "normal", "wake_word": "ok_ania", "end_silence_ms": 1100,
       "audio_target": "", "conversation": True}


@unittest.skipIf(Dm is None, "needs the runtime venv (numpy)")
class DaemonPlayerTests(unittest.TestCase):
    def make(self, have, player_words):
        patches = [mock.patch.object(Dm.C, "load_config", return_value=dict(CFG)),
                   mock.patch.object(Dm.W, "TOML_FILE", Path("/nonexistent/config.toml")),
                   mock.patch.object(Dm.M, "installed", return_value=have),
                   mock.patch.object(Dm.M, "listing", return_value=[]),
                   mock.patch.object(Dm.A, "words_in_use", return_value=set(player_words)),
                   mock.patch.object(Dm.D, "notify"),
                   mock.patch.object(Dm.Daemon, "publish")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(Dm.W, "load", side_effect=lambda cfg, path=None: Dm.W.legacy(cfg)):
            d = Dm.Daemon()
        d.wc = Dm.W.legacy(CFG)
        return d

    def test_only_word_is_players(self):
        d = self.make({"ok_ania": Path("/x")}, {"ok_ania"})
        with mock.patch.object(Dm.W, "load", return_value=Dm.W.legacy(CFG)):
            self.assertFalse(d.rebuild())
            d.rebuild()
        self.assertEqual(d.state, "setup")
        self.assertIn("belongs to Ania Player", d.message)
        self.assertEqual(Dm.D.notify.call_count, 1)        # once, not on every rebuild
        self.assertEqual(d.snapshot()["ania_player_words"], ["ok_ania"])

    def test_falls_back_to_other_word(self):
        d = self.make({"ok_ania": Path("/x"), "hey_jarvis": Path("/y")}, {"ok_ania"})
        fake = mock.Mock(names=["hey_jarvis"])
        fake.phrase_of.return_value = "hey jarvis"
        with mock.patch.object(Dm.W, "load", return_value=Dm.W.legacy(CFG)), \
                mock.patch.object(Dm, "MultiDetector", return_value=fake) as md:
            self.assertTrue(d.rebuild())
        self.assertEqual([s.name for s in md.call_args[0][0]], ["hey_jarvis"])
        self.assertEqual(d.snapshot()["wake_word"], "hey_jarvis")

    def test_watch_player_triggers_rebuild(self):
        d = self.make({"ok_ania": Path("/x")}, {"ok_ania"})
        d.need_rebuild = False
        d.player_words = set()
        d.watch_player()
        self.assertTrue(d.need_rebuild)


if __name__ == "__main__":
    unittest.main()
