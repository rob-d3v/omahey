# SPDX-License-Identifier: Apache-2.0
"""Everything that touches the desktop: Voxtype, the Omarchy agent window,
typing (wtype), chimes (pw-play) and notifications. Stdlib only."""
from __future__ import annotations

import json
import logging
import math
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path

from . import config as C

log = logging.getLogger("omahey.desktop")

AGENT_CLASS = "org.omarchy.agent"     # fixed app-id used by `omarchy agent`


def _run(cmd: list[str], timeout: float = 10.0, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: timed out")


# ── Voxtype ────────────────────────────────────────────────────────────────
class Voxtype:
    @staticmethod
    def installed() -> bool:
        return shutil.which("voxtype") is not None

    @staticmethod
    def state() -> str:
        """idle | recording | transcribing | stopped | missing"""
        if not Voxtype.installed():
            return "missing"
        r = _run(["voxtype", "status"], timeout=3)
        out = (r.stdout or "").strip().lower()
        return out.split()[0] if r.returncode == 0 and out else "stopped"

    @staticmethod
    def start(transcript_file: Path | None = None, type_out: bool = False) -> bool:
        cmd = ["voxtype", "record", "start"]
        if transcript_file is not None:
            cmd.append(f"--file={transcript_file}")
        elif type_out:
            cmd += ["--type", "--no-auto-submit"]
        r = _run(cmd, timeout=5)
        if r.returncode != 0:
            log.error("voxtype record start failed: %s", (r.stderr or r.stdout).strip())
        return r.returncode == 0

    @staticmethod
    def stop_wait(timeout_s: int = 60) -> dict:
        """Stop and block until the transcript is final (file mode only)."""
        r = _run(["voxtype", "record", "stop", "--wait", "--json", "--timeout", str(timeout_s)],
                 timeout=timeout_s + 5)
        try:
            data = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            data = {"status": "error", "text": "", "message": (r.stderr or "").strip()}
        data["exit"] = r.returncode
        return data

    @staticmethod
    def stop() -> None:
        _run(["voxtype", "record", "stop"], timeout=5)

    @staticmethod
    def cancel() -> None:
        _run(["voxtype", "record", "cancel"], timeout=5)

    @staticmethod
    def wait_idle(timeout_s: float = 60) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if Voxtype.state() in ("idle", "stopped", "missing"):
                return
            time.sleep(0.25)


# ── Hyprland / Omarchy agent ───────────────────────────────────────────────
def default_agent() -> str:
    r = _run(["omarchy-default-agent"], timeout=5)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _clients() -> list[dict]:
    r = _run(["hyprctl", "clients", "-j"], timeout=3)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else []
    except ValueError:
        return []


def agent_window() -> str | None:
    wins = [c for c in _clients() if c.get("class") == AGENT_CLASS]
    if not wins:
        return None
    wins.sort(key=lambda c: c.get("focusHistoryID", 1 << 30))
    return wins[0].get("address")


def active_window() -> dict:
    r = _run(["hyprctl", "activewindow", "-j"], timeout=3)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except ValueError:
        return {}


def focus(address: str) -> bool:
    # Same two spellings omarchy-launch-or-focus uses (Lua dispatcher first).
    for cmd in (["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{address}" }})'],
                ["hyprctl", "dispatch", "focuswindow", f"address:{address}"]):
        r = _run(cmd, timeout=3)
        if r.returncode == 0 and (r.stdout or "").strip() == "ok":
            return True
    return False


def ensure_agent(launch_timeout_s: float = 12, ready_delay_ms: int = 1500) -> str:
    """Focus the agent window, launching `omarchy agent` first if needed.
    Returns the window address; raises RuntimeError with a user-facing reason."""
    addr = agent_window()
    if addr is None:
        addr = _launch_agent(set(), launch_timeout_s, ready_delay_ms)
    focus(addr)
    return addr


def new_agent(launch_timeout_s: float = 12, ready_delay_ms: int = 1500) -> str:
    """Open a new `omarchy agent` window, even if one is already open, and
    return its address."""
    before = {c.get("address") for c in _clients() if c.get("class") == AGENT_CLASS}
    addr = _launch_agent(before, launch_timeout_s, ready_delay_ms)
    focus(addr)
    return addr


def _launch_agent(before: set, launch_timeout_s: float, ready_delay_ms: int) -> str:
    """Start `omarchy agent` and wait for an agent window not in `before`."""
    if not default_agent():
        subprocess.Popen(["omarchy-agent", "--pick"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise RuntimeError("No default agent yet. Pick one: omarchy default agent <name>")
    log.info("launching omarchy agent")
    with open(C.LOG_FILE, "ab") as logf:
        proc = subprocess.Popen(["omarchy-agent"], start_new_session=True,
                                stdout=logf, stderr=logf, cwd=str(C.HOME))
    end = time.monotonic() + launch_timeout_s
    while time.monotonic() < end:
        new = [c for c in _clients() if c.get("class") == AGENT_CLASS and c.get("address") not in before]
        if new:
            time.sleep(ready_delay_ms / 1000.0)    # let the TUI draw its prompt
            return new[0]["address"]
        if proc.poll() not in (None, 0):
            raise RuntimeError("omarchy agent failed to start (see omahey logs)")
        time.sleep(0.2)
    raise RuntimeError("The agent window did not appear")


def type_into(address: str, text: str, submit: bool, submit_delay_ms: int = 350) -> bool:
    """Type `text` into the agent window (re-focusing it first) and press Enter."""
    if active_window().get("address") != address:
        focus(address)
        time.sleep(0.15)
    r = _run(["wtype", "-d", "2", "-"], timeout=30, input_text=text)
    if r.returncode != 0:
        log.error("wtype failed: %s", r.stderr.strip())
        return False
    if submit:
        # Pause first: TUIs such as codex treat Enter inside a fast burst of
        # keystrokes as a pasted newline instead of "send".
        _run(["wtype", "-s", str(int(submit_delay_ms)), "-k", "Return"], timeout=10)
    return True


# ── feedback ───────────────────────────────────────────────────────────────
_TONES = {
    "wake": [(880, 0.07), (1320, 0.09)],
    "done": [(1320, 0.06), (880, 0.08)],
    "bye": [(660, 0.08), (440, 0.12)],
    "error": [(330, 0.18)],
}


def _tone_file(name: str) -> Path:
    path = C.RUNTIME_DIR / f"chime-{name}.wav"
    if path.exists():
        return path
    sr, frames = 22050, bytearray()
    for freq, dur in _TONES[name]:
        n = int(sr * dur)
        for i in range(n):
            env = min(1.0, i / (sr * 0.008), (n - i) / (sr * 0.02))
            frames += struct.pack("<h", int(0.25 * 32767 * env * math.sin(2 * math.pi * freq * i / sr)))
    C.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return path


def chime(name: str) -> None:
    if not shutil.which("pw-play"):
        return
    try:
        subprocess.Popen(["pw-play", "--media-role", "Notification", str(_tone_file(name))],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def notify(title: str, body: str = "", urgent: bool = False) -> None:
    if shutil.which("notify-send"):
        cmd = ["notify-send", "-a", "omahey", "-i", "audio-input-microphone"]
        if urgent:
            cmd += ["-u", "critical"]
        subprocess.Popen(cmd + [title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
