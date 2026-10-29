# SPDX-License-Identifier: Apache-2.0
import unittest

from omawake.arbiter import Arbiter


class ArbiterTests(unittest.TestCase):
    def test_highest_score_in_window_wins(self):
        a = Arbiter(window_s=0.4, settle_s=1.0)
        self.assertTrue(a.offer("omarchy", 1.2, 10.00))
        self.assertIsNone(a.poll(10.10))
        self.assertTrue(a.offer("hey_omarchy", 2.5, 10.20))
        self.assertIsNone(a.poll(10.30))
        w = a.poll(10.41)
        self.assertEqual(w["wake"], "hey_omarchy")
        self.assertEqual(w["rivals"], ["omarchy"])
        self.assertIsNone(a.poll(10.9))

    def test_late_rival_inside_settle_is_dropped(self):
        a = Arbiter(window_s=0.2, settle_s=1.0)
        a.offer("a", 1.0, 0.0)
        self.assertEqual(a.poll(0.2)["wake"], "a")
        self.assertFalse(a.offer("b", 9.0, 0.9))      # within ~1 s of the win
        self.assertIsNone(a.poll(1.5))
        self.assertTrue(a.offer("b", 9.0, 1.3))       # after settle
        self.assertEqual(a.poll(1.6)["wake"], "b")

    def test_per_word_cooldown(self):
        a = Arbiter(window_s=0.0, settle_s=0.0, cooldowns={"a": 5.0}, default_cooldown_s=1.0)
        a.offer("a", 1.0, 0.0)
        self.assertEqual(a.poll(0.0)["wake"], "a")
        self.assertFalse(a.offer("a", 3.0, 2.0))      # a still cooling
        self.assertTrue(a.offer("b", 0.5, 2.0))       # b is not
        self.assertEqual(a.poll(2.0)["wake"], "b")
        self.assertTrue(a.offer("a", 1.0, 5.1))
        self.assertFalse(a.offer("b", 1.0, 2.5))      # b default cooldown 1 s
        self.assertEqual(a.poll(5.1)["wake"], "a")

    def test_same_word_twice_keeps_best(self):
        a = Arbiter(window_s=0.3, settle_s=0.0)
        a.offer("a", 0.5, 0.0, {"peak": 0.7})
        a.offer("a", 0.9, 0.1, {"peak": 0.9})
        w = a.poll(0.3)
        self.assertEqual((w["wake"], w["score"], w["peak"], w["rivals"]), ("a", 0.9, 0.9, []))

    def test_tie_goes_to_earlier(self):
        a = Arbiter(window_s=0.3, settle_s=0.0)
        a.offer("x", 1.0, 0.0)
        a.offer("y", 1.0, 0.1)
        self.assertEqual(a.poll(0.3)["wake"], "x")


if __name__ == "__main__":
    unittest.main()
