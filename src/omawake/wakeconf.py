# SPDX-License-Identifier: Apache-2.0
"""Wake words and their actions: ~/.config/omawake/config.toml. Stdlib only.

    [[wake]]
    model = "hey_omarchy"
    action = "ask"                 # ask | dictate | command | agent-prompt

    [[wake]]
    model = "hey_jarvis"
    action = "command"
    command = "omarchy-launch-browser"          # fixed command, no speech
    # or a voice command map (say the wake word, then a phrase):
    # commands = { "lock" = "omarchy-system-lock" }
    # pack = "omarchy"                          # add the built-in command pack
    # fallback = "agent"                        # unknown phrase -> omarchy agent prompt

Security model (see README "Security"):
  * commands come only from this file, never from transcribed speech;
  * the file must be a regular file owned by you and not writable by others,
    in a directory owned by you (or root) that others cannot write;
    otherwise no command from it ever runs (ask / dictate still work);
  * commands run as an argv list (shlex.split), never through a shell.

omawake never writes this file after creating the starter template; the
on/off switches in the widget live in config.json (`word_enabled`).
"""
from __future__ import annotations

import os
import shlex
import shutil
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C

TOML_FILE = C.CONFIG_DIR / "config.toml"
ACTIONS = ("ask", "dictate", "command", "agent-prompt")
FALLBACKS = ("none", "agent")
WAKE_KEYS = {"model", "action", "command", "commands", "pack", "fallback", "threshold",
             "sensitivity", "cooldown_s", "enabled", "timeout_s", "label"}
TOP_KEYS = {"wake", "arbitration_ms", "settle_ms", "command_timeout_s", "match_cutoff"}

# Built only from commands that ship in /usr/share/omarchy/bin (checked
# against Omarchy 4.x). Entries whose program is missing are dropped at
# load time, so the pack never offers something that cannot run.
OMARCHY_PACK: dict[str, str] = {
    "lock screen": "omarchy-system-lock",
    "lock": "omarchy-system-lock",
    "screenshot": "omarchy-capture-screenshot",
    "take a screenshot": "omarchy-capture-screenshot",
    "screen recording": "omarchy-capture-screenrecording",
    "next background": "omarchy-theme-bg-next",
    "change theme": "omarchy-theme-switcher",
    "night light": "omarchy-toggle-nightlight",
    "toggle night light": "omarchy-toggle-nightlight",
    "open browser": "omarchy-launch-browser",
    "browser": "omarchy-launch-browser",
    "open terminal": "omarchy-launch-terminal",
    "terminal": "omarchy-launch-terminal",
    "open files": "omarchy-launch-nautilus",
    "volume up": "omarchy-audio-output-volume raise",
    "louder": "omarchy-audio-output-volume raise",
    "volume down": "omarchy-audio-output-volume lower",
    "quieter": "omarchy-audio-output-volume lower",
    "mute": "omarchy-audio-output-volume mute-toggle",
    "do not disturb": "omarchy-toggle-notification-silencing",
    "stay awake": "omarchy-toggle-idle",
    "open menu": "omarchy-menu",
}
PACKS = {"omarchy": OMARCHY_PACK}

TEMPLATE = """\
# omawake wake words. Each [[wake]] block is one wake word and what it does.
# Reload after editing: the widget does it for you, or run `omawake reload`.
# Check it: omawake config check      Models: omawake models
#
# action = "ask"           open your Omarchy agent and type what you say next
# action = "dictate"       type what you say next at the cursor (Voxtype)
# action = "agent-prompt"  start a new agent with what you say: omarchy agent prompt "<text>"
# action = "command"       run a command from THIS file (never from speech):
#                            command  = "omarchy-launch-browser"    (no speech needed)
#                            commands = { "lock" = "omarchy-system-lock" }   (say a phrase)
#                            pack = "omarchy"     adds the built-in Omarchy command pack
#                            fallback = "agent"   unknown phrase -> omarchy agent prompt
# Optional per word: threshold = 0.6 | sensitivity = "low|normal|high" |
#                    cooldown_s = 2 | enabled = false | timeout_s = 15

[[wake]]
model = "{model}"{model_note}
action = "ask"

# Coming soon: the official “Hey Omarchy” model is still training. Once
# `omawake models` lists hey_omarchy as installed, use it here instead.
# [[wake]]
# model = "hey_omarchy"
# action = "ask"

# [[wake]]
# model = "{example}"            # omawake models download {example} (non-commercial licence)
# action = "command"
# pack = "omarchy"
# commands = { "next background" = "omarchy-theme-bg-next" }
# fallback = "agent"
"""


@dataclass
class WakeSpec:
    model: str
    action: str = "ask"
    command: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    fallback: str = "none"
    threshold: float | None = None
    sensitivity: str | None = None
    cooldown_s: float | None = None
    timeout_s: float | None = None
    enabled: bool = True
    label: str = ""

    def describe(self) -> str:
        if self.action == "command":
            if self.command:
                return f"runs {self.command}"
            extra = ", else asks the agent" if self.fallback == "agent" else ""
            return f"voice commands ({len(self.commands)} phrases{extra})"
        return {"ask": "asks the agent", "dictate": "dictates at the cursor",
                "agent-prompt": "new agent prompt"}[self.action]

    def to_json(self) -> dict:
        return {"model": self.model, "action": self.action, "command": self.command,
                "commands": dict(self.commands), "fallback": self.fallback,
                "threshold": self.threshold, "sensitivity": self.sensitivity,
                "cooldown_s": self.cooldown_s, "enabled": self.enabled,
                "label": self.label, "describe": self.describe()}


@dataclass
class WakeConfig:
    specs: list[WakeSpec]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "legacy"            # "toml" | "legacy"
    path: str = str(TOML_FILE)
    commands_allowed: bool = True
    unsafe_reason: str = ""
    arbitration_ms: int = 400
    settle_ms: int = 1000
    command_timeout_s: float = 15.0
    match_cutoff: float = 0.72

    def enabled(self) -> list[WakeSpec]:
        return [s for s in self.specs if s.enabled]

    def get(self, model: str) -> WakeSpec | None:
        return next((s for s in self.specs if s.model == model), None)


# ── security ───────────────────────────────────────────────────────────────
def check_file_safe(path: Path, uid: int | None = None) -> str:
    """'' when commands from `path` may run, else the reason they may not.

    The file (after following symlinks, so dotfile managers keep working)
    must be a regular file owned by `uid` and not group/world-writable, and
    its directory must be owned by `uid` or root and not writable by others
    (else someone could swap the file)."""
    uid = os.getuid() if uid is None else uid
    try:
        real = Path(os.path.realpath(path))
        st = os.stat(real)
    except OSError as e:
        return f"cannot stat {path}: {e.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return f"{real} is not a regular file"
    if st.st_uid != uid:
        return f"{real} is not owned by you (owner uid {st.st_uid})"
    if st.st_mode & (stat.S_IWOTH | stat.S_IWGRP):
        return f"{real} is writable by other users (mode {stat.S_IMODE(st.st_mode):o}); run: chmod go-w {real}"
    try:
        dst = os.stat(real.parent)
    except OSError as e:
        return f"cannot stat {real.parent}: {e.strerror}"
    if dst.st_uid not in (uid, 0):
        return f"{real.parent} is not owned by you"
    if dst.st_mode & stat.S_IWOTH and not dst.st_mode & stat.S_ISVTX:
        return f"{real.parent} is world-writable"
    if dst.st_mode & stat.S_IWGRP and dst.st_uid != uid:
        return f"{real.parent} is group-writable"
    return ""


def parse_command(cmdline: str) -> list[str]:
    """argv for a configured command. Raises ValueError when unusable."""
    if not isinstance(cmdline, str) or not cmdline.strip():
        raise ValueError("empty command")
    argv = shlex.split(cmdline)
    if not argv:
        raise ValueError("empty command")
    return argv


# ── parsing ────────────────────────────────────────────────────────────────
def _num(v, name: str, lo: float, hi: float, errs: list, where: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
        errs.append(f"{where}: {name} must be a number between {lo:g} and {hi:g}")
        return None
    return float(v)


def parse(text: str, have_program=shutil.which) -> WakeConfig:
    """Parse and validate config.toml text. Invalid [[wake]] blocks are
    dropped with an error; the rest still loads."""
    wc = WakeConfig(specs=[], source="toml")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        wc.errors.append(f"config.toml: {e}")
        return wc
    for k in set(data) - TOP_KEYS:
        wc.warnings.append(f"unknown setting '{k}' (ignored)")
    for key, lo, hi in (("arbitration_ms", 0, 1000), ("settle_ms", 0, 5000),
                        ("command_timeout_s", 1, 300), ("match_cutoff", 0.4, 1.0)):
        v = _num(data.get(key), key, lo, hi, wc.errors, "config.toml")
        if v is not None:
            setattr(wc, key, int(v) if key.endswith("_ms") else v)
    blocks = data.get("wake", [])
    if not isinstance(blocks, list):
        wc.errors.append("'wake' must be a list of [[wake]] tables")
        return wc
    seen: set[str] = set()
    for i, b in enumerate(blocks, 1):
        where = f"[[wake]] #{i}"
        if not isinstance(b, dict):
            wc.errors.append(f"{where}: not a table")
            continue
        errs: list[str] = []
        model = b.get("model")
        if not isinstance(model, str) or not model.strip():
            wc.errors.append(f"{where}: 'model' is required")
            continue
        model = model.strip()
        where = f"[[wake]] {model}"
        if model in seen:
            wc.errors.append(f"{where}: listed twice (only the first one is used)")
            continue
        for k in set(b) - WAKE_KEYS:
            wc.warnings.append(f"{where}: unknown key '{k}' (ignored)")
        action = b.get("action", "ask")
        if action not in ACTIONS:
            wc.errors.append(f"{where}: action must be one of {', '.join(ACTIONS)}")
            continue
        s = WakeSpec(model=model, action=action)
        s.label = str(b.get("label") or "")
        s.enabled = b.get("enabled", True) is not False
        s.threshold = _num(b.get("threshold"), "threshold", 0.01, 0.999, errs, where)
        s.cooldown_s = _num(b.get("cooldown_s"), "cooldown_s", 0.0, 60.0, errs, where)
        s.timeout_s = _num(b.get("timeout_s"), "timeout_s", 1.0, 300.0, errs, where)
        sens = b.get("sensitivity")
        if sens is not None and sens not in C.SENSITIVITIES:
            errs.append(f"{where}: sensitivity must be low, normal or high")
        else:
            s.sensitivity = sens
        fb = b.get("fallback", "none")
        if fb not in FALLBACKS:
            errs.append(f"{where}: fallback must be 'none' or 'agent'")
        else:
            s.fallback = fb
        if action == "command":
            cmd, cmds, pack = b.get("command"), b.get("commands"), b.get("pack")
            if cmd is not None and (cmds is not None or pack is not None):
                errs.append(f"{where}: use either 'command' or 'commands'/'pack', not both")
            elif cmd is not None:
                try:
                    parse_command(cmd)
                    s.command = cmd
                except ValueError as e:
                    errs.append(f"{where}: command: {e}")
            else:
                table: dict[str, str] = {}
                if pack is not None:
                    if pack not in PACKS:
                        errs.append(f"{where}: unknown pack '{pack}' (known: {', '.join(PACKS)})")
                    else:
                        for ph, c in PACKS[pack].items():
                            if have_program(parse_command(c)[0]):
                                table[ph] = c
                if cmds is not None and not isinstance(cmds, dict):
                    errs.append(f"{where}: 'commands' must be a table of \"phrase\" = \"command\"")
                for ph, c in (cmds or {}).items() if isinstance(cmds, dict) else ():
                    try:
                        parse_command(c)
                    except ValueError as e:
                        errs.append(f"{where}: commands.{ph!r}: {e}")
                        continue
                    if not str(ph).strip():
                        errs.append(f"{where}: empty phrase in commands")
                        continue
                    table[str(ph).strip()] = c
                if not table and not errs:
                    errs.append(f"{where}: action 'command' needs 'command', 'commands' or 'pack'")
                s.commands = table
        elif any(k in b for k in ("command", "commands", "pack")):
            wc.warnings.append(f"{where}: command settings are ignored for action '{action}'")
        if errs:
            wc.errors.extend(errs)
            continue
        seen.add(model)
        wc.specs.append(s)
    return wc


def legacy(cfg: dict) -> WakeConfig:
    """No config.toml: the single wake word + mode from config.json."""
    return WakeConfig(specs=[WakeSpec(model=str(cfg.get("wake_word") or "hey_omarchy"),
                                      action=cfg.get("mode", "ask"))], source="legacy")


def load(cfg: dict | None = None, path: Path | None = None) -> WakeConfig:
    cfg = C.load_config() if cfg is None else cfg
    path = TOML_FILE if path is None else Path(path)
    if not path.exists():
        wc = legacy(cfg)
        wc.path = str(path)
    else:
        try:
            wc = parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            wc = WakeConfig(specs=[], errors=[f"cannot read {path}: {e}"], source="toml")
        wc.path = str(path)
        wc.unsafe_reason = check_file_safe(path)
        wc.commands_allowed = not wc.unsafe_reason
    overlay = cfg.get("word_enabled") or {}
    if isinstance(overlay, dict):
        for s in wc.specs:
            if isinstance(overlay.get(s.model), bool):
                s.enabled = overlay[s.model]
    return wc


PREFERRED = "hey_omarchy"         # the official word, once its model is published
FALLBACK = "hey_jarvis"           # downloadable today (upstream, non-commercial)


def starter_model(have, skip=(), order=()) -> tuple[str, bool]:
    """(model, installed) for the starter config: the official word when it
    is installed, else an installed word (catalogue order first) that Ania
    Player is not using, else the downloadable fallback."""
    usable = [m for m in have if m not in set(skip)]
    if PREFERRED in usable:
        return PREFERRED, True
    for m in [*order, *sorted(usable)]:
        if m in usable:
            return m, True
    return FALLBACK, False


def render_template(model: str = PREFERRED, installed: bool = True) -> str:
    note = "" if installed else \
        f"   # not installed yet: omawake models download {model} (then: omawake reload)"
    example = "hey_mycroft" if model == FALLBACK else FALLBACK
    return TEMPLATE.replace("{model}", model).replace("{model_note}", note) \
        .replace("{example}", example)


def write_template(path: Path | None = None, model: str = PREFERRED, installed: bool = True) -> bool:
    """Create the starter config.toml (0600) if it does not exist."""
    path = TOML_FILE if path is None else Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(render_template(model, installed))
    return True
