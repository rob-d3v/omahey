# SPDX-License-Identifier: Apache-2.0
"""Wake word bundles: the catalog (models.json), local discovery, download.

Catalogue kinds: "bundle" (a wake-core archive from this repo's releases),
"upstream-head" (one openWakeWord head fetched from its upstream release,
pinned by sha256, wrapped into a local bundle after the user accepts its
licence; omawake never redistributes it) and "user" (your own model).

A bundle is a folder with `manifest.json` (wake-core format), the head
`.onnx`, and `models/` (melspectrogram, embedding, silero_vad). An optional
second-stage verifier lives in a sibling folder `<bundle>.verifier/` with
`verifier.json` + `verifier.onnx` (same layout as Ania Player).
Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import config as C

CATALOG_FILE = C.REPO_DIR / "models.json"

# sha256 of byte-different exports of the same frozen openWakeWord front-end
# (upstream v0.5.1 and the copies inside wake-core bundles). Verified to give
# bit-identical outputs, so every head can share one front-end stream.
FRONTEND_ALIASES = {
    "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f": "oww-mel-v1",
    "497b170c85dc86eea3c4cc648e940876d3028641696f9e6624a27bc45dd871c1": "oww-mel-v1",
    "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f": "oww-emb-v1",
    "30726fbed99be0403bb89a14ab500b11308f4340a110d4ecf69d998f566951f2": "oww-emb-v1",
}


def catalog() -> dict:
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"models": []}
    except (OSError, ValueError):
        return {"models": []}


def _bundle_name(path: Path) -> str | None:
    try:
        raw = json.loads((path / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    head = raw.get("head_model") or raw.get("head")
    if not head:
        return None
    return str(raw.get("model_name") or raw.get("phrase") or Path(head).stem.replace("_head", ""))


def _bundle_phrase(path: Path) -> str:
    try:
        raw = json.loads((path / "manifest.json").read_text(encoding="utf-8-sig"))
        return str(raw.get("wake_word") or raw.get("phrase") or raw.get("model_name") or path.name)
    except (OSError, ValueError):
        return path.name


DONATE_URL = "https://aniamodels.shop/omarchy-voice"
DONATE_LINE = f"Help train “Hey Omarchy” — donate your voice (2 min): {DONATE_URL}"


def donate_hint(have: dict) -> str | None:
    """The call to action, shown only while the official “Hey Omarchy” model is not installed."""
    return None if "hey_omarchy" in have else DONATE_LINE


def installed(cfg: dict) -> dict[str, Path]:
    """{wake_word_id: bundle_path}; earlier search dirs win."""
    found: dict[str, Path] = {}
    for d in C.model_search_dirs(cfg):
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            if not sub.is_dir() or sub.name.endswith(".verifier"):
                continue
            name = _bundle_name(sub)
            if name and name not in found:
                found[name] = sub
    return found


def listing(cfg: dict) -> list[dict]:
    """Catalog entries merged with what is on disk, for the CLI and widget."""
    have = installed(cfg)
    out, seen = [], set()
    for m in catalog().get("models", []):
        mid = str(m.get("id"))
        seen.add(mid)
        out.append({
            "id": mid,
            "phrase": m.get("phrase", mid),
            "language": m.get("language", "en"),
            "status": "installed" if mid in have else m.get("status", "available"),
            "path": str(have[mid]) if mid in have else None,
            "source": "catalog",
            "kind": m.get("kind", "bundle"),
            "license": m.get("license"),
            "note": m.get("note") or m.get("attribution"),
        })
    for mid, p in have.items():
        if mid not in seen:
            out.append({"id": mid, "phrase": _bundle_phrase(p), "language": "?",
                        "status": "installed", "path": str(p), "source": "local"})
    return out


def resolve(cfg: dict, exclude=()) -> tuple[str, Path] | None:
    """The configured wake word, else the catalog default, else any local one
    (never one in `exclude`: words Ania Player is using)."""
    have = {k: v for k, v in installed(cfg).items() if k not in set(exclude)}
    want = [cfg.get("wake_word"), catalog().get("default")]
    for w in want:
        if w and w in have:
            return w, have[w]
    if have:
        k = sorted(have)[0]
        return k, have[k]
    return None


def effective_words(cfg: dict, wc=None, skip=()) -> tuple[object, list[dict]]:
    """(WakeConfig, words): what will really listen, for the daemon, status
    and setup alike. Legacy config (no config.toml) falls back to any
    installed word when the configured one is missing; `configured` then
    names the word that was asked for. Words in `skip` (used by a running
    Ania Player) are never active and carry "ania_player": True."""
    from . import wakeconf as W
    wc = W.load(cfg) if wc is None else wc
    have = installed(cfg)
    skip = set(skip)
    configured = None
    if wc.source == "legacy" and wc.specs:
        first = wc.specs[0]
        if first.model not in have or first.model in skip:
            found = resolve(cfg, exclude=skip)
            if found:
                configured, first.model = first.model, found[0]
    phrases = {m["id"]: m["phrase"] for m in listing(cfg)}
    words = []
    for i, s in enumerate(wc.specs):
        inst, shared = s.model in have, s.model in skip
        words.append({"model": s.model,
                      "phrase": s.label or phrases.get(s.model) or s.model.replace("_", " "),
                      "action": s.action, "describe": s.describe(), "enabled": s.enabled,
                      "installed": inst, "active": s.enabled and inst and not shared,
                      "ania_player": shared,
                      "configured": configured if i == 0 else None})
    return wc, words


def entry(model_id: str) -> dict | None:
    return next((m for m in catalog().get("models", []) if m.get("id") == model_id), None)


def license_notice(e: dict) -> str:
    return (f"“{e.get('phrase', e['id'])}” is not distributed by omawake. It is downloaded from its\n"
            f"upstream project: {e.get('attribution', e.get('url'))}.\n"
            f"Licence: {e.get('license')} ({e.get('license_url', '')}).\n"
            "Non-commercial use only, with attribution; share-alike applies to derivatives.")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fetch(url: str, sha: str, dest: Path, log=print) -> None:
    if not str(url).startswith("https://") or not sha:
        raise RuntimeError(f"refusing {url}: needs an https url and a pinned sha256")
    log(f"Downloading {url}")
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    got = _sha256(tmp)
    if got != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}: {got} != {sha}")
    tmp.replace(dest)


def _local_frontend(cfg: dict) -> dict[str, Path]:
    """mel / emb / silero files from an installed bundle (copied instead of
    downloaded, and identical bytes keep the shared front-end shared)."""
    out: dict[str, Path] = {}
    for b in installed(cfg).values():
        d = b / "models"
        mel, emb, vad = d / "melspectrogram.onnx", d / "embedding_model.onnx", d / "silero_vad.onnx"
        if "mel" not in out and mel.is_file() and emb.is_file() \
                and FRONTEND_ALIASES.get(_sha256(mel)) == "oww-mel-v1" \
                and FRONTEND_ALIASES.get(_sha256(emb)) == "oww-emb-v1":
            out["mel"], out["emb"] = mel, emb
        if "vad" not in out and vad.is_file():
            out["vad"] = vad
            lic = d / "LICENSE.silero-vad"
            if lic.is_file():
                out["vad_license"] = lic
    return out


def make_head_bundle(head: Path, dest: Path, model_id: str, phrase: str, threshold: float,
                     frontend: dict[str, Path], info: dict | None = None) -> Path:
    """Wrap a bare openWakeWord head (1,16,96)->(1,1) into a wake-core bundle
    folder. `frontend` needs "mel" and "emb"; "vad" (Silero v5) is optional."""
    info = dict(info or {})
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "models").mkdir(exist_ok=True)
    shutil.copyfile(head, dest / f"{model_id}_head.onnx")
    shutil.copyfile(frontend["mel"], dest / "models/melspectrogram.onnx")
    shutil.copyfile(frontend["emb"], dest / "models/embedding_model.onnx")
    vad_on = "vad" in frontend
    if vad_on:
        shutil.copyfile(frontend["vad"], dest / "models/silero_vad.onnx")
        if frontend.get("vad_license"):
            shutil.copyfile(frontend["vad_license"], dest / "models/LICENSE.silero-vad")
    man = {
        "format": "openwakeword_head_v1", "engine": "openwakeword",
        "model_name": model_id, "wake_word": phrase.lower(), "language": info.get("language", "en"),
        "head_model": f"{model_id}_head.onnx",
        "frontend": {"melspectrogram": "models/melspectrogram.onnx", "embedding": "models/embedding_model.onnx"},
        "embedding_window": 16, "frame_ms": 80, "sample_rate": 16000,
        "threshold": float(threshold), "confirm_frames": 2, "cooldown_ms": 2000,
        "decision": {
            "peak_margin": {"max": 0.10, "frac": 0.5},
            "vad": {"enabled": vad_on, "model": "models/silero_vad.onnx", "speech_prob_min": 0.5,
                    "hold_frames": 16, "min_voiced_frames": 3},
            "energy_gate": {"enabled": False},
            "adaptive": {"enabled": True, "bg_ref_dbfs": -50.0, "bg_span_db": 20.0, "bg_logit_boost": 1.0,
                         "steady_lo_db": 2.0, "steady_hi_db": 5.0, "tonal_min_dbfs": -60.0,
                         "tonal_logit_boost": 1.5, "max_threshold": 0.99995},
            "on_verifier_reject": "reset",
        },
        "runtime_contract": {"input_dtype": "int16", "chunk_ms": 80, "apply_agc": False,
                             "fixed_gain_db": 0.0, "dc_removal": "chunk_mean", "extra_margin": 0.0},
        "license": info.get("license", "unknown"),
        "source": info.get("url", ""),
    }
    (dest / "manifest.json").write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    if info.get("license"):
        (dest / "LICENSE-NOTICE.txt").write_text(
            f"{info.get('attribution', '')}\nSource: {info.get('url', '')}\n"
            f"Licence: {info['license']} {info.get('license_url', '')}\n"
            "Downloaded by the user from upstream; not redistributed by omawake.\n"
            "Front-end models: openWakeWord (Apache-2.0). Silero VAD: MIT.\n", encoding="utf-8")
    return dest


def _download_head(e: dict, accept_license: bool, log=print) -> Path:
    if not accept_license:
        raise PermissionError(license_notice(e) + "\nRe-run with --accept-license to download it.")
    cfg = C.load_config()
    C.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(C.DATA_DIR)) as tmp:
        t = Path(tmp)
        head = t / "head.onnx"
        _fetch(e["url"], e.get("sha256"), head, log)
        fe = _local_frontend(cfg)
        if "mel" not in fe:
            cat = catalog().get("frontend", {})
            for k, name in (("mel", "melspectrogram"), ("emb", "embedding")):
                fe[k] = t / f"{name}.onnx"
                _fetch(cat[name]["url"], cat[name]["sha256"], fe[k], log)
        if "vad" not in fe:
            log("No Silero VAD v5 found locally: this wake word runs without the speech gate.")
        out = t / "bundle"
        make_head_bundle(head, out, e["id"], e.get("phrase", e["id"]), float(e.get("threshold", 0.5)), fe, e)
        dest = C.MODELS_DIR / e["id"]
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(out), dest)
    log(f"Installed {e['id']} -> {dest} ({e.get('license')})")
    return dest


def download(model_id: str, log=print, accept_license: bool = False) -> Path:
    """Fetch a catalog model over HTTPS, verify sha256, install into
    MODELS_DIR. Never executes anything from the download."""
    e = entry(model_id)
    if e is None:
        raise ValueError(f"unknown wake word '{model_id}'")
    if e.get("kind") == "user":
        raise RuntimeError(f"'{model_id}' is your own model: {e.get('note', '')}")
    if e.get("status") not in (None, "available"):
        raise RuntimeError(f"'{model_id}' is not published yet (status: {e.get('status')})")
    if e.get("kind") == "upstream-head":
        return _download_head(e, accept_license, log)
    return _download_bundle(e, log)


def _download_bundle(entry: dict, log=print) -> Path:
    model_id = entry["id"]
    url, sha = entry.get("url"), entry.get("sha256")
    if not url or not str(url).startswith("https://") or not sha:
        raise RuntimeError(f"catalog entry for '{model_id}' has no https url + sha256")
    C.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(C.DATA_DIR)) as tmp:
        arc = Path(tmp) / "bundle.tar.gz"
        _fetch(url, sha, arc, log)
        out = Path(tmp) / "x"
        with tarfile.open(arc, "r:gz") as t:
            t.extractall(out, filter="data")
        cands = [p.parent for p in out.rglob("manifest.json")]
        if not cands:
            raise RuntimeError("archive has no manifest.json")
        src = min(cands, key=lambda p: len(p.parts))
        dest = C.MODELS_DIR / model_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), dest)
        ver = src.parent / (src.name + ".verifier")
        if ver.is_dir():
            vdest = C.MODELS_DIR / (model_id + ".verifier")
            if vdest.exists():
                shutil.rmtree(vdest)
            shutil.move(str(ver), vdest)
    log(f"Installed {model_id} -> {dest}")
    return dest


MAX_IMPORT_BYTES = 200 * 1024 * 1024


def _safe_extract(archive: Path, out: Path) -> None:
    """Unpack a .zip / .tar(.gz) refusing absolute paths, `..`, links and
    archives that expand past MAX_IMPORT_BYTES."""
    import zipfile
    out.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            total = 0
            for i in z.infolist():
                p = Path(i.filename)
                if p.is_absolute() or ".." in p.parts:
                    raise RuntimeError(f"unsafe path in archive: {i.filename}")
                if (i.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError(f"archive contains a link: {i.filename}")
                total += i.file_size
                if total > MAX_IMPORT_BYTES:
                    raise RuntimeError("archive is too large for a wake word")
            z.extractall(out)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            if sum(m.size for m in t.getmembers()) > MAX_IMPORT_BYTES:
                raise RuntimeError("archive is too large for a wake word")
            t.extractall(out, filter="data")
        return
    raise RuntimeError("not a .zip or .tar.gz wake word pack")


def import_bundle(src: Path, log=print, replace: bool = False) -> tuple[str, Path]:
    """Install a wake word you already have (an Ania pack from aniamodels.shop:
    .zip / .tar.gz, its folder, or its manifest.json) into MODELS_DIR.
    The pack is loaded the way the daemon loads it before anything is copied;
    nothing from it is executed. Returns (wake_word_id, installed_path)."""
    from . import engine as E
    src = Path(src).expanduser()
    if not src.exists():
        raise RuntimeError(f"no such file: {src}")
    C.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(C.DATA_DIR)) as tmp:
        if src.is_file() and src.name != "manifest.json":
            root = Path(tmp) / "x"
            _safe_extract(src, root)
        else:
            root = src.parent if src.name == "manifest.json" else src
        cands = [p.parent for p in root.rglob("manifest.json")
                 if not any(part.endswith(".verifier") for part in p.parent.relative_to(root).parts)]
        cands = [p for p in cands if _bundle_name(p)]
        if not cands:
            raise RuntimeError("no wake word manifest.json found (is this an Ania wake word pack?)")
        pack = min(cands, key=lambda p: len(p.parts))
        name = _bundle_name(pack)
        if not name or "/" in name or name.startswith(".") or name.endswith(".verifier"):
            raise RuntimeError(f"pack has an unusable name: {name!r}")
        try:
            E.prepare_bundle(pack)
        except Exception as e:  # noqa: BLE001 - any load error means "not usable"
            raise RuntimeError(f"pack does not load: {e}") from e
        dest = C.MODELS_DIR / name
        if dest.exists() and not replace:
            raise RuntimeError(f"'{name}' is already installed ({dest}); use --replace to overwrite it")
        stage = Path(tmp) / "stage"
        shutil.copytree(pack, stage / name, symlinks=False)
        ver = next((v for v in (pack.parent / (pack.name + ".verifier"), pack.parent / (name + ".verifier"))
                    if v.is_dir()), None)
        if ver is not None:
            shutil.copytree(ver, stage / (name + ".verifier"), symlinks=False)
        if ver is None and (C.MODELS_DIR / (name + ".verifier")).exists():
            shutil.rmtree(C.MODELS_DIR / (name + ".verifier"))   # it belonged to the old pack
        for item in sorted(stage.iterdir()):
            target = C.MODELS_DIR / item.name
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(item), target)
    log(f"Installed {name} -> {dest}")
    return name, dest
