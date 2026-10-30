# SPDX-License-Identifier: Apache-2.0
"""Paths and the user config file (~/.config/omawake/config.json).

Everything here is stdlib-only so `omawake status`, `pause`, `setup`, ... run
before the virtualenv exists.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

APP = "omawake"
PLUGIN_ID = "io.github.rob-d3v.omawake"
CREATE_WAKEWORD_URL = "https://aniamodels.shop/create-wakeword"

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parents[2]          # plugin checkout root
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / APP
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / APP
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / APP
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / APP

CONFIG_FILE = CONFIG_DIR / "config.json"
VENV_DIR = DATA_DIR / "venv"
MODELS_DIR = DATA_DIR / "models"
LOG_FILE = STATE_DIR / "omawake.log"
STATE_FILE = RUNTIME_DIR / "state.json"
SOCKET_FILE = RUNTIME_DIR / "control.sock"
UNIT_NAME = "omawake.service"
UNIT_FILE = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user" / UNIT_NAME

MODES = ("ask", "dictate")
SENSITIVITIES = ("low", "normal", "high")
# Logit offset added to the model's calibrated threshold. Positive = stricter.
SENSITIVITY_SHIFT = {"low": 1.0, "normal": 0.0, "high": -1.0}

DEFAULTS: dict = {
    "mode": "ask",
    "sensitivity": "normal",
    "wake_word": "hey_omarchy",
    # Extra folders scanned for wake word bundles (each bundle = a folder
    # with manifest.json + head .onnx + models/). Ania Player's folder is
    # picked up automatically when it exists.
    "model_dirs": [],
    "chime": True,
    "conversation": True,
    "conversation_timeout_s": 20,
    "first_speech_timeout_s": 6,
    "end_silence_ms": 1100,
    "max_utterance_s": 35,          # + 20 s wait stays under Voxtype's 60 s cap
    "submit_delay_ms": 350,
    "agent_ready_delay_ms": 1500,
    "agent_launch_timeout_s": 12,
    "dismiss_phrases": ["that's all", "that is all", "dismiss", "goodbye",
                        "good bye", "bye", "never mind", "nevermind", "stop listening"],
    "audio_target": "",          # pw-record --target; empty = default source
}

ANIA_PLAYER_MODELS = HOME / ".local/share/aniaplayer/wakeword"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR, RUNTIME_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # The state dir holds the log, which names wake words and commands.
    for d in (RUNTIME_DIR, STATE_DIR):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(user, dict):
            cfg.update(user)
    except (OSError, ValueError):
        pass
    if cfg.get("mode") not in MODES:
        cfg["mode"] = DEFAULTS["mode"]
    if cfg.get("sensitivity") not in SENSITIVITIES:
        cfg["sensitivity"] = DEFAULTS["sensitivity"]
    return cfg


def save_config(changes: dict) -> dict:
    """Merge `changes` into the user's config file (only non-default keys the
    user touched are written) and return the effective config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    current.update(changes)
    atomic_write(CONFIG_FILE, json.dumps(current, indent=2) + "\n")
    return load_config()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def model_search_dirs(cfg: dict) -> list[Path]:
    dirs = [MODELS_DIR]
    for d in cfg.get("model_dirs") or []:
        dirs.append(Path(os.path.expanduser(str(d))))
    if ANIA_PLAYER_MODELS.is_dir():
        dirs.append(ANIA_PLAYER_MODELS)
    out, seen = [], set()
    for d in dirs:
        if str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out
