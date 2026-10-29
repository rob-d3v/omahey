# SPDX-License-Identifier: Apache-2.0
"""Ania Player coexistence: which wake words the running player listens for.

Ania Player (aniaplayer.service) has its own wake word engine. If omawake
listened for the same word, one "Ok Ania" would wake both. So omawake leaves
every word the player is using to the player.

The player keeps its wake word settings in its voice assistant plugin:
~/.config/aniaplayer/plugins/voice_assistant/voice_config.json, section
"wake_word" (keys from the player's voice_config.py):

    wake_word_enabled  bool (older files: mode = "wake_word" | "both")
    model_name         primary word (player default "hey_brother")
    model_name_2       legacy AND mode second word
    extra_wake_words   OR-mode extra words (ignored when model_name_2 is set)

Opt out with "skip_ania_player_words": false in ~/.config/omawake/config.json.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import config as C

UNIT = "aniaplayer.service"
DEFAULT_MODEL = "hey_brother"           # player's DEFAULT_WAKE_WORD_MODEL
PLAYER_CFG = Path(os.environ.get("XDG_CONFIG_HOME", C.HOME / ".config")) / "aniaplayer"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def voice_config_file(base: Path | None = None) -> Path:
    base = PLAYER_CFG if base is None else Path(base)
    va = _read(base / "plugins_state.json").get("voice_assistant")
    if isinstance(va, dict) and va.get("install_path"):
        p = Path(str(va["install_path"])) / "voice_config.json"
        if p.is_file():
            return p
    return base / "plugins/voice_assistant/voice_config.json"


def configured_words(base: Path | None = None) -> set[str]:
    """Wake words the player's config turns on (empty when its wake word is off)."""
    ww = _read(voice_config_file(base)).get("wake_word")
    if not isinstance(ww, dict):
        return set()
    enabled = ww.get("wake_word_enabled")
    if enabled is None:
        enabled = ww.get("mode") in ("wake_word", "both")
    if enabled is not True:
        return set()
    names = [ww.get("model_name") or DEFAULT_MODEL]
    second = str(ww.get("model_name_2") or "").strip()
    if second:
        names.append(second)
    elif isinstance(ww.get("extra_wake_words"), list):
        names += ww["extra_wake_words"]
    return {str(n).strip() for n in names if isinstance(n, str) and str(n).strip()}


def running() -> bool:
    try:
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT],
                          capture_output=True, timeout=5).returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:                                  # started by hand, not as a service
        procs = list(Path("/proc").iterdir())
    except OSError:
        return False
    for p in procs:
        if not p.name.isdigit():
            continue
        try:
            argv = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for arg in argv[:3]:
            name = os.path.basename(arg.decode(errors="replace"))
            if name in ("aniaplayer", "run_ania_player.py") or arg == b"ania_player":
                return True
    return False


def words_in_use(cfg: dict, base: Path | None = None, is_running=running) -> set[str]:
    """Wake words omawake must leave to a running Ania Player."""
    if cfg.get("skip_ania_player_words", True) is False:
        return set()
    words = configured_words(base)
    return words if words and is_running() else set()
