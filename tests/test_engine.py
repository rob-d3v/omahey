# SPDX-License-Identifier: Apache-2.0
"""Needs the venv (numpy/onnxruntime). Model checks run only when a bundle
with golden files is installed locally."""
import unittest
from pathlib import Path

from omawake import config as C
from omawake import models as M
from omawake.engine import Endpointer


class EndpointerTests(unittest.TestCase):
    def test_onset_and_end(self):
        e = Endpointer(end_silence_ms=400)
        for _ in range(5):
            e.update(0.0, 10, 10)
        self.assertEqual(e.snapshot()[:2], (False, False))
        for _ in range(4):
            e.update(0.9, 900, 10)
        self.assertEqual(e.snapshot()[:2], (True, False))
        for _ in range(5):
            e.update(0.1, 10, 10)
        self.assertEqual(e.snapshot()[:2], (True, True))

    def test_single_blip_is_not_speech(self):
        e = Endpointer()
        for p in (0.0, 0.9, 0.0, 0.0, 0.9, 0.0):
            e.update(p, 10, 10)
        self.assertFalse(e.snapshot()[0])


class GoldenTests(unittest.TestCase):
    def test_golden_clips(self):
        found = M.resolve(C.load_config())
        if not found or not (found[1] / "golden").is_dir():
            self.skipTest("no local bundle with golden clips")
        from omawake import wake_core as ref
        from omawake.engine import Detector
        for wav in sorted((found[1] / "golden").glob("*.wav")):
            ev, _ = Detector(found[1]).run_pcm(ref.read_wav_int16(wav))
            if wav.name.startswith("quieto"):
                self.assertEqual(ev, [], wav.name)
            elif wav.name.startswith("voz"):
                self.assertEqual(len(ev), 1, wav.name)
        fails: list = []
        ref._check_stream_golden(found[1], fails, False)
        self.assertEqual(fails, [])


if __name__ == "__main__":
    unittest.main()
