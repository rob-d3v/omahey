# SPDX-License-Identifier: Apache-2.0
"""omahey models import: packs you already own (Ania .zip / folder / manifest.json)."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from omahey import config as C
from omahey import engine as E
from omahey import models as M


def make_pack(d: Path, name="hey_test", verifier=False) -> Path:
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"model_name": name, "head_model": "head.onnx"}))
    (d / "head.onnx").write_bytes(b"x")
    if verifier:
        v = d.parent / (d.name + ".verifier")
        v.mkdir()
        (v / "verifier.json").write_text("{}")
    return d


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.models = self.tmp / "models"
        self.patches = [mock.patch.object(C, "MODELS_DIR", self.models),
                        mock.patch.object(C, "DATA_DIR", self.tmp),
                        mock.patch.object(E, "prepare_bundle", return_value={})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_folder_with_verifier(self):
        src = make_pack(self.tmp / "src" / "pack", verifier=True)
        name, dest = M.import_bundle(src, log=lambda *_: None)
        self.assertEqual(name, "hey_test")
        self.assertTrue((dest / "head.onnx").is_file())
        self.assertTrue((self.models / "hey_test.verifier" / "verifier.json").is_file())

    def test_flat_site_zip_and_replace_drops_old_verifier(self):
        make_pack(self.tmp / "a" / "pack", verifier=True)
        M.import_bundle(self.tmp / "a" / "pack" / "manifest.json", log=lambda *_: None)
        z = self.tmp / "site.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("manifest.json", json.dumps({"model_name": "hey_test", "head_model": "head.onnx"}))
            f.writestr("head.onnx", b"y")
        with self.assertRaises(RuntimeError):
            M.import_bundle(z, log=lambda *_: None)          # already installed
        M.import_bundle(z, log=lambda *_: None, replace=True)
        self.assertEqual((self.models / "hey_test" / "head.onnx").read_bytes(), b"y")
        self.assertFalse((self.models / "hey_test.verifier").exists())

    def test_refuses_unsafe_or_foreign_archives(self):
        z = self.tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("../escape.txt", "x")
        with self.assertRaisesRegex(RuntimeError, "unsafe path"):
            M.import_bundle(z, log=lambda *_: None)
        z2 = self.tmp / "audio.zip"
        with zipfile.ZipFile(z2, "w") as f:
            f.writestr("rec/a.ogg", "x")
        with self.assertRaisesRegex(RuntimeError, "no wake word manifest"):
            M.import_bundle(z2, log=lambda *_: None)
        self.assertFalse((self.tmp.parent / "escape.txt").exists())

    def test_pack_that_does_not_load_is_not_installed(self):
        src = make_pack(self.tmp / "src" / "pack")
        with mock.patch.object(E, "prepare_bundle", side_effect=ValueError("bad head")):
            with self.assertRaisesRegex(RuntimeError, "does not load"):
                M.import_bundle(src, log=lambda *_: None)
        self.assertFalse((self.models / "hey_test").exists())


if __name__ == "__main__":
    unittest.main()


class LegacyMigrationTests(unittest.TestCase):
    """omawake folders move to omahey once, and never over existing omahey ones."""

    def test_moves_once_and_never_overwrites(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from omahey import config as C
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            (t / "c/omawake").mkdir(parents=True)
            (t / "c/omawake/config.toml").write_text("x")
            (t / "d/omawake/models").mkdir(parents=True)
            with mock.patch.multiple(C, CONFIG_DIR=t / "c/omahey", STATE_DIR=t / "s/omahey",
                                     DATA_DIR=t / "d/omahey", MODELS_DIR=t / "d/omahey/models"):
                moved = C.migrate_legacy()
                self.assertEqual(len(moved), 2)
                self.assertEqual((t / "c/omahey/config.toml").read_text(), "x")
                self.assertTrue((t / "d/omahey/models").is_dir())
                (t / "c/omawake").mkdir()
                self.assertEqual(C.migrate_legacy(), [])
