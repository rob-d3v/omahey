# SPDX-License-Identifier: Apache-2.0
"""Pick one wake word when several fire close together. Pure Python.

The first verified candidate opens a short window (`window_s`, default
0.4 s). When it closes, the candidate with the highest score wins. The
score is how far the word's peak went over its own threshold, in logit
space, so a word with a strict threshold is not penalised for it. After a
win nothing else fires for `settle_s` (default 1 s), and the winning word
is muted for its own cooldown.
"""
from __future__ import annotations


class Arbiter:
    def __init__(self, window_s: float = 0.4, settle_s: float = 1.0,
                 cooldowns: dict[str, float] | None = None, default_cooldown_s: float = 2.0):
        self.window_s = max(0.0, float(window_s))
        self.settle_s = max(0.0, float(settle_s))
        self.cooldowns = dict(cooldowns or {})
        self.default_cooldown_s = float(default_cooldown_s)
        self.reset()

    def reset(self) -> None:
        self.pending: list[tuple[float, float, str, dict]] = []   # (score, t, name, info)
        self.opened: float | None = None
        self.last_fire: dict[str, float] = {}
        self.blocked_until = float("-inf")

    def cooling(self, name: str, now: float) -> bool:
        t = self.last_fire.get(name)
        return t is not None and now - t < self.cooldowns.get(name, self.default_cooldown_s)

    def offer(self, name: str, score: float, now: float, info: dict | None = None) -> bool:
        """A word's candidate. False when it is dropped right away."""
        if now < self.blocked_until or self.cooling(name, now):
            return False
        if self.opened is None:
            self.opened = now
        self.pending.append((float(score), now, name, dict(info or {})))
        return True

    def poll(self, now: float) -> dict | None:
        """The winner once the window has closed, else None."""
        if self.opened is None or now - self.opened < self.window_s:
            return None
        best: dict[str, tuple[float, float, str, dict]] = {}
        for c in self.pending:
            if c[2] not in best or c[0] > best[c[2]][0]:
                best[c[2]] = c
        score, t, name, info = max(best.values(), key=lambda c: (c[0], -c[1]))
        self.pending, self.opened = [], None
        self.last_fire[name] = now
        self.blocked_until = now + self.settle_s
        return {"wake": name, "score": score, "t": t,
                "rivals": sorted(n for n in best if n != name), **info}
