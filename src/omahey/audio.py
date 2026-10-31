# SPDX-License-Identifier: Apache-2.0
"""Microphone capture through PipeWire's `pw-record` (16 kHz mono s16 raw).

A pipe from pw-record avoids PortAudio/ALSA quirks, shares the source with
Voxtype and other apps, and shows up as a normal PipeWire stream named
"omahey" (so the mic indicator and pavucontrol show who is listening).
"""
from __future__ import annotations

import shutil
import subprocess


def list_sinks() -> set[str]:
    """Names of the PipeWire/Pulse sinks (empty if pactl is missing)."""
    exe = shutil.which("pactl")
    if not exe:
        return set()
    try:
        out = subprocess.run([exe, "list", "short", "sinks"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {cols[1] for cols in (ln.split("\t") for ln in out.splitlines()) if len(cols) > 1}


def resolve_target(target: str, sinks: set[str] | None = None) -> tuple[str, bool]:
    """`audio_target` -> (pw-record --target, capture a sink's monitor?).

    `pw-record --target foo.monitor` records silence: to capture what a sink
    plays, the stream must target the sink itself with
    `stream.capture.sink=true`. So "foo.monitor" and a bare sink name both
    become ("foo", True); anything else (a source) is passed through."""
    target = (target or "").strip()
    if not target:
        return "", False
    if target.endswith(".monitor"):
        return target[: -len(".monitor")], True
    if sinks is None:
        sinks = list_sinks()
    return target, target in sinks


def record_cmd(exe: str, target: str = "", sinks: set[str] | None = None) -> list[str]:
    tgt, monitor = resolve_target(target, sinks)
    props = ("node.name = \"omahey\" node.description = \"omahey wake word\" "
             "application.name = \"omahey\"")
    if monitor:
        props += " stream.capture.sink = true"
    cmd = [exe, "--rate", "16000", "--channels", "1", "--format", "s16", "--raw",
           "--latency", "80ms", "--media-role", "Communication",
           "-P", "{ " + props + " }"]
    if tgt:
        cmd += ["--target", tgt]
    cmd.append("-")
    return cmd


class MicStream:
    def __init__(self, target: str = ""):
        self.target = target
        self.proc: subprocess.Popen | None = None

    def open(self):
        exe = shutil.which("pw-record")
        if not exe:
            raise RuntimeError("pw-record not found (install pipewire)")
        cmd = record_cmd(exe, self.target)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     bufsize=0)
        return self.proc.stdout

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
