# SPDX-License-Identifier: Apache-2.0
"""config.toml parsing, validation and the file-permission checks. Stdlib only."""
import os
import tempfile
import unittest
from pathlib import Path

from omawake import wakeconf as W

ALL = lambda prog: True  # noqa: E731 - every program "exists"


class ParseTests(unittest.TestCase):
    def test_example_config(self):
        wc = W.parse('''
arbitration_ms = 300
[[wake]]
model = "hey_omarchy"
action = "ask"
[[wake]]
model = "omarchy"
action = "dictate"
sensitivity = "high"
[[wake]]
model = "hey_jarvis"
action = "command"
command = "omarchy-launch-browser"
[[wake]]
model = "computer"
action = "command"
commands = { "lock" = "omarchy-system-lock", "next background" = "omarchy-theme-bg-next" }
fallback = "agent"
threshold = 0.6
cooldown_s = 3
''', have_program=ALL)
        self.assertEqual(wc.errors, [])
        self.assertEqual([s.model for s in wc.specs], ["hey_omarchy", "omarchy", "hey_jarvis", "computer"])
        self.assertEqual(wc.arbitration_ms, 300)
        self.assertEqual(wc.get("omarchy").sensitivity, "high")
        self.assertEqual(wc.get("hey_jarvis").command, "omarchy-launch-browser")
        c = wc.get("computer")
        self.assertEqual(c.commands["lock"], "omarchy-system-lock")
        self.assertEqual((c.fallback, c.threshold, c.cooldown_s), ("agent", 0.6, 3.0))

    def test_invalid_blocks_are_dropped_with_errors(self):
        wc = W.parse('''
[[wake]]
action = "ask"
[[wake]]
model = "a"
action = "explode"
[[wake]]
model = "b"
action = "command"
[[wake]]
model = "c"
action = "command"
command = "x"
commands = { "y" = "z" }
[[wake]]
model = "d"
threshold = 7
[[wake]]
model = "e"
action = "command"
command = "   "
[[wake]]
model = "ok"
[[wake]]
model = "ok"
action = "dictate"
''', have_program=ALL)
        self.assertEqual([s.model for s in wc.specs], ["ok"])
        self.assertEqual(wc.specs[0].action, "ask")
        self.assertEqual(len(wc.errors), 7, wc.errors)

    def test_bad_toml(self):
        wc = W.parse("[[wake]\nmodel=", have_program=ALL)
        self.assertEqual(wc.specs, [])
        self.assertTrue(wc.errors)

    def test_unbalanced_quotes_rejected(self):
        wc = W.parse('[[wake]]\nmodel="a"\naction="command"\ncommand="echo \\"oops"\n', have_program=ALL)
        self.assertEqual(wc.specs, [])

    def test_pack_only_keeps_present_programs(self):
        wc = W.parse('[[wake]]\nmodel="j"\naction="command"\npack="omarchy"\n'
                     'commands={ "lock" = "my-locker" }\n',
                     have_program=lambda p: p == "omarchy-toggle-nightlight")
        t = wc.get("j").commands
        self.assertEqual(t["lock"], "my-locker")            # user entry wins over the pack
        self.assertEqual(t["night light"], "omarchy-toggle-nightlight")
        self.assertNotIn("screenshot", t)

    def test_unknown_keys_warn(self):
        wc = W.parse('foo = 1\n[[wake]]\nmodel="a"\ncolour="red"\n', have_program=ALL)
        self.assertEqual(len(wc.warnings), 2)
        self.assertEqual(len(wc.specs), 1)

    def test_legacy_and_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "config.toml"
            wc = W.load({"wake_word": "ok_ania", "mode": "dictate"}, missing)
            self.assertEqual((wc.source, wc.specs[0].model, wc.specs[0].action), ("legacy", "ok_ania", "dictate"))
            missing.write_text('[[wake]]\nmodel="a"\n[[wake]]\nmodel="b"\nenabled=false\n')
            os.chmod(missing, 0o600)
            wc = W.load({"word_enabled": {"a": False, "b": True}}, missing)
            self.assertEqual([(s.model, s.enabled) for s in wc.specs], [("a", False), ("b", True)])

    def test_pack_commands_are_real_omarchy_commands(self):
        omarchy_bin = Path("/usr/share/omarchy/bin")
        if not omarchy_bin.is_dir():
            self.skipTest("not on Omarchy")
        for phrase, cmd in W.OMARCHY_PACK.items():
            self.assertTrue((omarchy_bin / W.parse_command(cmd)[0]).is_file(), phrase)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "omawake"
        self.dir.mkdir(mode=0o700)
        self.f = self.dir / "config.toml"
        self.f.write_text('[[wake]]\nmodel="a"\naction="command"\ncommand="true"\n')
        os.chmod(self.f, 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_file_ok(self):
        self.assertEqual(W.check_file_safe(self.f), "")
        self.assertTrue(W.load({}, self.f).commands_allowed)

    def test_world_writable_refused(self):
        os.chmod(self.f, 0o666)
        why = W.check_file_safe(self.f)
        self.assertIn("writable by other users", why)
        wc = W.load({}, self.f)
        self.assertFalse(wc.commands_allowed)
        self.assertEqual(wc.unsafe_reason, why)

    def test_group_writable_refused(self):
        os.chmod(self.f, 0o620)
        self.assertIn("writable", W.check_file_safe(self.f))

    def test_other_owner_refused(self):
        self.assertIn("not owned by you", W.check_file_safe(self.f, uid=os.getuid() + 1))

    def test_world_writable_dir_refused(self):
        os.chmod(self.dir, 0o777)
        try:
            self.assertIn("world-writable", W.check_file_safe(self.f))
        finally:
            os.chmod(self.dir, 0o700)

    def test_symlink_follows_target(self):
        link = Path(self.tmp.name) / "link.toml"
        link.symlink_to(self.f)
        self.assertEqual(W.check_file_safe(link), "")
        os.chmod(self.f, 0o646)
        self.assertNotEqual(W.check_file_safe(link), "")

    def test_not_a_regular_file(self):
        self.assertIn("not a regular file", W.check_file_safe(self.dir))

    def test_template_is_private(self):
        p = self.dir / "new.toml"
        self.assertTrue(W.write_template(p))
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertFalse(W.write_template(p))
        wc = W.parse(p.read_text(), have_program=ALL)
        self.assertEqual((wc.errors, [s.model for s in wc.specs]), ([], ["hey_omarchy"]))


if __name__ == "__main__":
    unittest.main()
