# SPDX-License-Identifier: Apache-2.0
"""The omahey daemon.

Threads:
  audio   pw-record -> shared front-end (mel + embedding + Silero) -> N wake
          heads -> arbiter -> endpointer; posts one wake event per utterance
  worker  runs one session at a time for the word that won (ask, dictate,
          command, agent-prompt), drives Voxtype, the agent and commands
  control unix socket for the CLI and the bar widget (JSON lines)

The bar widget reads $XDG_RUNTIME_DIR/omahey/state.json (written on every
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

from . import aniaplayer as A
from . import commands as K
from . import config as C
from . import desktop as D
from . import models as M
from . import text as T
from . import wakeconf as W
from .arbiter import Arbiter
from .audio import MicStream
from .engine import Endpointer, MultiDetector, WordSpec, pcm_chunks

log = logging.getLogger("omahey")
VERSION = "0.3.0"


def setup_logging(verbose: bool = False) -> None:
    # Everything the daemon writes (log, rotated logs, turn files) is private
    # to the user, whatever the session umask is.
    os.umask(0o077)
    C.ensure_dirs()
    for f in C.STATE_DIR.glob(C.LOG_FILE.name + "*"):
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
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
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.cfg = C.load_config()
        self.wc = W.load(self.cfg)
        self.arbiter = Arbiter()
        self.words: list[dict] = []
        self.scores: dict[str, float] = {}
        self.last_result = ""
        self.running = True
        self.paused = bool(self.cfg.get("start_paused", False))
        self.state = "starting"
        self.message = ""
        self.wake_id = ""
        self.phrase = ""
        self.detector: MultiDetector | None = None
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
        self.player_words: set[str] = set()     # left to a running Ania Player
        self._told_player: set[str] = set()     # explained once per word

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
                "words": self.words, "config_source": self.wc.source,
                "config_errors": self.wc.errors[:5], "commands_allowed": self.wc.commands_allowed,
                "unsafe_reason": self.wc.unsafe_reason, "last_result": self.last_result,
                "dry_run": self.dry_run, "ania_player_words": sorted(self.player_words),
            }

    def publish(self) -> None:
        try:
            C.atomic_write(C.STATE_FILE, json.dumps(self.snapshot()) + "\n")
        except OSError as e:
            log.warning("cannot write state file: %s", e)

    # ── detector ───────────────────────────────────────────────────────────
    def idle_message(self) -> str:
        names = [f"“{w['phrase']}”" for w in self.words if w["active"]]
        if not names:
            return "No wake word active"
        return "Say " + (", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1 else names[0])

    def rebuild(self) -> bool:
        self.need_rebuild = False
        self.wc = W.load(self.cfg)
        for e in self.wc.errors:
            log.error("config: %s", e)
        for w in self.wc.warnings:
            log.warning("config: %s", w)
        if self.wc.unsafe_reason:
            log.error("config.toml is not safe, commands are disabled: %s", self.wc.unsafe_reason)
        have = M.installed(self.cfg)
        self.player_words = A.words_in_use(self.cfg)
        self.wc, words = M.effective_words(self.cfg, self.wc, self.player_words)
        if words and words[0]["configured"]:
            log.info("wake word %r not usable here, using %r", words[0]["configured"], words[0]["model"])
        self.explain_player_words(words)
        gshift = C.SENSITIVITY_SHIFT.get(self.cfg["sensitivity"], 0.0)
        specs = []
        for s, w in zip(self.wc.specs, words):
            if w["active"]:
                shift = C.SENSITIVITY_SHIFT.get(s.sensitivity, gshift) if s.sensitivity else gshift
                specs.append(WordSpec(s.model, have[s.model], shift, s.threshold, s.cooldown_s))
        self.words = words
        self.arbiter = Arbiter(self.wc.arbitration_ms / 1000.0, self.wc.settle_ms / 1000.0,
                               {s.model: s.cooldown_s for s in self.wc.specs if s.cooldown_s is not None})
        if not specs:
            self.detector = None
            self.wake_id = self.phrase = ""
            shared = [w["phrase"] for w in words if w["ania_player"] and w["enabled"]]
            if shared:
                why = f"“{shared[0]}” belongs to Ania Player; pick another word: omahey config edit"
            elif not any(w["installed"] for w in words):
                why = "No wake word installed. Run: omahey setup"
            else:
                why = "All wake words are turned off"
            self.set_state("setup", why)
            return False
        try:
            t0 = time.monotonic()
            self.detector = MultiDetector(specs)
        except Exception as e:  # noqa: BLE001 - surface any model problem in the widget
            log.exception("loading wake words failed")
            self.detector = None
            self.set_state("error", f"Could not load wake words: {e}")
            return False
        labels = {s.model: s.label for s in self.wc.specs}
        for w in self.words:
            if w["active"] and not labels.get(w["model"]):
                w["phrase"] = self.detector.phrase_of(w["model"])
        self.wake_id = ",".join(self.detector.names)
        self.phrase = self.detector.phrase_of(self.detector.names[0])
        log.info("detector (%.2fs): %s", time.monotonic() - t0, self.detector.describe())
        if not self.busy:
            self.set_state("idle", self.idle_message())
        return True

    def explain_player_words(self, words: list[dict]) -> None:
        """Log and notify once per word that omahey leaves to Ania Player."""
        for w in words:
            if not w["ania_player"] or w["model"] in self._told_player:
                continue
            self._told_player.add(w["model"])
            log.warning("skipping wake word %r: Ania Player (aniaplayer.service) already listens for it, "
                        "so one phrase would wake both. Set \"skip_ania_player_words\": false in %s "
                        "to listen anyway.", w["model"], C.CONFIG_FILE)
            D.notify(f"omahey leaves “{w['phrase']}” to Ania Player",
                     "Ania Player already listens for this wake word, so omahey skips it "
                     "to avoid waking both. Pick another word: omahey config edit")

    def watch_player(self) -> None:
        """Re-check Ania Player's words (started, stopped, changed) now and then."""
        try:
            now = A.words_in_use(self.cfg)
        except Exception:  # noqa: BLE001 - never kill the daemon over this
            return
        if now != self.player_words:
            log.info("Ania Player wake words changed: %s -> %s", sorted(self.player_words), sorted(now))
            self.need_rebuild = True

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
                    self.scores = {r["wake"]: r.get("score") or 0.0 for r in out["per_wake"]}
                    self.last_score = max(self.scores.values(), default=0.0)
                    frames += 1
                    now = time.monotonic()
                    if not self.busy:
                        for ev in out["events"]:
                            if self.arbiter.offer(ev["wake"], ev["margin"], now, ev):
                                log.info("candidate: %s", ev)
                        win = self.arbiter.poll(now)
                        if win:
                            log.info("wake: %s%s", win["wake"],
                                     f" (beat {', '.join(win['rivals'])})" if win["rivals"] else "")
                            self.events.put(("wake", win))
                    if frames == 30 and self.state == "error":
                        self.set_state("idle", self.idle_message())
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
                self.dispatch(payload)
            except Aborted:
                log.info("session aborted")
                D.Voxtype.cancel()
            except Exception as e:  # noqa: BLE001
                log.exception("session failed")
                D.chime("error")
                D.notify("omahey", str(e))
                self.set_state("error", str(e))
                time.sleep(2)
            finally:
                self.busy = False
                if self.detector is not None:
                    self.detector.reset()   # drop audio heard during the session
                self.arbiter.pending, self.arbiter.opened = [], None
                if self.paused:
                    self.set_state("paused", "Paused")
                elif self.detector is not None:
                    self.set_state("idle", self.idle_message())

    def spec_for(self, payload: dict) -> W.WakeSpec:
        name = payload.get("wake")
        spec = self.wc.get(name) if name and name != "manual" else None
        if spec is None:
            spec = next(iter(self.wc.enabled()), None) or W.WakeSpec(model=name or "manual")
        return spec

    def dispatch(self, payload: dict) -> None:
        spec = self.spec_for(payload)
        action = payload.get("mode") or spec.action
        phrase = self.detector.phrase_of(spec.model) if self.detector else spec.model.replace("_", " ")
        log.info("action %s for %s", action, spec.model)
        if action == "dictate":
            self.dictate_session()
        elif action == "command":
            self.command_session(spec, phrase, payload.get("text"))
        elif action == "agent-prompt":
            self.agent_prompt_session(phrase, payload.get("text"))
        else:
            self.ask_session(payload.get("text"), phrase)

    def report(self, title: str, body: str = "", ok: bool = True) -> None:
        """Result of a command, shown in the widget and as a notification."""
        self.last_result = f"{title}: {body}" if body else title
        log.info("result: %s", self.last_result)
        D.notify(title, body, urgent=not ok)
        if self.cfg.get("chime", True):
            D.chime("done" if ok else "error")

    def _listen_phrase(self, phrase: str, injected: str | None, message: str) -> str | None:
        """One short transcript after the wake word (None = nothing said)."""
        if injected is not None:
            text = injected
        else:
            text = self.record_turn(float(self.cfg["first_speech_timeout_s"]), "listening", message)
        if text is None:
            return None
        text = T.strip_wake(text, phrase)
        if T.is_noise(text) or T.is_dismiss(text, self.cfg["dismiss_phrases"]):
            return None
        return text

    def command_session(self, spec: W.WakeSpec, phrase: str, injected: str | None = None) -> None:
        if not self.wc.commands_allowed:
            self.report("omahey: commands disabled", self.wc.unsafe_reason, ok=False)
            return
        timeout = spec.timeout_s or self.wc.command_timeout_s
        if spec.command:
            if self.cfg.get("chime", True):
                D.chime("wake")
            self.set_state("running", spec.command)
            r = K.run(spec.command, timeout, self.dry_run, why=f"wake {spec.model}")
            self.report(f"omahey: {spec.command}", "done" if r["ok"] else r.get("error", "failed"), r["ok"])
            return
        if self.cfg.get("chime", True) and injected is None:
            D.chime("wake")
        text = self._listen_phrase(phrase, injected, "Say a command…")
        if text is None:
            if self.cfg.get("chime", True):
                D.chime("bye")
            return
        hit = K.match(text, spec.commands, self.wc.match_cutoff)
        if hit:
            said, cmd, score = hit
            self.set_state("running", f"{said} → {cmd}")
            log.info("voice command -> %r (%.2f)", said, score)
            log.debug("voice command heard %r", text)
            r = K.run(cmd, timeout, self.dry_run, why=f"{spec.model}: {said!r}")
            self.report(f"omahey: {said}", cmd if r["ok"] else f"{cmd}: {r.get('error')}", r["ok"])
        elif spec.fallback == "agent":
            r = self._agent_prompt(text)
            self.report("omahey: asked the agent", "" if r["ok"] else r.get("error", ""), r["ok"])
        else:
            self.report("omahey: no matching command", "Try: omahey match \"<phrase>\"", ok=False)

    def agent_prompt_session(self, phrase: str, injected: str | None = None) -> None:
        if self.cfg.get("chime", True) and injected is None:
            D.chime("wake")
        text = self._listen_phrase(phrase, injected, "Listening…")
        if text is None:
            if self.cfg.get("chime", True):
                D.chime("bye")
            return
        r = self._agent_prompt(text)
        self.report("omahey: new agent prompt", "" if r["ok"] else r.get("error", ""), r["ok"])

    def _agent_prompt(self, text: str) -> dict:
        return K.agent_prompt(text, self.dry_run,
                              launch_timeout_s=float(self.cfg["agent_launch_timeout_s"]),
                              ready_delay_ms=int(self.cfg["agent_ready_delay_ms"]),
                              submit_delay_ms=int(self.cfg["submit_delay_ms"]))

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
            raise RuntimeError("Voxtype is not running. Run: omahey setup")
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
        text = str(res.get("text") or "").strip()
        # What was said stays out of the log unless you ask for it (-v).
        log.info("transcript: status=%s exit=%s, %d chars", res.get("status"), res.get("exit"), len(text))
        log.debug("transcript text: %r", text)
        return text

    def ask_session(self, injected: str | None = None, phrase: str = "") -> None:
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
            text = T.strip_wake(text, phrase or self.phrase)
            if T.is_dismiss(text, cfg["dismiss_phrases"]):
                log.info("dismissed")
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
            raise RuntimeError("Voxtype is not running. Run: omahey setup")
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
                    "scores": {k: round(v, 4) for k, v in self.scores.items()},
                    "detector": self.detector.describe() if self.detector else None}
        if cmd in ("pause", "resume", "toggle"):
            want = {"pause": True, "resume": False}.get(cmd, not self.paused)
            self.paused = want
            if want:
                self.abort.set()
                self.set_state("paused", "Paused")
            else:
                self.need_rebuild = self.detector is None
                self.set_state("idle" if self.detector else self.state, self.idle_message() if self.detector else self.message)
            return {"ok": True, "paused": self.paused}
        if cmd == "reload":
            old = self._signature()
            self.cfg = C.load_config()
            self.mic.target = self.cfg.get("audio_target", "")
            new_wc = W.load(self.cfg)
            if old != self._signature(new_wc) or self.detector is None:
                self.need_rebuild = True
            else:
                self.wc = new_wc          # actions/commands changed only: no model reload
                self.rebuild_words_only()
            self.publish()
            return {"ok": True, "errors": new_wc.errors, "unsafe_reason": new_wc.unsafe_reason}
        if cmd == "trigger":
            if self.busy:
                return {"ok": False, "error": "busy"}
            if self.paused:
                return {"ok": False, "error": "paused"}
            self.events.put(("wake", {"wake": req.get("wake") or "manual", "mode": req.get("mode"),
                                      "text": req.get("text")}))
            return {"ok": True}
        if cmd == "cancel":
            self.abort.set()
            return {"ok": True}
        if cmd == "quit":
            self.stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _signature(self, wc: W.WakeConfig | None = None) -> str:
        """What needs a model reload when it changes."""
        wc = wc or self.wc
        words = [(s.model, s.enabled, s.threshold, s.sensitivity, s.cooldown_s) for s in wc.specs]
        return json.dumps([words, wc.arbitration_ms, wc.settle_ms, self.cfg.get("sensitivity"),
                           self.cfg.get("audio_target"), self.cfg.get("model_dirs"),
                           self.cfg.get("skip_ania_player_words"),
                           self.cfg.get("wake_word") if wc.source == "legacy" else None])

    def rebuild_words_only(self) -> None:
        acts = {s.model: s for s in self.wc.specs}
        for w in self.words:
            s = acts.get(w["model"])
            if s:
                w["action"], w["describe"] = s.action, s.describe()
        log.info("config reloaded (actions only)")

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
                log.error("omahey is already running")
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
        log.info("omahey %s started (pid %d)", VERSION, os.getpid())
        next_watch = time.monotonic() + 20
        while self.running:
            time.sleep(0.3)
            if time.monotonic() >= next_watch:
                next_watch = time.monotonic() + 20
                self.watch_player()
        self.mic.close()
        if self.busy:
            D.Voxtype.cancel()
        for t in threads:
            t.join(timeout=3)
        with self.lock:
            self.state, self.message = "stopped", "Not running"
        self.mic_open = False
        self.publish()
        log.info("omahey stopped")
        return 0


def main(verbose: bool = False, dry_run: bool = False) -> int:
    setup_logging(verbose)
    if dry_run:
        log.warning("dry run: commands and agent prompts are logged, not run")
    return Daemon(dry_run).run()
