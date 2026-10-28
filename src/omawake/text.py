# SPDX-License-Identifier: Apache-2.0
"""Transcript clean-up: strip a leading wake phrase, spot dismiss phrases."""
from __future__ import annotations

import re

_PUNCT = re.compile(r"[^\w\s']+")
WAKE_VARIANTS = ["hey omarchy", "hi omarchy", "ok omarchy", "okay omarchy", "omarchy",
                 "oh marchy", "omar key", "o'marchy"]


def norm(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", text.lower().replace("’", "'")).split())


def strip_wake(text: str, phrase: str = "") -> str:
    """Drop a wake phrase the user said again at the start of the turn."""
    variants = sorted({norm(phrase)} | set(WAKE_VARIANTS) - {""}, key=len, reverse=True)
    words = text.strip()
    low = norm(words)
    for v in variants:
        if v and low.startswith(v + " ") or low == v:
            # cut the same number of words from the original text
            n = len(v.split())
            parts = words.split()
            rest = " ".join(parts[n:]).lstrip(" ,.!?;:-")
            return rest[:1].upper() + rest[1:] if rest else ""
    return words


def is_dismiss(text: str, phrases: list[str]) -> bool:
    """True when the whole (short) utterance is a dismiss phrase, optionally
    with politeness around it ("ok that's all, thanks")."""
    t = norm(text)
    if not t or len(t.split()) > 6:
        return False
    filler = r"(?:ok|okay|alright|all right|thanks|thank you|please|omarchy|hey|so|um|uh|that's it|\s)*"
    for p in phrases:
        p = norm(p)
        if p and re.fullmatch(filler + r"\b" + re.escape(p) + r"\b" + filler, t):
            return True
    return False


def is_noise(text: str) -> bool:
    """Whisper's usual output on silence/noise."""
    t = norm(text)
    return t in ("", "you", "thank you", "thanks for watching", "bye") or len(t) < 2
