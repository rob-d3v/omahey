# SPDX-License-Identifier: Apache-2.0
"""Phrase matching and the command runner (argv only, never a shell)."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omawake import commands as K
from omawake import config as C
from omawake import wakeconf as W

TABLE = {
    "lock screen": "omarchy-system-lock", "lock": "omarchy-system-lock",
    "screenshot": "omarchy-capture-screenshot", "take a screenshot": "omarchy-capture-screenshot",
    "volume up": "omarchy-audio-output-volume raise", "volume down": "omarchy-audio-output-volume lower",
    "night light": "omarchy-toggle-nightlight", "open browser": "omarchy-launch-browser",
    "open terminal": "omarchy-launch-terminal", "next background": "omarchy-theme-bg-next",
}


class MatchTests(unittest.TestCase):
    def expect(self, said, cmd):
        hit = K.match(said, TABLE)
        self.assertIsNotNone(hit, said)
        self.assertEqual(hit[1], cmd, said)

    def test_matches(self):
        self.expect("Lock screen.", "omarchy-system-lock")
        self.expect("could you lock the screen please", "omarchy-system-lock")
        self.expect("Take a screenshot!", "omarchy-capture-screenshot")
        self.expect("screen shot", "omarchy-capture-screenshot")
        self.expect("volume up", "omarchy-audio-output-volume raise")
        self.expect("Volume down, please.", "omarchy-audio-output-volume lower")
        self.expect("turn on the night light", "omarchy-toggle-nightlight")
        self.expect("open the browser", "omarchy-launch-browser")
        self.expect("open a terminal", "omarchy-launch-terminal")
        self.expect("next backgrounds", "omarchy-theme-bg-next")

    def test_no_match(self):
        for said in ("what's the weather like", "refactor the parser", "", "   ", "open"):
            self.assertIsNone(K.match(said, TABLE), said)

    def test_ambiguous_is_none(self):
        self.assertIsNone(K.match("volume", TABLE))

    def test_speech_only_selects_configured_command(self):
        hit = K.match("lock screen; rm -rf ~", TABLE)
        self.assertIn(hit[1] if hit else None, (None, "omarchy-system-lock"))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.p = [mock.patch.object(C, "STATE_DIR", d), mock.patch.object(C, "LOG_FILE", d / "log"),
                  mock.patch.object(C, "HOME", d)]
        for p in self.p:
            p.start()
        self.d = d

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def test_no_shell_metacharacters(self):
        marker = self.d / "PWNED"
        r = K.run(f'echo "$(touch {marker})" ; touch {marker} && touch {marker}')
        self.assertTrue(r["ok"], r)
        self.assertFalse(marker.exists())
        self.assertEqual(r["argv"][0], "echo")
        self.assertIn(";", r["argv"])            # literal argument, not a separator

    def test_popen_gets_argv_list_without_shell(self):
        with mock.patch.object(K.subprocess, "Popen") as po:
            po.return_value.wait.return_value = 0
            po.return_value.pid = 1
            K.run("omarchy-audio-output-volume raise")
        args, kw = po.call_args
        self.assertIsInstance(args[0], list)
        self.assertTrue(args[0][0].endswith("omarchy-audio-output-volume") or args[0][0] == "omarchy-audio-output-volume")
        self.assertEqual(args[0][1:], ["raise"])
        self.assertIs(kw.get("shell"), False)

    def test_timeout_kills(self):
        t = time.monotonic()
        r = K.run("sleep 30", timeout_s=0.5)
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"])
        self.assertLess(time.monotonic() - t, 5)

    def test_missing_program(self):
        r = K.run("definitely-not-a-program-omawake")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_logged(self):
        K.run("true")
        self.assertTrue((self.d / "log").exists())

    def test_dry_run_runs_nothing(self):
        with mock.patch.object(K.subprocess, "Popen") as po:
            r = K.run("true", dry_run=True)
            a = K.agent_prompt("x; rm -rf ~", dry_run=True) if K.shutil.which(K.AGENT_PROMPT) else None
        po.assert_not_called()
        self.assertTrue(r["dry_run"])
        if a:
            self.assertEqual(a["argv"], [K.AGENT_PROMPT, "x; rm -rf ~"])   # one argv element

    def test_agent_prompt_is_one_argument(self):
        with mock.patch.object(K.shutil, "which", return_value="/usr/bin/omarchy-agent-prompt"), \
                mock.patch.object(K.subprocess, "Popen") as po:
            K.agent_prompt('open "$(id)" && reboot')
        args, kw = po.call_args
        self.assertEqual(args[0], ["/usr/bin/omarchy-agent-prompt", 'open "$(id)" && reboot'])
        self.assertIs(kw.get("shell"), False)


class UnsafeConfigTests(unittest.TestCase):
    """The daemon must not run any command from a config others can write."""

    def test_daemon_refuses(self):
        try:
            from omawake import daemon as Dm
        except ImportError:
            self.skipTest("needs the venv")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "config.toml"
            f.write_text('[[wake]]\nmodel="j"\naction="command"\ncommand="true"\n'
                         '[[wake]]\nmodel="k"\naction="command"\ncommands={ "lock" = "true" }\n')
            os.chmod(f, 0o666)
            wc = W.load({}, f)
            self.assertFalse(wc.commands_allowed)
            dm = Dm.Daemon.__new__(Dm.Daemon)
            dm.wc, dm.cfg, dm.dry_run = wc, dict(C.DEFAULTS, chime=False), False
            dm.last_result = ""
            dm.record_turn = mock.Mock(side_effect=AssertionError("must not listen"))
            with mock.patch.object(Dm.K, "run") as run, mock.patch.object(Dm.D, "notify") as note:
                dm.command_session(wc.get("j"), "j")
                dm.command_session(wc.get("k"), "k", injected="lock")
            run.assert_not_called()
            self.assertEqual(note.call_count, 2)
            self.assertIn("disabled", dm.last_result)


if __name__ == "__main__":
    unittest.main()
