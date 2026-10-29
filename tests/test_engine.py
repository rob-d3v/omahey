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


class MultiDetectorTests(unittest.TestCase):
    """Golden clips through the multi-word path: same events as the
    single-word detector, and extra heads share the one front-end."""

    def bundle(self):
        for p in M.installed(C.load_config()).values():
            if (p / "golden").is_dir():
                return p
        self.skipTest("no local bundle with golden clips")

    def test_same_events_as_single(self):
        b = self.bundle()
        from omawake import wake_core as ref
        from omawake.engine import Detector, MultiDetector, WordSpec
        one = MultiDetector([WordSpec("w", b)])
        two = MultiDetector([WordSpec("w", b), WordSpec("w2", b, threshold=0.999)])
        self.assertEqual(len(two.groups), 1)
        for wav in sorted((b / "golden").glob("*.wav")):
            pcm = ref.read_wav_int16(wav)
            ref_ev, _ = Detector(b).run_pcm(pcm)
            for det in (one, two):
                det.reset()
                got = [e for e in det.run_pcm(pcm) if e["wake"] == "w"]
                self.assertEqual([e["frame"] for e in got], [e["frame"] for e in ref_ev], wav.name)

    def test_frontend_aliases(self):
        from omawake.engine import FRONTEND_ALIASES
        self.assertEqual(sorted(set(FRONTEND_ALIASES.values())), ["oww-emb-v1", "oww-mel-v1"])


if __name__ == "__main__":
    unittest.main()
