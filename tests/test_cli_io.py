# SPDX-License-Identifier: Apache-2.0
"""CLI plumbing: broken pipes, `test --wav` padding, audio_target -> pw-record."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

from omahey import audio as A
from omahey.engine import WAV_LEAD_S, WAV_TAIL_S, pad_pcm

SRC = str(Path(__file__).resolve().parents[1] / "src")


class BrokenPipeTests(unittest.TestCase):
    """`omahey status | head -1`: the reader leaves before we finish writing."""

    def _closed_pipe(self, *args):
        code = ("import os, runpy, sys; r, w = os.pipe(); os.close(r); os.dup2(w, 1); "
                f"sys.argv = ['omahey', *{list(args)!r}]; "
                "runpy.run_module('omahey', run_name='__main__')")
        env = dict(os.environ, PYTHONPATH=SRC)
        return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                              text=True, timeout=60)

    def test_help_into_closed_pipe_is_quiet(self):
        p = self._closed_pipe("--help")
        self.assertNotIn("BrokenPipeError", p.stderr)
        self.assertNotIn("Exception ignored", p.stderr)
        self.assertEqual(p.returncode, 0)

    def test_status_into_closed_pipe_is_quiet(self):
        p = self._closed_pipe("status")
        self.assertNotIn("BrokenPipeError", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


class PadTests(unittest.TestCase):
    def test_pads_both_sides_with_silence(self):
        pcm = np.full(16000, 1000, dtype=np.int16)
        out = pad_pcm(pcm)
        lead, tail = int(WAV_LEAD_S * 16000), int(WAV_TAIL_S * 16000)
        self.assertEqual(out.dtype, np.int16)
        self.assertEqual(out.shape[0], lead + 16000 + tail)
        self.assertFalse(out[:lead].any())
        self.assertFalse(out[-tail:].any())
        self.assertTrue((out[lead:lead + 16000] == 1000).all())

    def test_custom_lengths(self):
        self.assertEqual(pad_pcm(np.zeros(10, np.int16), 0, 0.5).shape[0], 8010)


class TargetTests(unittest.TestCase):
    def test_default_source(self):
        self.assertEqual(A.resolve_target("", set()), ("", False))
        cmd = A.record_cmd("pw-record", "", set())
        self.assertNotIn("--target", cmd)
        self.assertNotIn("stream.capture.sink", " ".join(cmd))

    def test_monitor_name_targets_the_sink(self):
        self.assertEqual(A.resolve_target("foo.monitor", set()), ("foo", True))
        cmd = A.record_cmd("pw-record", "foo.monitor", set())
        self.assertEqual(cmd[cmd.index("--target") + 1], "foo")
        props = cmd[cmd.index("-P") + 1]
        self.assertIn("stream.capture.sink = true", props)
        self.assertIn('node.name = "omahey"', props)
        self.assertEqual(cmd[-1], "-")

    def test_bare_sink_name(self):
        self.assertEqual(A.resolve_target("t", {"t", "other"}), ("t", True))

    def test_source_passes_through(self):
        self.assertEqual(A.resolve_target("alsa_input.usb-mic", {"t"}), ("alsa_input.usb-mic", False))
        cmd = A.record_cmd("pw-record", "alsa_input.usb-mic", {"t"})
        self.assertEqual(cmd[cmd.index("--target") + 1], "alsa_input.usb-mic")
        self.assertNotIn("stream.capture.sink", " ".join(cmd))

    def test_list_sinks_parses_pactl(self):
        from unittest import mock
        out = "55\tt\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n60\talsa_output.pci\tPipeWire\tx\tRUNNING\n"
        with mock.patch.object(A.shutil, "which", return_value="/usr/bin/pactl"), \
             mock.patch.object(A.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, out, "")):
            self.assertEqual(A.list_sinks(), {"t", "alsa_output.pci"})
        with mock.patch.object(A.shutil, "which", return_value=None):
            self.assertEqual(A.list_sinks(), set())


class DonateHintTest(unittest.TestCase):
    def test_shown_only_while_hey_omarchy_is_missing(self):
        from omahey import models as M
        self.assertEqual(M.donate_hint({}), M.DONATE_LINE)
        self.assertIn("https://avatar.diatech.solutions/omarchy-voice", M.DONATE_LINE)
        self.assertEqual(M.donate_hint({"hey_jarvis": object()}), M.DONATE_LINE)
        self.assertIsNone(M.donate_hint({"hey_omarchy": object()}))



if __name__ == "__main__":
    unittest.main()

