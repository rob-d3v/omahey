# SPDX-License-Identifier: Apache-2.0
"""Microphone capture through PipeWire's `pw-record` (16 kHz mono s16 raw).

A pipe from pw-record avoids PortAudio/ALSA quirks, shares the source with
Voxtype and other apps, and shows up as a normal PipeWire stream named
"omawake" (so the mic indicator and pavucontrol show who is listening).
"""
from __future__ import annotations

import shutil
import subprocess


class MicStream:
    def __init__(self, target: str = ""):
        self.target = target
        self.proc: subprocess.Popen | None = None

    def open(self):
        exe = shutil.which("pw-record")
        if not exe:
            raise RuntimeError("pw-record not found (install pipewire)")
        cmd = [exe, "--rate", "16000", "--channels", "1", "--format", "s16", "--raw",
               "--latency", "80ms", "--media-role", "Communication",
               "-P", "{ node.name = \"omawake\" node.description = \"omawake wake word\" "
                     "application.name = \"omawake\" }"]
        if self.target:
            cmd += ["--target", self.target]
        cmd.append("-")
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
