# SPDX-License-Identifier: Apache-2.0
"""`omahey setup` / `omahey uninstall`: idempotent first run. Stdlib only.

Each step checks first and only acts when something is missing, so running
it again is always safe. Nothing outside these paths is touched:
  ~/.local/share/omahey/   (venv, downloaded models)
  ~/.config/omahey/        (config.json, config.toml: created only on request)
  ~/.config/systemd/user/omahey.service
  ~/.local/bin/omahey      (symlink to the plugin's bin/omahey)
Voxtype's own config and your keybindings are never edited.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as C
from . import models as M
from . import wakeconf as W

BIN = C.REPO_DIR / "bin" / "omahey"
LINK = C.HOME / ".local/bin/omahey"


class UI:
    def __init__(self, assume_yes: bool):
        self.yes = assume_yes
        self.tty = sys.stdin.isatty()

    def ask(self, question: str, default: bool = True) -> bool:
        if self.yes:
            print(f"  {question} yes (--yes)")
            return True
        if not self.tty:
            print(f"  {question} skipped (no terminal; re-run with --yes)")
            return False
        hint = "[Y/n]" if default else "[y/N]"
        ans = input(f"  {question} {hint} ").strip().lower()
        return default if not ans else ans in ("y", "yes")



def remove_legacy_leftovers() -> list[str]:
    """What omawake left behind that only it used: its venv and its command link."""
    gone = []
    venv = C.legacy_venv()
    if venv.is_dir() and not venv.is_symlink() and (venv / "pyvenv.cfg").is_file():
        shutil.rmtree(venv, ignore_errors=True)
        gone.append(str(venv))
        try:
            venv.parent.rmdir()  # only when nothing else is in there
        except OSError:
            pass
    link = LINK.parent / C.LEGACY_APP
    # Only our own link: it points into the old plugin folder, now usually gone.
    if link.is_symlink() and C.LEGACY_PLUGIN_ID in os.readlink(link):
        link.unlink()
        gone.append(str(link))
    return gone

def ok(msg):
    print(f"  ✓ {msg}")


def warn(msg):
    print(f"  ! {msg}")


def head(msg):
    print(f"\n{msg}")


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _run_tool(argv: list[str]) -> None:
    """Run an Omarchy helper; say so plainly when it is missing (not Omarchy?)."""
    if not shutil.which(argv[0]):
        warn(f"{argv[0]} not found: is this Omarchy? Install it yourself and re-run setup")
        return
    subprocess.run(argv)


def service_active() -> bool:
    return _systemctl("is-active", "--quiet", C.UNIT_NAME).returncode == 0


def voxtype_active() -> bool:
    return _systemctl("is-active", "--quiet", "voxtype.service").returncode == 0 or \
        subprocess.run(["pgrep", "-x", "voxtype"], capture_output=True).returncode == 0


def venv_ok() -> bool:
    py = C.VENV_DIR / "bin" / "python"
    if not py.exists():
        return False
    r = subprocess.run([str(py), "-c", "import numpy, onnxruntime"], capture_output=True)
    return r.returncode == 0


def active_model(cfg: dict) -> tuple[str, Path] | None:
    """First wake word that will really listen (legacy: any installed one;
    never a word a running Ania Player is using)."""
    from . import aniaplayer as A
    have = M.installed(cfg)
    for w in M.effective_words(cfg, skip=A.words_in_use(cfg))[1]:
        if w["active"]:
            return w["model"], have[w["model"]]
    return None


def readiness() -> dict:
    cfg = C.load_config()
    found = active_model(cfg)
    return {
        "voxtype": shutil.which("voxtype") is not None,
        "voxtype_running": voxtype_active(),
        "venv": venv_ok(),
        "model": found[0] if found else None,
        "service": C.UNIT_FILE.exists(),
        "service_active": service_active(),
        "default_agent": _default_agent(),
    }


def _default_agent() -> str:
    try:
        r = subprocess.run(["omarchy-default-agent"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


UNIT = """[Unit]
Description=omahey: wake word for Omarchy (unofficial community plugin)
Documentation=https://github.com/rob-d3v/omahey
PartOf=graphical-session.target
After=graphical-session.target pipewire.service voxtype.service

[Service]
Type=simple
ExecStart="{bin}" daemon
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=graphical-session.target
"""


def run_setup(assume_yes=False, service=True, link=True) -> int:
    ui = UI(assume_yes)
    C.ensure_dirs()
    print("omahey setup (safe to run again)")
    problems = 0

    head("1/6 Voxtype (Omarchy dictation)")
    if not shutil.which("voxtype"):
        warn("Voxtype is not installed.")
        if ui.ask("Install it now with omarchy-voxtype-install? (asks for your password)"):
            _run_tool(["omarchy-voxtype-install"])
    if shutil.which("voxtype"):
        ok("voxtype found")
        if not voxtype_active():
            warn("the Voxtype daemon is not running")
            if ui.ask("Start it (systemctl --user enable --now voxtype)?"):
                _systemctl("enable", "--now", "voxtype.service")
        if voxtype_active():
            ok("voxtype daemon running")
        else:
            problems += 1
    else:
        warn("omahey needs Voxtype for speech-to-text; re-run setup after installing it")
        problems += 1

    head("2/6 Default agent")
    agent = _default_agent()
    if agent:
        ok(f"omarchy default agent: {agent}")
    else:
        warn("no default agent yet (Ask mode needs one): omarchy default agent <name>")

    head("3/6 Python runtime (uv)")
    if not shutil.which("uv"):
        warn("uv is not installed")
        if ui.ask("Install uv with omarchy-pkg-add uv? (asks for your password)"):
            _run_tool(["omarchy-pkg-add", "uv"])
    if shutil.which("uv"):
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(C.VENV_DIR))
        r = subprocess.run(["uv", "sync", "--frozen", "--no-dev", "--project", str(C.REPO_DIR)], env=env)
        if r.returncode == 0 and venv_ok():
            ok(f"runtime ready in {C.VENV_DIR}")
        else:
            warn("uv sync failed (see above)")
            problems += 1
    else:
        problems += 1

    head("4/6 Wake word model")
    cfg = C.load_config()
    wc = W.load(cfg)
    have = M.installed(cfg)
    for e in wc.errors:
        warn(f"config.toml: {e}")
    if wc.unsafe_reason:
        warn(f"commands disabled: {wc.unsafe_reason}")
    for spec in wc.enabled():
        if spec.model in have:
            continue
        entry = M.entry(spec.model)
        if entry and entry.get("status") == "available" and entry.get("kind", "bundle") == "bundle":
            try:
                M.download(spec.model, log=lambda s: print("  " + s))
            except Exception as e:  # noqa: BLE001
                warn(f"download failed: {e}")
        elif entry and entry.get("kind") == "upstream-head":
            warn(f"{spec.model} has a non-commercial licence: omahey models download {spec.model}")
        elif entry:
            warn(f"“{entry.get('phrase', spec.model)}” is not published yet ({entry.get('status')}).")
        else:
            warn(f"{spec.model}: no such model installed")
    from .cli import words_offline
    for w in words_offline(C.load_config()):
        if w.get("ania_player"):
            warn(f"{w['model']}: Ania Player already listens for this word, so omahey skips it"
                 " (pick another one: omahey config edit)")
    found = active_model(C.load_config())
    if found:
        ok("wake words: " + ", ".join(w["model"] for w in _words()) + f" (first: {found[0]})")
    else:
        warn("no wake word model on this machine yet; omahey will wait for one")
        print("    Until “Hey Omarchy” is published, pick one of these:")
        print("      omahey models download hey_jarvis && omahey wake hey_jarvis"
              "   # non-commercial licence, shown first")
        print("      your own wake word: https://avatar.diatech.solutions/create-wakeword"
              " (unpack it into ~/.local/share/omahey/models/)")
        problems += 1
    hint = M.donate_hint(M.installed(C.load_config()))
    if hint:
        print(f"    {hint}")

    head("5/6 Command line")
    if link:
        if LINK.is_symlink() and Path(os.readlink(LINK)) == BIN:
            ok(f"{LINK} -> {BIN}")
        elif LINK.exists() or LINK.is_symlink():
            warn(f"{LINK} exists and is not ours; leaving it alone")
        else:
            LINK.parent.mkdir(parents=True, exist_ok=True)
            LINK.symlink_to(BIN)
            ok(f"linked {LINK}")
    else:
        ok("skipped (--no-link)")

    head("6/6 Background service")
    if C.legacy_unit_is_ours():
        # The same daemon under its old name would hold the mic too.
        _systemctl("disable", "--now", C.LEGACY_UNIT_NAME)
        C.LEGACY_UNIT_FILE.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        ok(f"removed the old {C.LEGACY_UNIT_NAME}")
    for gone in remove_legacy_leftovers():
        ok(f"removed the old {gone}")
    if service:
        text = UNIT.format(bin=BIN)
        if not C.UNIT_FILE.exists() or C.UNIT_FILE.read_text() != text:
            C.atomic_write(C.UNIT_FILE, text)
            _systemctl("daemon-reload")
            ok(f"wrote {C.UNIT_FILE}")
        if venv_ok():
            _systemctl("enable", C.UNIT_NAME)
            _systemctl("restart", C.UNIT_NAME)
            if service_active():
                ok("omahey.service running")
            else:
                warn("omahey.service did not start: journalctl --user -u omahey")
                problems += 1
    else:
        ok("skipped (--no-service); run `omahey daemon` yourself")

    print("\nDone." if not problems else f"\nFinished with {problems} item(s) to fix (see ! above).")
    if found:
        print(f"Say “{found[0].replace('_', ' ')}” and start talking.")
    return 0 if not problems else 1


def _words() -> list[dict]:
    from .cli import words_offline
    return [w for w in words_offline(C.load_config()) if w["active"]]


def run_uninstall(assume_yes=False, keep_config=False) -> int:
    ui = UI(assume_yes)
    print("omahey uninstall")
    if C.UNIT_FILE.exists() or service_active():
        _systemctl("disable", "--now", C.UNIT_NAME)
        C.UNIT_FILE.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        ok("service removed")
    if LINK.is_symlink() and Path(os.readlink(LINK)) == BIN:
        LINK.unlink()
        ok("command link removed")
    for d in (C.DATA_DIR, C.RUNTIME_DIR, C.STATE_DIR, C.CACHE_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            ok(f"removed {d}")
    if C.CONFIG_DIR.exists() and not keep_config and ui.ask(f"Remove your settings in {C.CONFIG_DIR}?", False):
        shutil.rmtree(C.CONFIG_DIR, ignore_errors=True)
        ok("settings removed")
    print(f"\nTo remove the bar widget too: omarchy plugin remove {C.PLUGIN_ID}")
    return 0
