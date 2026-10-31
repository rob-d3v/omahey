# SPDX-License-Identifier: Apache-2.0
"""Paths and the user config file (~/.config/omahey/config.json).

Everything here is stdlib-only so `omahey status`, `pause`, `setup`, ... run
before the virtualenv exists.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

APP = "omahey"
PLUGIN_ID = "io.github.rob-d3v.omahey"
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
LOG_FILE = STATE_DIR / "omahey.log"
STATE_FILE = RUNTIME_DIR / "state.json"
SOCKET_FILE = RUNTIME_DIR / "control.sock"
UNIT_NAME = "omahey.service"
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


# The plugin was called omawake until 0.3.0. Its folders move over once, the first
# time omahey runs, unless the user already has omahey folders of their own.
# An unrelated app (jacob-vincent-mink/omawake) uses the same folder and unit
# names, so nothing named omawake is touched unless it is provably ours.
LEGACY_APP = "omawake"
LEGACY_UNIT_NAME = "omawake.service"
LEGACY_UNIT_FILE = UNIT_FILE.parent / LEGACY_UNIT_NAME
LEGACY_PLUGIN_ID = "io.github.rob-d3v.omawake"
LEGACY_UNIT_DESCRIPTION = "Description=omawake: wake word for Omarchy (unofficial community plugin)"


def legacy_venv() -> Path:
    return DATA_DIR.parent / LEGACY_APP / "venv"


def legacy_unit_is_ours() -> bool:
    try:
        return LEGACY_UNIT_DESCRIPTION in LEGACY_UNIT_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def legacy_is_ours() -> bool:
    """Whether the omawake folders were made by this plugin under its old name:
    its systemd unit, its uv venv (the other app has none) or its plugin folder."""
    return (legacy_unit_is_ours() or (legacy_venv() / "pyvenv.cfg").is_file()
            or (CONFIG_DIR.parent / "omarchy/plugins" / LEGACY_PLUGIN_ID).is_dir())


def migrate_legacy() -> list[str]:
    """Move ~/.config/omawake, the downloaded models and the log dir to the omahey
    paths. The old venv is left behind: setup builds a fresh one. Returns what moved."""
    moved = []
    if not legacy_is_ours():
        return moved
    pairs = [(CONFIG_DIR.parent / LEGACY_APP, CONFIG_DIR),
             (STATE_DIR.parent / LEGACY_APP, STATE_DIR),
             (DATA_DIR.parent / LEGACY_APP / "models", MODELS_DIR)]
    for old, new in pairs:
        if old.is_dir() and not new.exists():
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                old.rename(new)
                moved.append(f"{old} -> {new}")
            except OSError:
                pass
    return moved


def ensure_dirs() -> None:
    migrate_legacy()
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
