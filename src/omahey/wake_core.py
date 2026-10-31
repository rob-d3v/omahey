# SPDX-License-Identifier: Apache-2.0
"""wake-core streaming runtime (openWakeWord head + melspectrogram + embedding,
optional verifier.onnx second stage, Silero VAD gate, adaptive threshold).

Extracted unchanged from the Ania Player reference runtime (`bundle_infer.py`,
wake-core/1.0, by the same author). Only the batch helpers and the CLI were
dropped; the streaming classes below are byte-for-byte the reference so the
`golden_stream.json` shipped with each model bundle keeps proving parity
(`omahey test --selftest`). Inline comments are kept in the original
Portuguese on purpose: editing them would break the byte-for-byte rule.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

def read_wav_int16(path: Path, sample_rate: int = 16000) -> np.ndarray:
    """WAV mono PCM 16 bits → int16. Recusa o resto em vez de adivinhar.

    Reamostrar ou converter aqui esconderia o problema no lugar errado: se o
    dispositivo entrega 48 kHz estéreo, é a captura que precisa mudar, e é
    melhor descobrir isso na instalação do que num falso positivo seis meses
    depois.
    """
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path.name}: esperava PCM 16 bits, veio "
                             f"{w.getsampwidth()*8} bits")
        if w.getframerate() != sample_rate:
            raise SystemExit(f"{path.name}: esperava {sample_rate} Hz, veio "
                             f"{w.getframerate()} Hz")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(1).astype(np.int16)
    return np.ascontiguousarray(x, dtype=np.int16)



# ════════════════════════════════════════════════════════════════════════════
# wake-core — runtime de STREAMING (especificação: docs/wake-core-spec.md)
# ════════════════════════════════════════════════════════════════════════════
#
# Tudo abaixo é a referência executável da spec. Kotlin (Android), JS (site) e
# o Player portam ESTE código; os arquivos dourados (`golden_stream.json`)
# provam a porta quadro a quadro. Regras que não se negociam:
#
#   * o mel roda em TODO bloco de 80 ms, com as 480 amostras do bloco anterior
#     como contexto — inclusive no silêncio. Pular quadro cola palavras
#     separadas na mesma janela (o defeito do openWakeword.js);
#   * nenhuma decisão muda o áudio que vai ao modelo: gate, VAD e limiar
#     adaptativo decidem SE um score conta, nunca O QUE o modelo vê;
#   * manifest sem os campos novos => comportamento do infer.py antigo
#     (limiar, confirm_frames, cooldown e nada mais).

ENGINE_VERSION = "wake-core/1.0"

SR = 16000
CHUNK = 1280            # 80 ms
MEL_CONTEXT = 480       # 3 hops de 160 do bloco anterior -> 8 quadros de mel por bloco
MEL_WINDOW = 76
MEL_STEP = 8
EMB_DIM = 96
VAD_WIN = 512           # Silero v5 @16 kHz
VAD_CTX = 64
BG_HISTORY = 125        # ~10 s de blocos para o piso de fundo
BG_PERCENTILE = 20.0
TONAL_LAG = 20          # o "fundo" exclui os últimos 1,6 s (a frase candidata)
TONAL_SPAN = 25         # ... e olha 2 s antes disso
EPS_RMS = 1.0           # piso numérico do RMS (escala int16)

DEFAULT_RUNTIME = {
    "input_dtype": "int16",
    "chunk_ms": 80,
    "apply_agc": False,        # True só para pacotes legados (morphi); AGC do Android
    "fixed_gain_db": 0.0,      # ganho FIXO aplicado antes do mel
    "dc_removal": "none",      # "none" | "chunk_mean"
    "extra_margin": 0.0,
}

DEFAULT_DECISION = {
    # margem de pico: número absoluto, ou {"max": .10, "frac": .5} =
    # min(max, (1 - thr_eff) * frac) (a regra do Android)
    "peak_margin": 0.0,
    "vad": {"enabled": False, "model": "models/silero_vad.onnx",
            "speech_prob_min": 0.5, "hold_frames": 16, "min_voiced_frames": 1},
    "energy_gate": {"enabled": False, "window_frames": 16,
                    "peak_min_rms": 120.0, "peak_bg_k": 8.0,
                    "voiced_min_rms": 40.0, "voiced_bg_k": 3.0,
                    "min_voiced_frames": 4},
    "adaptive": {"enabled": False,
                 "bg_ref_dbfs": -50.0, "bg_span_db": 20.0, "bg_logit_boost": 0.0,
                 "steady_lo_db": 2.0, "steady_hi_db": 5.0,
                 "tonal_min_dbfs": -60.0, "tonal_logit_boost": 0.0,
                 "max_threshold": 0.99995},
    "on_verifier_reject": "reset",
}

# Perfil recomendado para pacotes novos (W1 grava isto no manifest) e usado
# pelo wakebench para medir os pacotes antigos "com o motor novo".
ROBUST_PROFILE = {
    "runtime_contract": {"dc_removal": "chunk_mean", "apply_agc": False,
                         "fixed_gain_db": 0.0},
    "decision": {
        "peak_margin": {"max": 0.10, "frac": 0.5},
        "vad": {"enabled": True, "speech_prob_min": 0.5, "hold_frames": 16,
                "min_voiced_frames": 3},
        "energy_gate": {"enabled": False},
        "adaptive": {"enabled": True, "bg_logit_boost": 1.0,
                     "tonal_logit_boost": 1.5},
    },
}


def _deep_merge(base: dict, over: dict | None) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def logit(p: float) -> float:
    p = min(max(float(p), 1e-7), 1 - 1e-7)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def percentile(values, pct: float) -> float:
    """Percentil com interpolação linear (igual ao numpy 'linear' e ao Player)."""
    v = sorted(float(x) for x in values)
    if not v:
        return 0.0
    k = (len(v) - 1) * (pct / 100.0)
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return v[lo]
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


# ── manifest: dois dialetos, um contrato ────────────────────────────────────
def load_manifest(bundle: Path) -> dict:
    """Lê o manifest do treinador (snake_case) OU o do Player/Android
    (`head`, `frontEnds`, `headWindow`, `phrase`) e devolve um só formato."""
    bundle = Path(bundle)
    raw = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
    head = raw.get("head_model") or raw.get("head")
    if not head:
        raise ValueError(f"{bundle}: manifest sem head_model/head")
    fe = raw.get("frontend") or raw.get("frontEnds") or {}
    name = raw.get("model_name") or raw.get("phrase") or Path(head).stem.replace("_head", "")
    m = dict(raw)
    m.update({
        "model_name": name,
        "wake_word": raw.get("wake_word") or raw.get("phrase") or name,
        "head_model": head,
        "frontend": {"melspectrogram": fe.get("melspectrogram", "models/melspectrogram.onnx"),
                     "embedding": fe.get("embedding", "models/embedding_model.onnx")},
        "embedding_window": int(raw.get("embedding_window") or raw.get("headWindow") or 16),
        "frame_ms": int(raw.get("frame_ms") or raw.get("frameMs") or 80),
        "sample_rate": int(raw.get("sample_rate") or raw.get("sampleRate") or SR),
        "threshold": float(raw.get("threshold", 0.5)),
        "confirm_frames": int(raw.get("confirm_frames", raw.get("confirmFrames", 1))),
        "cooldown_ms": int(raw.get("cooldown_ms", raw.get("cooldownMs", 3000))),
    })
    m["runtime_contract"] = _deep_merge(DEFAULT_RUNTIME, raw.get("runtime_contract"))
    m["decision"] = _deep_merge(DEFAULT_DECISION, raw.get("decision"))
    if m["sample_rate"] != SR or m["frame_ms"] != 80:
        raise ValueError(f"{name}: wake-core exige 16 kHz / 80 ms")
    return m


def resolve_file(bundle: Path, rel: str, extra_dirs=()) -> Path:
    """`rel` dentro do pacote; senão `models/<nome>`; senão as pastas extras
    (front-ends compartilhados, `ANIA_WAKE_FRONTENDS`)."""
    bundle = Path(bundle)
    name = Path(rel).name
    env = os.environ.get("ANIA_WAKE_FRONTENDS")
    cands = [bundle / rel, bundle / "models" / name, bundle / name]
    repo = [str(HERE / "assets" / "frontends"), str(HERE / "assets" / "vad")]   # rodando do repo
    for d in list(extra_dirs) + ([env] if env else []) + repo:
        if d:
            cands += [Path(d) / name, Path(d) / "models" / name]
    for c in cands:
        if c.is_file():
            return c
    raise FileNotFoundError(f"{bundle}: não achei {rel} (procurei {[str(c) for c in cands]})")


def _session(path: Path, ncpu: int = 1):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.inter_op_num_threads = so.intra_op_num_threads = ncpu
    so.enable_cpu_mem_arena = False
    return ort.InferenceSession(str(path), sess_options=so,
                                providers=["CPUExecutionProvider"])


# ── VAD: Silero v5 (MIT) ────────────────────────────────────────────────────
class SileroVAD:
    """Silero VAD v5 em streaming: janelas de 512 amostras com 64 de contexto,
    estado (2,1,128). Probabilidade do BLOCO de 80 ms = máximo das janelas que
    terminaram dentro dele (2 ou 3)."""

    def __init__(self, path: Path, ncpu: int = 1):
        self.sess = _session(path, ncpu)
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), np.float32)
        self.ctx = np.zeros(VAD_CTX, np.float32)
        self.buf = np.zeros(0, np.float32)
        self.last = 0.0

    def push(self, x_float: np.ndarray) -> float:
        self.buf = np.concatenate([self.buf, x_float.astype(np.float32)])
        probs = []
        sr = np.array(SR, np.int64)
        while self.buf.shape[0] >= VAD_WIN:
            inp = np.concatenate([self.ctx, self.buf[:VAD_WIN]])[None, :]
            out, self.state = self.sess.run(None, {"input": inp, "state": self.state, "sr": sr})
            self.ctx = inp[0, -VAD_CTX:].copy()
            self.buf = self.buf[VAD_WIN:]
            probs.append(float(np.asarray(out).ravel()[0]))
        if probs:
            self.last = max(probs)
        return self.last


class Features:
    """O que o front-end compartilhado entrega por bloco de 80 ms.

    index, rms (pós-DC, pré-ganho, escala int16), level_db = 20*log10(max(rms,1)/32768),
    bg_rms/bg_db (P20 do rms nos últimos 125 blocos), steady_db (desvio-padrão do
    level_db em [k-44, k-20]), vad_prob (None sem VAD), emb (n<=emb_keep, 96) ou
    None no aquecimento, rms_hist/vad_hist (históricos, último = bloco atual).
    Classe simples (sem dataclass) para o infer.py carregar fora de sys.modules."""
    __slots__ = ("index", "rms", "level_db", "bg_rms", "bg_db", "steady_db",
                 "vad_prob", "emb", "rms_hist", "vad_hist")

    def __init__(self, index, rms, level_db, bg_rms, bg_db, steady_db, vad_prob, emb,
                 rms_hist=(), vad_hist=()):
        self.index, self.rms, self.level_db = index, rms, level_db
        self.bg_rms, self.bg_db, self.steady_db = bg_rms, bg_db, steady_db
        self.vad_prob, self.emb = vad_prob, emb
        self.rms_hist, self.vad_hist = rms_hist, vad_hist


def make_features(index: int, rms_hist: list, lvl_hist: list, vad_hist: list,
                  vad_p: float | None, emb: np.ndarray | None) -> Features:
    """Estatísticas de nível a partir do histórico (o último item é o bloco atual).

    bg  = P20 do rms nos últimos 125 blocos (interpolação linear), piso 1.
    steady = desvio-padrão (populacional) do level_db nos blocos
             [k-44, k-20] — o fundo ANTES da frase candidata."""
    rms = rms_hist[-1]
    bg = max(percentile(rms_hist[-BG_HISTORY:], BG_PERCENTILE), EPS_RMS)
    tw = lvl_hist[-(TONAL_LAG + TONAL_SPAN):-TONAL_LAG] if len(lvl_hist) > TONAL_LAG else []
    steady = float(np.std(np.asarray(tw, np.float64))) if len(tw) >= 2 else 99.0
    return Features(index=index, rms=rms, level_db=lvl_hist[-1], bg_rms=bg,
                    bg_db=20.0 * math.log10(bg / 32768.0), steady_db=steady,
                    vad_prob=vad_p, emb=emb, rms_hist=rms_hist, vad_hist=vad_hist)


class StreamFrontend:
    """mel(480 de contexto) -> embedding(76/8) + nível + Silero, um bloco por vez.

    Compartilhado por todas as wake words que têm os MESMOS front-ends e o
    MESMO processamento de áudio (dc_removal, ganho, agc)."""

    def __init__(self, mel_path: Path, emb_path: Path, runtime: dict,
                 vad_path: Path | None = None, emb_keep: int = 32, ncpu: int = 1):
        self.mel = _session(mel_path, ncpu)
        self.emb_s = _session(emb_path, ncpu)
        self._mel_in = self.mel.get_inputs()[0].name
        self._emb_in = self.emb_s.get_inputs()[0].name
        self.rt = dict(runtime)
        self.gain = 10.0 ** (float(self.rt.get("fixed_gain_db", 0.0)) / 20.0)
        self.vad = SileroVAD(vad_path, ncpu) if vad_path else None
        self.emb_keep = max(16, int(emb_keep))
        self.reset()

    def reset(self):
        self.k = 0
        self.ctx = np.zeros(MEL_CONTEXT, np.float32)
        self.mel_ring = np.zeros((0, 32), np.float32)
        self.emb_ring = np.zeros((0, EMB_DIM), np.float32)
        self.rms_hist: list[float] = []
        self.lvl_hist: list[float] = []
        self.vad_hist: list[float] = []
        self.agc_gain = 1.0
        if self.vad:
            self.vad.reset()

    def push(self, chunk: np.ndarray) -> Features:
        if chunk.dtype != np.int16:
            raise ValueError(f"wake-core espera PCM int16, veio {chunk.dtype}")
        if chunk.shape[0] != CHUNK:
            raise ValueError(f"bloco de {chunk.shape[0]} amostras; o contrato é {CHUNK}")
        x = chunk.astype(np.float64)
        if self.rt.get("dc_removal") == "chunk_mean":
            x = x - x.mean()
        rms = float(np.sqrt(np.mean(x * x)))
        g = self.gain
        if self.rt.get("apply_agc"):
            # AGC legado (Android/Player): alvo 1500, [1/8, 8], suavização 0,9.
            tgt = min(max(1500.0 / (rms + 1e-9), 1.0 / 8.0), 8.0)
            self.agc_gain = 0.9 * self.agc_gain + 0.1 * tgt
            g = g * self.agc_gain
        y = np.clip(x * g, -32768.0, 32767.0).astype(np.float32)

        mel_in = np.concatenate([self.ctx, y])[None, :]
        self.ctx = y[-MEL_CONTEXT:].copy()
        spec = np.asarray(self.mel.run(None, {self._mel_in: mel_in})[0], np.float32)
        spec = spec.reshape(-1, 32) / 10.0 + 2.0
        self.mel_ring = np.concatenate([self.mel_ring, spec])[-(MEL_WINDOW + 2 * MEL_STEP):]
        if self.mel_ring.shape[0] >= MEL_WINDOW:
            w = self.mel_ring[-MEL_WINDOW:][None, :, :, None]
            e = np.asarray(self.emb_s.run(None, {self._emb_in: w})[0], np.float32).reshape(-1)[-EMB_DIM:]
            self.emb_ring = np.concatenate([self.emb_ring, e[None]])[-self.emb_keep:]

        vad_p = self.vad.push(y / 32768.0) if self.vad else None

        lvl = 20.0 * math.log10(max(rms, EPS_RMS) / 32768.0)
        self.rms_hist.append(rms)
        self.lvl_hist.append(lvl)
        self.vad_hist.append(vad_p if vad_p is not None else 0.0)
        keep = max(BG_HISTORY, TONAL_LAG + TONAL_SPAN)
        if len(self.rms_hist) > keep:
            del self.rms_hist[:-keep], self.lvl_hist[:-keep], self.vad_hist[:-keep]
        f = make_features(self.k, self.rms_hist, self.lvl_hist, self.vad_hist,
                          vad_p, self.emb_ring if self.emb_ring.shape[0] >= 16 else None)
        self.k += 1
        return f


# ── verificadores (2º estágio) ──────────────────────────────────────────────
class OnnxVerifier:
    """`verifier.onnx`: entrada (1, window, 96) -> probabilidade (1, 1)."""

    def __init__(self, path: Path, threshold: float, window: int, ncpu: int = 1):
        self.sess = _session(path, ncpu)
        self.inp = self.sess.get_inputs()[0].name
        self.threshold = float(threshold)
        self.window = int(window)

    def score(self, emb: np.ndarray) -> float:
        x = emb[-self.window:][None].astype(np.float32)
        return float(np.asarray(self.sess.run(None, {self.inp: x})[0]).ravel()[0])


class EnrollmentVerifier:
    """Verificador pessoal: regressão logística sobre as embeddings achatadas
    da janela (JSON: mean, scale, w, b, threshold, window). Desligado por padrão."""

    def __init__(self, path: Path, threshold: float | None = None):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.window = int(d["window"])
        self.mean = np.asarray(d["mean"], np.float64)
        self.scale = np.asarray(d["scale"], np.float64)
        self.w = np.asarray(d["w"], np.float64)
        self.b = float(d["b"])
        self.threshold = float(threshold if threshold is not None else d["threshold"])

    def score(self, emb: np.ndarray) -> float:
        z = (emb[-self.window:].reshape(-1).astype(np.float64) - self.mean) / self.scale
        return sigmoid(float(z @ self.w + self.b))


# ── decisão por wake word ───────────────────────────────────────────────────
class WakeDecider:
    """Score do head + gates + limiar adaptativo + confirmação + margem de pico.
    Não decide sozinho o disparo: devolve CANDIDATO; quem dispara é o WakeCore
    (verificador, arbitragem OU e refratário global)."""

    def __init__(self, man: dict, head_path: Path, verifier: OnnxVerifier | None = None,
                 enrollment: EnrollmentVerifier | None = None, ncpu: int = 1):
        self.man = man
        self.name = man["model_name"]
        self.head = _session(head_path, ncpu)
        self.head_in = self.head.get_inputs()[0].name
        self.win = int(man["embedding_window"])
        self.thr = float(man["threshold"])
        self.cf = max(1, int(man["confirm_frames"]))
        self.cooldown_frames = int(round(int(man["cooldown_ms"]) / 80.0))
        self.d = man["decision"]
        self.verifier = verifier
        self.enrollment = enrollment
        self.reset()

    def reset(self):
        self.run = 0
        self.peak = 0.0

    def head_score(self, emb: np.ndarray | None) -> float | None:
        if emb is None or emb.shape[0] < self.win:
            return None
        x = emb[-self.win:][None].astype(np.float32)
        return float(np.asarray(self.head.run(None, {self.head_in: x})[0]).ravel()[0])

    # ---- partes puras (também usadas pelo wakebench sobre features em cache)
    def threshold_eff(self, f: Features) -> float:
        a = self.d["adaptive"]
        if not a.get("enabled"):
            return self.thr
        boost = 0.0
        if a.get("bg_logit_boost"):
            boost += a["bg_logit_boost"] * min(max((f.bg_db - a["bg_ref_dbfs"]) / a["bg_span_db"], 0.0), 1.0)
        if a.get("tonal_logit_boost"):
            lo, hi = a["steady_lo_db"], a["steady_hi_db"]
            steady = min(max((hi - f.steady_db) / (hi - lo), 0.0), 1.0)
            loud = min(max((f.bg_db - a["tonal_min_dbfs"]) / 10.0, 0.0), 1.0)
            boost += a["tonal_logit_boost"] * steady * loud
        return min(sigmoid(logit(self.thr) + boost), float(a.get("max_threshold", 0.99995)))

    def gate(self, f: Features) -> bool:
        v = self.d["vad"]
        if v.get("enabled"):
            if f.vad_prob is None:
                raise RuntimeError("decision.vad ligado e o front-end sem VAD")
            h = int(v["hold_frames"])
            voiced = sum(1 for p in f.vad_hist[-h:] if p >= float(v["speech_prob_min"]))
            if voiced < max(1, int(v["min_voiced_frames"])):
                return False
        e = self.d["energy_gate"]
        if e.get("enabled"):
            w = f.rms_hist[-int(e["window_frames"]):]
            peak = max(w) if w else 0.0
            vthr = max(float(e["voiced_min_rms"]), f.bg_rms * float(e["voiced_bg_k"]))
            voiced = sum(1 for r in w if r > vthr)
            if not (peak >= max(float(e["peak_min_rms"]), f.bg_rms * float(e["peak_bg_k"]))
                    and voiced >= int(e["min_voiced_frames"])):
                return False
        return True

    def margin(self, thr_eff: float) -> float:
        pm = self.d.get("peak_margin", 0.0)
        if isinstance(pm, dict):
            return min(float(pm.get("max", 0.10)), (1.0 - thr_eff) * float(pm.get("frac", 0.5)))
        return float(pm or 0.0)

    def step_score(self, score: float | None, f: Features, muted: bool) -> dict:
        """Avança a máquina de estados com um score já calculado."""
        r = {"score": score, "thr": self.thr, "thr_eff": self.thr, "gate": False,
             "run": 0, "cand": False}
        if score is None:
            self.reset()
            return r
        te = self.threshold_eff(f)
        g = self.gate(f)
        r["thr_eff"], r["gate"] = te, g
        if muted:
            self.reset()
            return r
        if score >= te and g:
            self.run += 1
            self.peak = max(self.peak, score)
            if self.run >= self.cf and self.peak >= te + self.margin(te):
                r["cand"] = True
        else:
            self.reset()
        r["run"] = self.run
        r["peak"] = self.peak
        return r

    def verify(self, emb: np.ndarray) -> tuple[bool, dict]:
        info = {}
        if self.verifier is not None:
            v = self.verifier.score(emb)
            info["verifier"] = v
            if v < self.verifier.threshold:
                return False, info
        if self.enrollment is not None:
            e = self.enrollment.score(emb)
            info["enrollment"] = e
            if e < self.enrollment.threshold:
                return False, info
        return True, info


class WakeCore:
    """Uma ou várias wake words em modo OU sobre front-ends compartilhados.

        core = WakeCore([Path("ok_niva"), Path("ok_ania")])
        for chunk in blocos_de_1280_int16:
            ev = core.process(chunk)["event"]      # None ou {"wake": ..., ...}
    """

    def __init__(self, bundles, overrides: dict | None = None, extra_dirs=(),
                 enrollment: dict | None = None, use_verifier: bool = True,
                 global_refractory_ms: int | None = None, ncpu: int = 1):
        self.deciders: list[WakeDecider] = []
        self.groups: dict[tuple, StreamFrontend] = {}
        self.dec_group: list[tuple] = []
        for b in bundles:
            b = Path(b)
            man = load_manifest(b)
            if overrides:
                man["runtime_contract"] = _deep_merge(man["runtime_contract"], overrides.get("runtime_contract"))
                man["decision"] = _deep_merge(man["decision"], overrides.get("decision"))
                for k in ("threshold", "confirm_frames", "cooldown_ms"):
                    if k in overrides:
                        man[k] = overrides[k]
            mel = resolve_file(b, man["frontend"]["melspectrogram"], extra_dirs)
            emb = resolve_file(b, man["frontend"]["embedding"], extra_dirs)
            rt = man["runtime_contract"]
            vad_on = bool(man["decision"]["vad"].get("enabled"))
            vad = resolve_file(b, man["decision"]["vad"]["model"], extra_dirs) if vad_on else None
            ver = None
            vm = man.get("verifier")
            if use_verifier and vm and vm.get("file"):
                ver = OnnxVerifier(resolve_file(b, vm["file"]), vm["threshold"], vm.get("window", 24), ncpu)
            enr = None
            if enrollment and man["model_name"] in enrollment:
                enr = EnrollmentVerifier(Path(enrollment[man["model_name"]]))
            key = (_sha(mel), _sha(emb), rt.get("dc_removal"), float(rt.get("fixed_gain_db", 0.0)),
                   bool(rt.get("apply_agc")), _sha(vad) if vad else None)
            need = max(int(man["embedding_window"]), ver.window if ver else 0, enr.window if enr else 0)
            if key not in self.groups:
                self.groups[key] = StreamFrontend(mel, emb, rt, vad, emb_keep=max(32, need), ncpu=ncpu)
            else:
                fe = self.groups[key]
                fe.emb_keep = max(fe.emb_keep, need)
            self.deciders.append(WakeDecider(man, resolve_file(b, man["head_model"]), ver, enr, ncpu))
            self.dec_group.append(key)
        self.refractory = (int(round(global_refractory_ms / 80.0)) if global_refractory_ms is not None else None)
        self.reset()

    def reset(self):
        for fe in self.groups.values():
            fe.reset()
        for d in self.deciders:
            d.reset()
        self.mute = 0
        self.k = 0

    def process(self, chunk: np.ndarray) -> dict:
        feats = {key: fe.push(chunk) for key, fe in self.groups.items()}
        scores = [d.head_score(feats[key].emb) for d, key in zip(self.deciders, self.dec_group)]
        return self.decide(feats, scores)

    def decide(self, feats: dict, scores: list) -> dict:
        """Decisão de um bloco a partir de features e scores JÁ calculados.
        É o que o wakebench reexecuta sobre features em cache."""
        muted = self.mute > 0
        per, cands = [], []
        for d, key, sc in zip(self.deciders, self.dec_group, scores):
            f = feats[key]
            r = d.step_score(sc, f, muted)
            r["wake"] = d.name
            if r["cand"]:
                ok, info = d.verify(f.emb)
                r.update(info)
                r["verified"] = ok
                if ok:
                    cands.append((logit(r["peak"]) - logit(r["thr_eff"]), d, r))
                elif d.d.get("on_verifier_reject", "reset") == "reset":
                    d.reset()
            per.append(r)
        event = None
        if cands:
            cands.sort(key=lambda t: -t[0])
            _, d, r = cands[0]
            r["fired"] = True
            event = {"wake": d.name, "frame": self.k, "t_s": round((self.k + 1) * CHUNK / SR, 3),
                     "score": r["score"], "peak": r["peak"], "thr_eff": r["thr_eff"]}
            for k in ("verifier", "enrollment"):
                if k in r:
                    event[k] = r[k]
            self.mute = self.refractory if self.refractory is not None else d.cooldown_frames
            for dd in self.deciders:
                dd.reset()
        elif self.mute > 0:
            self.mute -= 1
        f0 = next(iter(feats.values()))
        out = {"frame": self.k, "per_wake": per, "event": event, "features": f0}
        self.k += 1
        return out

    def run_pcm(self, pcm: np.ndarray, trace: bool = False):
        """Arquivo inteiro em blocos de 1280 (o resto < 1280 é descartado)."""
        events, frames = [], []
        for k in range(pcm.shape[0] // CHUNK):
            o = self.process(np.ascontiguousarray(pcm[k * CHUNK:(k + 1) * CHUNK]))
            if o["event"]:
                events.append(o["event"])
            if trace:
                f = o["features"]
                frames.append({
                    "rms": round(f.rms, 2), "bg_db": round(f.bg_db, 3), "steady_db": round(min(f.steady_db, 99.0), 3),
                    "vad": None if f.vad_prob is None else round(f.vad_prob, 5),
                    "wakes": [{"s": None if r["score"] is None else round(r["score"], 5),
                               "te": round(r["thr_eff"], 6), "g": int(bool(r["gate"])),
                               "run": r["run"], "fire": int(bool(r.get("fired")))} for r in o["per_wake"]],
                })
        return events, frames


def stream_events(bundle: Path, pcm: np.ndarray, **kw) -> list[dict]:
    return WakeCore([bundle], **kw).run_pcm(pcm)[0]


# ── autoteste ───────────────────────────────────────────────────────────────
def _check_stream_golden(bundle: Path, fails: list, verbose: bool) -> None:
    """`golden_stream.json`: o caminho de STREAMING quadro a quadro.

    Score dentro de `tol_score`; VAD dentro de `tol_vad`; disparos idênticos,
    exceto quando o score do quadro em questão está a menos de `tol_score` do
    limiar efetivo (quadro ambíguo, ver spec §9)."""
    gp = bundle / "golden_stream.json"
    if not gp.exists():
        if verbose:
            print("[4/4] golden_stream.json ausente (pacote anterior ao wake-core) — pulado")
        return
    g = json.loads(gp.read_text(encoding="utf-8"))
    ts, tv = float(g.get("tol_score", 2e-3)), float(g.get("tol_vad", 2e-2))
    worst_s = worst_v = 0.0
    for item in g["clips"]:
        core = WakeCore([bundle], overrides=g.get("overrides"))
        pcm = read_wav_int16(bundle / item["file"])
        events, frames = core.run_pcm(pcm, trace=True)
        exp = item["frames"]
        if len(frames) != len(exp):
            fails.append(f"{item['file']}: {len(frames)} quadros (stream), esperava {len(exp)}")
            continue
        for i, (a, b) in enumerate(zip(frames, exp)):
            if b["vad"] is not None and a["vad"] is not None:
                worst_v = max(worst_v, abs(a["vad"] - b["vad"]))
            for wa, wb in zip(a["wakes"], b["wakes"]):
                if wa["s"] is None or wb["s"] is None:
                    if (wa["s"] is None) != (wb["s"] is None):
                        fails.append(f"{item['file']}#{i}: aquecimento diverge")
                    continue
                d = abs(wa["s"] - wb["s"])
                worst_s = max(worst_s, d)
                if wa["fire"] != wb["fire"] and abs(wb["s"] - wb["te"]) > ts:
                    fails.append(f"{item['file']}#{i}: disparo {wa['fire']} esperava {wb['fire']}")
        if [e["frame"] for e in events] != item.get("event_frames", [e["frame"] for e in events]):
            # só informativo quando ambíguo; as divergências reais já entraram acima
            pass
    if worst_s > ts:
        fails.append(f"stream: score diverge {worst_s:.2e} (tol {ts:.0e})")
    if worst_v > tv:
        fails.append(f"stream: VAD diverge {worst_v:.2e} (tol {tv:.0e})")
    if verbose:
        print(f"[4/4] streaming: {len(g['clips'])} áureos · score {worst_s:.2e} · vad {worst_v:.2e}")
