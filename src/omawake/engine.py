# SPDX-License-Identifier: Apache-2.0
"""Builds the wake detector from a bundle and tracks speech for endpointing."""
from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

import numpy as np

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


class Detector(ref.WakeCore):
    """`WakeCore` for one bundle, with the Ania Player policy for old bundles
    (no `decision` block -> ROBUST_PROFILE), sidecar verifier, and a
    sensitivity shift applied in logit space to the calibrated threshold."""

    def __init__(self, bundle: Path, sensitivity_shift: float = 0.0, ncpu: int = 1):
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

        mel = ref.resolve_file(bundle, man["frontend"]["melspectrogram"])
        emb = ref.resolve_file(bundle, man["frontend"]["embedding"])
        rt = man["runtime_contract"]
        vad_path = None
        try:
            vad_path = ref.resolve_file(bundle, man["decision"]["vad"].get("model", "models/silero_vad.onnx"))
        except FileNotFoundError:
            if man["decision"]["vad"].get("enabled"):
                log.warning("%s: silero_vad.onnx missing, wake VAD gate disabled", man["model_name"])
                man["decision"]["vad"]["enabled"] = False
        ver = None
        vm = man.get("verifier")
        if isinstance(vm, dict) and vm.get("file"):
            ver = PaddedVerifier(ref.resolve_file(bundle, vm["file"]), vm["threshold"],
                                   vm.get("window", 24), ncpu)
        need = max(int(man["embedding_window"]), ver.window if ver else 0)
        # The front-end always runs Silero when the file exists: the daemon
        # needs speech probability for endpointing even if the wake decision
        # does not gate on it.
        fe = ref.StreamFrontend(mel, emb, rt, vad_path, emb_keep=max(32, need), ncpu=ncpu)
        key = ("single",)
        self.groups = {key: fe}
        self.dec_group = [key]
        self.deciders = [ref.WakeDecider(man, ref.resolve_file(bundle, man["head_model"]), ver, None, ncpu)]
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
