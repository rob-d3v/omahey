# SPDX-License-Identifier: Apache-2.0
"""Voice commands: fuzzy-match a short transcript to a phrase from the
user's config, and run configured commands safely. Stdlib only.

The transcript only ever *selects* a command; it is never part of one.
The one place speech reaches a program is a new agent window (action
"agent-prompt" or fallback "agent"): it is typed there through wtype's stdin,
never put in a process's arguments, where other accounts could read it.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from difflib import SequenceMatcher

from . import config as C
from . import text as T
from .wakeconf import parse_command

log = logging.getLogger("omahey.commands")

_FILLER = {"please", "can", "could", "would", "you", "the", "a", "an", "my", "me", "for",
           "now", "to", "just", "hey", "ok", "okay", "uh", "um", "and", "thanks", "thank", "turn"}
_LEAD = ("can you ", "could you ", "would you ", "please ", "i want to ", "i'd like to ")


def _clean(s: str) -> str:
    t = T.norm(s)
    changed = True
    while changed:
        changed = False
        for p in _LEAD:
            if t.startswith(p):
                t, changed = t[len(p):], True
    for suf in (" please", " thanks", " thank you", " now"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    return t.strip()


def _content(s: str) -> list[str]:
    return [w for w in s.split() if w not in _FILLER]


def similarity(said: str, phrase: str) -> float:
    """0..1. Max of character similarity on the cleaned strings and on their
    content words (so "take a screenshot please" ~ "screenshot")."""
    a, b = _clean(said), _clean(phrase)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ca, cb = _content(a), _content(b)
    if ca and cb:
        ratio = max(ratio, SequenceMatcher(None, " ".join(ca), " ".join(cb)).ratio())
        # every content word of the phrase present, and little else said
        if set(cb) <= set(ca) and len(ca) <= len(cb) + 1:
            ratio = max(ratio, 0.9)
    return ratio


def match(said: str, table: dict[str, str], cutoff: float = 0.72,
          margin: float = 0.10) -> tuple[str, str, float] | None:
    """Best (phrase, command, score), or None when nothing is close enough or
    two *different* commands are too close to call."""
    if not said or not table:
        return None
    ranked = sorted(((similarity(said, p), p) for p in table), reverse=True)
    best_s, best_p = ranked[0]
    if best_s < cutoff:
        return None
    for s, p in ranked[1:]:
        if table[p] == table[best_p]:
            continue
        if best_s - s < margin:
            log.info("ambiguous command %r: %r vs %r", said, best_p, p)
            return None
        break
    return best_p, table[best_p], best_s


def run(cmdline: str, timeout_s: float = 15.0, dry_run: bool = False, why: str = "") -> dict:
    """Run a command from the config file: argv list, no shell, no stdin,
    output to the log, own process group (killed on timeout)."""
    try:
        argv = parse_command(cmdline)
    except ValueError as e:
        return {"ok": False, "error": str(e), "argv": []}
    return run_argv(argv, timeout_s, dry_run, why)


def run_argv(argv: list[str], timeout_s: float = 15.0, dry_run: bool = False, why: str = "") -> dict:
    exe = shutil.which(argv[0])
    if not exe:
        log.warning("command not found: %s (%s)", argv[0], why)
        return {"ok": False, "error": f"{argv[0]}: not found", "argv": argv}
    log.info("run %s%s%s", argv, f" ({why})" if why else "", " [dry run]" if dry_run else "")
    if dry_run:
        return {"ok": True, "argv": argv, "dry_run": True, "exit": 0}
    t0 = time.monotonic()
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(C.LOG_FILE, "ab") as logf:
        try:
            p = subprocess.Popen([exe, *argv[1:]], shell=False, stdin=subprocess.DEVNULL,
                                 stdout=logf, stderr=logf, start_new_session=True, cwd=str(C.HOME),
                                 close_fds=True)
        except OSError as e:
            log.error("cannot start %s: %s", argv[0], e)
            return {"ok": False, "error": str(e), "argv": argv}
    try:
        code = p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        log.warning("%s timed out after %.0fs and was stopped", argv[0], timeout_s)
        return {"ok": False, "error": f"timed out after {timeout_s:.0f}s", "argv": argv}
    dt = time.monotonic() - t0
    log.info("%s exited %d in %.2fs", argv[0], code, dt)
    return {"ok": code == 0, "exit": code, "argv": argv,
            **({} if code == 0 else {"error": f"exit status {code}"})}


def agent_prompt(text: str, dry_run: bool = False, launch_timeout_s: float = 12,
                 ready_delay_ms: int = 1500, submit_delay_ms: int = 350) -> dict:
    """Open a new `omarchy agent` window and type the transcript into it.

    `omarchy agent prompt "<text>"` would do the same in one call, but the
    text would then sit in the argv of every process down the chain, which
    any local account can list. Typing it keeps it in stdin only."""
    log.info("new agent prompt, %d chars%s", len(text), " [dry run]" if dry_run else "")
    if dry_run:
        return {"ok": True, "dry_run": True}
    from . import desktop as D
    try:
        addr = D.new_agent(launch_timeout_s, ready_delay_ms)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if not D.type_into(addr, text, submit=True, submit_delay_ms=submit_delay_ms):
        return {"ok": False, "error": "could not type into the agent window (wtype)"}
    return {"ok": True}