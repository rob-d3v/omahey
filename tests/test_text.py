# SPDX-License-Identifier: Apache-2.0
import unittest

from omahey import config as C
from omahey import text as T


class TextTests(unittest.TestCase):
    P = C.DEFAULTS["dismiss_phrases"]

    def test_dismiss(self):
        for s in ["That's all.", "ok, that's all, thanks", "Goodbye!", "never mind",
                  "Dismiss", "Hey Omarchy, that's all"]:
            self.assertTrue(T.is_dismiss(s, self.P), s)

    def test_not_dismiss(self):
        for s in ["That's all the files in the folder?", "Say goodbye to the old API and refactor it",
                  "list my files", ""]:
            self.assertFalse(T.is_dismiss(s, self.P), s)

    def test_strip_wake(self):
        self.assertEqual(T.strip_wake("Hey Omarchy, open my notes", ""), "Open my notes")
        self.assertEqual(T.strip_wake("ok ania what time is it", "ok ania"), "What time is it")
        self.assertEqual(T.strip_wake("refactor omarchy code", ""), "refactor omarchy code")
        self.assertEqual(T.strip_wake("Omarchy.", ""), "")

    def test_noise(self):
        self.assertTrue(T.is_noise("you"))
        self.assertTrue(T.is_noise(" . "))
        self.assertFalse(T.is_noise("fix the tests"))


if __name__ == "__main__":
    unittest.main()
