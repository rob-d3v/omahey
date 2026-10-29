# SPDX-License-Identifier: Apache-2.0
"""Builds the wake detector from a bundle and tracks speech for endpointing."""
from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

import numpy as np

from . import models as M
from . import wake_core as ref

log = logging.getLogger("omawake.engine")

CHUNK = ref.CHUNK          # 1280 samples = 80 ms @ 16 kHz
FRAME_MS = 80


def _sidecar_verifier(bundle: Path, man: dict) -> dict | None:
    """`<bundle>.verifier/verifier.json` next to a sealed bundle (Ania Player layout)."""
    if isinstance(man.get("verifier"), dict) and man["verifier"].get("file"):
        return None
    for cand in (bundle.parent / (bundle.name + ".verifier"),
                 bundle.parent / (str(man.get("model_name", "")) + ".verifier")):
        meta = cand / "verifier.json"
        if not meta.is_file():
            continue
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            vfile = cand / str(info.get("file", "verifier.onnx"))
            if not vfile.is_file():
                return None
            return {"file": str(vfile), "threshold": float(info["threshold"]),
                    "window": int(info.get("window", 24)),
                    "first_stage_logit_drop": float(info.get("first_stage_logit_drop", 0.0))}
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("unreadable verifier sidecar %s: %s", cand, e)
    return None


class PaddedVerifier(ref.OnnxVerifier):
    """A candidate can fire before `window` embeddings exist (right after the
    stream starts or resets). Repeat the oldest row instead of crashing."""

    def score(self, emb: np.ndarray) -> float:
        if emb.shape[0] < self.window:
            pad = np.repeat(emb[:1], self.window - emb.shape[0], axis=0)
            emb = np.concatenate([pad, emb])
        return super().score(emb)


FRONTEND_ALIASES = M.FRONTEND_ALIASES
_SHA_CACHE: dict[str, str] = {}


def frontend_id(path: Path) -> str:
    k = str(path)
    if k not in _SHA_CACHE:
        _SHA_CACHE[k] = ref._sha(path)
    h = _SHA_CACHE[k]
    return FRONTEND_ALIASES.get(h, h)


def prepare_bundle(bundle: Path) -> dict:
    """Manifest + resolved files for one bundle, with the Ania Player policy
    for old bundles (no `decision` block -> ROBUST_PROFILE) and the sidecar
    verifier (whose first-stage logit drop lowers the head threshold)."""
    bundle = Path(bundle)
    man = ref.load_manifest(bundle)
    raw = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(raw.get("decision"), dict):
        prof = copy.deepcopy(ref.ROBUST_PROFILE)
        man["decision"] = ref._deep_merge(man["decision"], prof["decision"])
        if "dc_removal" not in (raw.get("runtime_contract") or {}):
            man["runtime_contract"]["dc_removal"] = prof["runtime_contract"]["dc_removal"]
    drop = 0.0
    side = _sidecar_verifier(bundle, man)
    if side:
        man["verifier"] = side
        drop = side["first_stage_logit_drop"]
    vad_path = None
    try:
        vad_path = ref.resolve_file(bundle, man["decision"]["vad"].get("model", "models/silero_vad.onnx"))
    except FileNotFoundError:
        pass
    vm = man.get("verifier")
    return {"man": man, "drop": drop, "vad": vad_path,
            "mel": ref.resolve_file(bundle, man["frontend"]["melspectrogram"]),
            "emb": ref.resolve_file(bundle, man["frontend"]["embedding"]),
            "head": ref.resolve_file(bundle, man["head_model"]),
            "verifier": (ref.resolve_file(bundle, vm["file"]), vm) if isinstance(vm, dict) and vm.get("file") else None}


class Detector(ref.WakeCore):
    """`WakeCore` for one bundle, with the Ania Player policy for old bundles
    (no `decision` block -> ROBUST_PROFILE), sidecar verifier, and a
    sensitivity shift applied in logit space to the calibrated threshold."""

    def __init__(self, bundle: Path, sensitivity_shift: float = 0.0, ncpu: int = 1):
        bundle = Path(bundle)
        p = prepare_bundle(bundle)
        man, drop, vad_path = p["man"], p["drop"], p["vad"]
        if vad_path is None and man["decision"]["vad"].get("enabled"):
            log.warning("%s: silero_vad.onnx missing, wake VAD gate disabled", man["model_name"])
            man["decision"]["vad"]["enabled"] = False
        rt = man["runtime_contract"]
        ver = None
        if p["verifier"]:
            vpath, vm = p["verifier"]
            ver = PaddedVerifier(vpath, vm["threshold"], vm.get("window", 24), ncpu)
        need = max(int(man["embedding_window"]), ver.window if ver else 0)
        # The front-end always runs Silero when the file exists: the daemon
        # needs speech probability for endpointing even if the wake decision
        # does not gate on it.
        fe = ref.StreamFrontend(p["mel"], p["emb"], rt, vad_path, emb_keep=max(32, need), ncpu=ncpu)
        key = ("single",)
        self.groups = {key: fe}
        self.dec_group = [key]
        self.deciders = [ref.WakeDecider(man, p["head"], ver, None, ncpu)]
        self.refractory = None
        self.bundle = bundle
        self.name = man["model_name"]
        self.phrase = str(man.get("wake_word") or self.name).replace("_", " ")
        self.base_thr = float(man["threshold"])
        self.has_vad = vad_path is not None
        self.has_verifier = ver is not None
        self.set_sensitivity(sensitivity_shift, drop)
        self.reset()

    def set_sensitivity(self, shift: float, drop: float | None = None) -> None:
        if drop is not None:
            self._drop = drop
        d = self.deciders[0]
        d.thr = ref.sigmoid(ref.logit(self.base_thr) + float(shift) - self._drop)

    def describe(self) -> str:
        d = self.deciders[0]
        return (f"{self.name} thr={d.thr:.3f} (base {self.base_thr:g}) cf={d.cf} "
                f"vad={'on' if self.has_vad else 'off'} verifier={'on' if self.has_verifier else 'off'}")


class WordSpec:
    """One wake word for `MultiDetector`: `name` is the id used in the
    config (the bundle may call itself something else)."""

    def __init__(self, name: str, bundle: Path, shift: float = 0.0,
                 threshold: float | None = None, cooldown_s: float | None = None):
        self.name, self.bundle, self.shift = name, Path(bundle), float(shift)
        self.threshold, self.cooldown_s = threshold, cooldown_s


class _Slot:
    __slots__ = ("name", "phrase", "decider", "key", "base_thr", "mute", "cooldown_frames",
                 "has_verifier", "bundle")


class MultiDetector:
    """N wake words on ONE audio stream and ONE mel + embedding (+ Silero)
    front-end per distinct front-end. Only the heads (and verifiers) are per
    word. Unlike `WakeCore`, a candidate mutes only its own word, and every
    verified candidate is returned so the `Arbiter` can pick the winner."""

    def __init__(self, words: list[WordSpec], ncpu: int = 1):
        if not words:
            raise ValueError("no wake words")
        self.groups: dict[tuple, ref.StreamFrontend] = {}
        self.slots: list[_Slot] = []
        prepared = [(w, prepare_bundle(w.bundle)) for w in words]
        group_vad: dict[tuple, Path | None] = {}
        need: dict[tuple, int] = {}
        meta: dict[tuple, tuple] = {}
        for w, p in prepared:
            rt = p["man"]["runtime_contract"]
            key = (frontend_id(p["mel"]), frontend_id(p["emb"]), rt.get("dc_removal"),
                   float(rt.get("fixed_gain_db", 0.0)), bool(rt.get("apply_agc")))
            p["key"] = key
            meta.setdefault(key, (p["mel"], p["emb"], rt))
            if group_vad.get(key) is None:
                group_vad[key] = p["vad"]
            vwin = int(p["verifier"][1].get("window", 24)) if p["verifier"] else 0
            need[key] = max(need.get(key, 0), int(p["man"]["embedding_window"]), vwin)
        for key, (mel, emb, rt) in meta.items():
            self.groups[key] = ref.StreamFrontend(mel, emb, rt, group_vad[key],
                                                  emb_keep=max(32, need[key]), ncpu=ncpu)
        for w, p in prepared:
            man, key = p["man"], p["key"]
            if group_vad[key] is None and man["decision"]["vad"].get("enabled"):
                log.warning("%s: no silero_vad.onnx, wake VAD gate disabled", w.name)
                man["decision"]["vad"]["enabled"] = False
            ver = None
            if p["verifier"]:
                vpath, vm = p["verifier"]
                ver = PaddedVerifier(vpath, vm["threshold"], vm.get("window", 24), ncpu)
            s = _Slot()
            s.name, s.bundle, s.key = w.name, w.bundle, key
            s.phrase = str(man.get("wake_word") or w.name).replace("_", " ")
            s.decider = ref.WakeDecider(man, p["head"], ver, None, ncpu)
            s.base_thr = float(w.threshold if w.threshold is not None else man["threshold"])
            s.decider.thr = ref.sigmoid(ref.logit(s.base_thr) + w.shift - p["drop"])
            cd_ms = w.cooldown_s * 1000.0 if w.cooldown_s is not None else float(man["cooldown_ms"])
            s.cooldown_frames = int(round(cd_ms / FRAME_MS))
            s.has_verifier = ver is not None
            s.mute = 0
            self.slots.append(s)
        # Endpointing reads speech probability from a front-end that has Silero.
        self._vad_key = next((k for k, v in group_vad.items() if v is not None), next(iter(self.groups)))
        self.has_vad = group_vad[self._vad_key] is not None
        self.reset()

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.slots]

    def phrase_of(self, name: str) -> str:
        return next((s.phrase for s in self.slots if s.name == name), name.replace("_", " "))

    def reset(self) -> None:
        for fe in self.groups.values():
            fe.reset()
        for s in self.slots:
            s.decider.reset()
            s.mute = 0
        self.k = 0

    def describe(self) -> str:
        parts = [f"{len(self.slots)} word(s) on {len(self.groups)} front-end(s)"
                 f" vad={'on' if self.has_vad else 'off'}"]
        for s in self.slots:
            parts.append(f"{s.name} thr={s.decider.thr:.3f} (base {s.base_thr:g}) cf={s.decider.cf}"
                         f" verifier={'on' if s.has_verifier else 'off'}")
        return "; ".join(parts)

    def process(self, chunk: np.ndarray) -> dict:
        feats = {key: fe.push(chunk) for key, fe in self.groups.items()}
        per, events = [], []
        for s in self.slots:
            f = feats[s.key]
            d = s.decider
            muted = s.mute > 0
            sc = None if muted else d.head_score(f.emb)
            r = d.step_score(sc, f, muted)
            r["wake"] = s.name
            if muted:
                s.mute -= 1
            elif r["cand"]:
                ok, info = d.verify(f.emb)
                r.update(info)
                r["verified"] = ok
                if ok:
                    ev = {"wake": s.name, "frame": self.k, "score": r["score"], "peak": r["peak"],
                          "thr_eff": r["thr_eff"],
                          "margin": ref.logit(r["peak"]) - ref.logit(r["thr_eff"]), **info}
                    events.append(ev)
                    d.reset()
                    s.mute = s.cooldown_frames
                elif d.d.get("on_verifier_reject", "reset") == "reset":
                    d.reset()
            per.append(r)
        events.sort(key=lambda e: -e["margin"])
        out = {"frame": self.k, "per_wake": per, "events": events,
               "event": events[0] if events else None, "features": feats[self._vad_key]}
        self.k += 1
        return out

    def run_pcm(self, pcm: np.ndarray) -> list[dict]:
        evs = []
        for k in range(pcm.shape[0] // CHUNK):
            evs += self.process(np.ascontiguousarray(pcm[k * CHUNK:(k + 1) * CHUNK]))["events"]
        return evs


class Endpointer:
    """Speech onset / end-of-utterance from per-frame speech probability.

    Thread-safe: the audio thread calls `update`, the worker reads state."""

    def __init__(self, end_silence_ms: int = 1100, onset_frames: int = 2, prob_min: float = 0.5):
        self.lock = threading.Lock()
        self.end_frames = max(3, int(round(end_silence_ms / FRAME_MS)))
        self.onset_frames = onset_frames
        self.prob_min = prob_min
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.frames = 0
            self.recent: list[bool] = []
            self.started = False
            self.ended = False
            self.silence = 0
            self.speech_frames = 0

    def update(self, prob: float | None, rms: float, bg_rms: float) -> None:
        if prob is not None:
            voiced = prob >= self.prob_min
        else:  # energy fallback when no Silero model is around
            voiced = rms > max(150.0, 3.0 * bg_rms)
        with self.lock:
            self.frames += 1
            self.recent = (self.recent + [voiced])[-3:]
            if not self.started:
                if sum(self.recent) >= self.onset_frames:
                    self.started = True
                    self.speech_frames = sum(self.recent)
                return
            if voiced:
                self.silence = 0
                self.speech_frames += 1
            else:
                self.silence += 1
                if self.silence >= self.end_frames:
                    self.ended = True

    def snapshot(self) -> tuple[bool, bool, float]:
        with self.lock:
            return self.started, self.ended, self.frames * FRAME_MS / 1000.0


def pcm_chunks(stream, chunk: int = CHUNK):
    """Yield int16 numpy chunks of exactly `chunk` samples from a byte stream."""
    need = chunk * 2
    buf = b""
    while True:
        data = stream.read(need - len(buf))
        if not data:
            return
        buf += data
        if len(buf) >= need:
            yield np.frombuffer(buf[:need], dtype=np.int16).copy()
            buf = buf[need:]
