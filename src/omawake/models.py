# SPDX-License-Identifier: Apache-2.0
"""Wake word bundles: the catalog (models.json), local discovery, download.

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
        })
    for mid, p in have.items():
        if mid not in seen:
            out.append({"id": mid, "phrase": _bundle_phrase(p), "language": "?",
                        "status": "installed", "path": str(p), "source": "local"})
    return out


def resolve(cfg: dict) -> tuple[str, Path] | None:
    """The configured wake word, else the catalog default, else any local one."""
    have = installed(cfg)
    want = [cfg.get("wake_word"), catalog().get("default")]
    for w in want:
        if w and w in have:
            return w, have[w]
    if have:
        k = sorted(have)[0]
        return k, have[k]
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(model_id: str, log=print) -> Path:
    """Fetch a catalog bundle (tar.gz) over HTTPS, verify sha256, unpack into
    MODELS_DIR. Never executes anything from the archive."""
    entry = next((m for m in catalog().get("models", []) if m.get("id") == model_id), None)
    if entry is None:
        raise ValueError(f"unknown wake word '{model_id}'")
    if entry.get("status") not in (None, "available"):
        raise RuntimeError(f"'{model_id}' is not published yet (status: {entry.get('status')})")
    url, sha = entry.get("url"), entry.get("sha256")
    if not url or not str(url).startswith("https://") or not sha:
        raise RuntimeError(f"catalog entry for '{model_id}' has no https url + sha256")
    C.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(C.DATA_DIR)) as tmp:
        arc = Path(tmp) / "bundle.tar.gz"
        log(f"Downloading {url}")
        with urllib.request.urlopen(url, timeout=60) as r, open(arc, "wb") as f:
            shutil.copyfileobj(r, f)
        got = _sha256(arc)
        if got != sha:
            raise RuntimeError(f"checksum mismatch for {model_id}: {got} != {sha}")
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
