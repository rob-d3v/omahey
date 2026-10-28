# SPDX-License-Identifier: Apache-2.0
"""The omawake daemon.

Threads:
  audio   pw-record -> wake core (+ Silero VAD) -> endpointer; posts wake events
  worker  runs one session at a time (Ask / Dictate), drives Voxtype + agent
  control unix socket for the CLI and the bar widget (JSON lines)

The bar widget reads $XDG_RUNTIME_DIR/omawake/state.json (written on every
state change) and calls the CLI for actions.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import signal
import socket
import threading
import time
from pathlib import Path

from . import config as C
from . import desktop as D
from . import models as M
from . import text as T
from .audio import MicStream
from .engine import Detector, Endpointer, pcm_chunks

log = logging.getLogger("omawake")
VERSION = "0.1.0"


def setup_logging(verbose: bool = False) -> None:
    C.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(C.LOG_FILE, maxBytes=1_000_000, backupCount=2)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)


class Aborted(Exception):
    pass


class Daemon:
    def __init__(self):
        self.cfg = C.load_config()
        self.running = True
        self.paused = bool(self.cfg.get("start_paused", False))
        self.state = "starting"
        self.message = ""
        self.wake_id = ""
        self.phrase = ""
        self.detector: Detector | None = None
        self.need_rebuild = True
        self.endpointer = Endpointer(int(self.cfg["end_silence_ms"]))
        self.events: queue.Queue = queue.Queue()
        self.abort = threading.Event()
        self.busy = False
        self.mic = MicStream(self.cfg.get("audio_target", ""))
        self.mic_open = False
        self.last_score = 0.0
        self.turn_no = 0
        self.lock = threading.Lock()

    # ── state ──────────────────────────────────────────────────────────────
    def set_state(self, state: str, message: str = "") -> None:
        with self.lock:
            self.state, self.message = state, message
        log.info("state=%s %s", state, message)
        self.publish()

    def snapshot(self) -> dict:
        with self.lock:
            shown = "paused" if self.paused and not self.busy else self.state
            return {
                "state": shown, "message": self.message, "mode": self.cfg["mode"],
                "sensitivity": self.cfg["sensitivity"], "wake_word": self.wake_id,
                "phrase": self.phrase, "paused": self.paused, "conversation": bool(self.cfg["conversation"]),
                "mic": self.mic_open, "pid": os.getpid(), "version": VERSION, "updated": time.time(),
            }

    def publish(self) -> None:
        try:
            C.atomic_write(C.STATE_FILE, json.dumps(self.snapshot()) + "\n")
        except OSError as e:
            log.warning("cannot write state file: %s", e)

    # ── detector ───────────────────────────────────────────────────────────
    def rebuild(self) -> bool:
        self.need_rebuild = False
        found = M.resolve(self.cfg)
        if not found:
            self.detector = None
            self.wake_id = self.phrase = ""
            self.set_state("setup", "No wake word model installed. Run: omawake setup")
            return False
        wid, path = found
        try:
            shift = C.SENSITIVITY_SHIFT.get(self.cfg["sensitivity"], 0.0)
            self.detector = Detector(path, shift)
        except Exception as e:  # noqa: BLE001 - surface any model problem in the widget
            log.exception("loading %s failed", path)
            self.detector = None
            self.set_state("error", f"Could not load wake word {wid}: {e}")
            return False
        self.wake_id, self.phrase = wid, self.detector.phrase
        if wid != self.cfg.get("wake_word"):
            log.info("wake word %r not installed, using %r", self.cfg.get("wake_word"), wid)
        log.info("detector: %s (%s)", self.detector.describe(), path)
        if not self.busy:
            self.set_state("idle", f"Say “{self.phrase}”")
        return True

    # ── audio thread ───────────────────────────────────────────────────────
    def audio_loop(self) -> None:
        backoff = 1.0
        while self.running:
            if self.need_rebuild:
                self.rebuild()
            if self.paused or self.detector is None:
                time.sleep(0.2)
                if self.detector is None and not self.need_rebuild:
                    time.sleep(5)
                    self.need_rebuild = True
                continue
            try:
                stream = self.mic.open()
            except RuntimeError as e:
                self.set_state("error", str(e))
                time.sleep(5)
                continue
            self.mic_open = True
            self.publish()
            self.detector.reset()
            frames = 0
            try:
                for chunk in pcm_chunks(stream):
                    if not self.running or self.paused or self.need_rebuild:
                        break
                    out = self.detector.process(chunk)
                    f = out["features"]
                    self.endpointer.update(f.vad_prob, f.rms, f.bg_rms)
                    sc = out["per_wake"][0].get("score")
                    self.last_score = sc or 0.0
                    frames += 1
                    if out["event"] and not self.busy:
                        log.info("wake: %s", out["event"])
                        self.events.put(("wake", out["event"]))
                    if frames == 30 and self.state == "error":
                        self.set_state("idle", f"Say “{self.phrase}”")
                    backoff = 1.0
            except Exception:  # noqa: BLE001
                log.exception("audio loop crashed")
            finally:
                self.mic.close()
                self.mic_open = False
                self.publish()
            if self.running and not self.paused and not self.need_rebuild:
                self.set_state("error", "Microphone stream ended; retrying")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    # ── worker ─────────────────────────────────────────────────────────────
    def worker_loop(self) -> None:
        while self.running:
            try:
                kind, payload = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "quit":
                return
            if kind != "wake" or self.paused:
                continue
            self.busy = True
            self.abort.clear()
            try:
                mode = payload.get("mode") or self.cfg["mode"]
                if mode == "dictate":
                    self.dictate_session()
                else:
                    self.ask_session(payload.get("text"))
            except Aborted:
                log.info("session aborted")
                D.Voxtype.cancel()
            except Exception as e:  # noqa: BLE001
                log.exception("session failed")
                D.chime("error")
                D.notify("omawake", str(e))
                self.set_state("error", str(e))
                time.sleep(2)
            finally:
                self.busy = False
                if self.detector is not None:
                    self.detector.reset()   # drop audio heard during the session
                if self.paused:
                    self.set_state("paused", "Paused")
                elif self.detector is not None:
                    self.set_state("idle", f"Say “{self.phrase}”")

    def _check_abort(self) -> None:
        if self.abort.is_set() or not self.running:
            raise Aborted()

    def _wait_speech(self, wait_s: float) -> bool:
        """Block until the utterance ends. False = nobody spoke within wait_s."""
        max_utt = float(self.cfg["max_utterance_s"])
        while True:
            self._check_abort()
            started, ended, t = self.endpointer.snapshot()
            if not started and t > wait_s:
                return False
            if started and (ended or t > wait_s + max_utt):
                return True
            time.sleep(0.04)

    def _arm(self) -> None:
        # Skip the chime and the tail of the wake word before listening.
        time.sleep(0.25)
        self.endpointer = Endpointer(int(self.cfg["end_silence_ms"]))

    def record_turn(self, wait_s: float, state: str, message: str) -> str | None:
        """One Voxtype recording in file mode. None = no speech (cancelled)."""
        self.turn_no += 1
        out = C.RUNTIME_DIR / f"turn-{self.turn_no % 4}.txt"
        out.unlink(missing_ok=True)
        if D.Voxtype.state() in ("missing", "stopped"):
            raise RuntimeError("Voxtype is not running. Run: omawake setup")
        if not D.Voxtype.start(transcript_file=out):
            raise RuntimeError("voxtype record start failed")
        self.set_state(state, message)
        self._arm()
        try:
            spoke = self._wait_speech(wait_s)
        except Aborted:
            D.Voxtype.cancel()
            raise
        if not spoke:
            D.Voxtype.cancel()
            return None
        self.set_state("transcribing", "Transcribing…")
        res = D.Voxtype.stop_wait(timeout_s=60)
        out.unlink(missing_ok=True)
        log.info("transcript: %s", {k: res.get(k) for k in ("status", "text", "exit")})
        return str(res.get("text") or "").strip()

    def ask_session(self, injected: str | None = None) -> None:
        cfg = dict(self.cfg)
        if injected:
            cfg["conversation"] = False
        if cfg.get("chime", True):
            D.chime("wake")
        # Find or launch the agent while the user is already talking.
        box: dict = {}

        def prep():
            try:
                box["addr"] = D.ensure_agent(float(cfg["agent_launch_timeout_s"]),
                                             int(cfg["agent_ready_delay_ms"]))
            except Exception as e:  # noqa: BLE001
                box["err"] = e
        th = threading.Thread(target=prep, daemon=True)
        th.start()

        wait_s = float(cfg["first_speech_timeout_s"])
        state, msg = "listening", "Listening…"
        first = True
        while True:
            if injected:
                text, injected = injected, None
            else:
                text = self.record_turn(wait_s, state, msg)
            if text is None:
                if cfg.get("chime", True):
                    D.chime("bye")
                return
            text = T.strip_wake(text, self.phrase)
            if T.is_dismiss(text, cfg["dismiss_phrases"]):
                log.info("dismissed by %r", text)
                if cfg.get("chime", True):
                    D.chime("bye")
                return
            if not T.is_noise(text):
                th.join(timeout=float(cfg["agent_launch_timeout_s"]) + 5)
                if "err" in box:
                    raise box["err"]
                addr = box.get("addr")
                if not addr or D.agent_window() is None:
                    box.clear()
                    prep()
                    if "err" in box:
                        raise box["err"]
                    addr = box["addr"]
                self.set_state("typing", "Sending to agent…")
                if not D.type_into(addr, text, submit=True, submit_delay_ms=int(cfg["submit_delay_ms"])):
                    raise RuntimeError("Could not type into the agent window (wtype)")
                if cfg.get("chime", True):
                    D.chime("done")
                first = False
            elif first and not cfg.get("conversation", True):
                return
            if not cfg.get("conversation", True):
                return
            wait_s = float(cfg["conversation_timeout_s"])
            state, msg = "conversation", "Conversation: keep talking, or say “that's all”"

    def dictate_session(self) -> None:
        if self.cfg.get("chime", True):
            D.chime("wake")
        if D.Voxtype.state() in ("missing", "stopped"):
            raise RuntimeError("Voxtype is not running. Run: omawake setup")
        if not D.Voxtype.start(type_out=True):
            raise RuntimeError("voxtype record start failed")
        self.set_state("listening", "Dictating at the cursor…")
        self._arm()
        try:
            spoke = self._wait_speech(float(self.cfg["first_speech_timeout_s"]))
        except Aborted:
            D.Voxtype.cancel()
            raise
        if not spoke:
            D.Voxtype.cancel()
            return
        self.set_state("transcribing", "Typing…")
        D.Voxtype.stop()
        time.sleep(0.3)
        D.Voxtype.wait_idle(60)

    # ── control socket ─────────────────────────────────────────────────────
    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, **self.snapshot(), "score": round(self.last_score, 4),
                    "detector": self.detector.describe() if self.detector else None}
        if cmd in ("pause", "resume", "toggle"):
            want = {"pause": True, "resume": False}.get(cmd, not self.paused)
            self.paused = want
            if want:
                self.abort.set()
                self.set_state("paused", "Paused")
            else:
                self.need_rebuild = self.detector is None
                self.set_state("idle" if self.detector else self.state, f"Say “{self.phrase}”" if self.detector else self.message)
            return {"ok": True, "paused": self.paused}
        if cmd == "reload":
            old = (self.cfg.get("wake_word"), self.cfg.get("audio_target"), json.dumps(self.cfg.get("model_dirs")))
            self.cfg = C.load_config()
            new = (self.cfg.get("wake_word"), self.cfg.get("audio_target"), json.dumps(self.cfg.get("model_dirs")))
            self.mic.target = self.cfg.get("audio_target", "")
            if old != new or self.detector is None:
                self.need_rebuild = True
            elif self.detector is not None:
                self.detector.set_sensitivity(C.SENSITIVITY_SHIFT.get(self.cfg["sensitivity"], 0.0))
                log.info("detector: %s", self.detector.describe())
            self.publish()
            return {"ok": True}
        if cmd == "trigger":
            if self.busy:
                return {"ok": False, "error": "busy"}
            if self.paused:
                return {"ok": False, "error": "paused"}
            self.events.put(("wake", {"wake": "manual", "mode": req.get("mode"), "text": req.get("text")}))
            return {"ok": True}
        if cmd == "cancel":
            self.abort.set()
            return {"ok": True}
        if cmd == "quit":
            self.stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def control_loop(self) -> None:
        path = C.SOCKET_FILE
        path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o600)
        srv.listen(4)
        srv.settimeout(0.5)
        self._srv = srv
        while self.running:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(2)
                try:
                    data = b""
                    while not data.endswith(b"\n") and len(data) < 65536:
                        part = conn.recv(4096)
                        if not part:
                            break
                        data += part
                    resp = self.handle(json.loads(data.decode() or "{}"))
                except Exception as e:  # noqa: BLE001
                    resp = {"ok": False, "error": str(e)}
                try:
                    conn.sendall((json.dumps(resp) + "\n").encode())
                except OSError:
                    pass
        srv.close()
        path.unlink(missing_ok=True)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def stop(self, *_):
        self.running = False
        self.abort.set()
        self.events.put(("quit", None))

    def run(self) -> int:
        C.ensure_dirs()
        if C.SOCKET_FILE.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(C.SOCKET_FILE))
                s.close()
                log.error("omawake is already running")
                return 1
            except OSError:
                pass
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        threads = [threading.Thread(target=self.control_loop, name="control", daemon=True),
                   threading.Thread(target=self.worker_loop, name="worker", daemon=True),
                   threading.Thread(target=self.audio_loop, name="audio", daemon=True)]
        if self.paused:
            self.set_state("paused", "Paused")
        for t in threads:
            t.start()
        log.info("omawake %s started (pid %d)", VERSION, os.getpid())
        while self.running:
            time.sleep(0.3)
        self.mic.close()
        if self.busy:
            D.Voxtype.cancel()
        for t in threads:
            t.join(timeout=3)
        with self.lock:
            self.state, self.message = "stopped", "Not running"
        self.mic_open = False
        self.publish()
        log.info("omawake stopped")
        return 0


def main(verbose: bool = False) -> int:
    setup_logging(verbose)
    return Daemon().run()
