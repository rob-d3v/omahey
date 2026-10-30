# SPDX-License-Identifier: Apache-2.0
"""omawake command line. Only `daemon` and `test` need the venv (numpy/onnx)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import aniaplayer as A
from . import config as C
from . import models as M
from . import wakeconf as W


def request(cmd: str, **kw) -> dict | None:
    """Send one command to the running daemon; None when it is not running."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(C.SOCKET_FILE))
        s.sendall((json.dumps({"cmd": cmd, **kw}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            part = s.recv(65536)
            if not part:
                break
            data += part
        s.close()
        return json.loads(data.decode() or "{}")
    except (OSError, ValueError):
        return None


def cmd_status(a) -> int:
    from .setup import readiness
    live = request("status")
    ready = readiness()
    cfg = C.load_config()
    if live:
        out = dict(live, running=True)
    else:
        out = {"running": False, "state": "stopped", "message": "Not running",
               "mode": cfg["mode"], "sensitivity": cfg["sensitivity"],
               "wake_word": "", "conversation": cfg["conversation"], "paused": False}
    out["setup"] = ready
    out["setup_ok"] = bool(ready["voxtype"] and ready["venv"] and ready["model"]
                           and (ready["service"] or out["running"]))
    out["wake_words"] = [{"id": m["id"], "phrase": m["phrase"], "status": m["status"]}
                         for m in M.listing(cfg)]
    if not live:
        out["words"] = words_offline(cfg)
        # the word(s) that would really load, not the configured name
        out["wake_word"] = ",".join(w["model"] for w in out["words"] if w["active"])
        wc = W.load(cfg)
        out.update(config_source=wc.source, config_errors=wc.errors[:5],
                   commands_allowed=wc.commands_allowed, unsafe_reason=wc.unsafe_reason)
    out["configured_wake_word"] = next((w["configured"] for w in out.get("words") or []
                                        if w.get("configured")), None)
    out["config_file"] = str(W.TOML_FILE)
    out["config_exists"] = W.TOML_FILE.exists()
    if a.json:
        print(json.dumps(out))
        return 0
    print(f"omawake: {out['state']}" + (f" — {out.get('message')}" if out.get("message") else ""))
    ww = out.get("wake_word") or "none"
    if out.get("configured_wake_word"):
        ww += f" (configured {out['configured_wake_word']} is not usable here)"
    print(f"  mode {out['mode']} · sensitivity {out['sensitivity']} · wake word {ww}"
          f" · conversation {'on' if out.get('conversation') else 'off'}")
    if live and live.get("detector"):
        print(f"  detector {live['detector']}")
    for w in out.get("words") or []:
        print(f"  {'on ' if w['enabled'] else 'off'} {w['model']:14} {w['describe']}" + word_note(w))
    if out.get("unsafe_reason"):
        print(f"  ! commands disabled: {out['unsafe_reason']}")
    for k, v in ready.items():
        print(f"  {k:16} {v}")
    if not out["setup_ok"]:
        print("  -> run: omawake setup")
    return 0


def _set(key: str, value) -> int:
    C.save_config({key: value})
    r = request("reload")
    print(f"{key} = {value}" + ("" if r else " (daemon not running; saved for next start)"))
    return 0


def words_offline(cfg: dict) -> list[dict]:
    """Same choice the daemon makes (fallback, Ania Player's words skipped)."""
    return M.effective_words(cfg, skip=A.words_in_use(cfg))[1]


def word_note(w: dict) -> str:
    if w.get("ania_player"):
        return "  (skipped: Ania Player uses this word)"
    if not w["installed"]:
        return "  (not installed: omawake models)"
    return ""


def cmd_words(a) -> int:
    cfg = C.load_config()
    live = request("status")
    words = (live or {}).get("words") or words_offline(cfg)
    wc = W.load(cfg)
    if a.json:
        print(json.dumps({"words": words, "source": wc.source, "path": wc.path,
                          "errors": wc.errors, "warnings": wc.warnings,
                          "commands_allowed": wc.commands_allowed, "unsafe_reason": wc.unsafe_reason,
                          "specs": [s.to_json() for s in wc.specs]}))
        return 0
    src = wc.path if wc.source == "toml" else f"config.json (no {W.TOML_FILE.name} yet: omawake config init)"
    print(f"Wake words from {src}")
    scores = (live or {}).get("scores") or {}
    for w in words:
        state = "on " if w["enabled"] else "off"
        miss = word_note(w)
        sc = f"  score {scores[w['model']]:.2f}" if w["model"] in scores else ""
        print(f"  [{state}] {w['model']:14} “{w['phrase']}” -> {w['describe']}{miss}{sc}")
    for s in wc.specs:
        if s.action == "command" and s.commands and a.verbose:
            print(f"\n  {s.model} phrases:")
            for ph, c in s.commands.items():
                print(f"    {ph!r:28} {c}")
    for e in wc.errors:
        print(f"  error: {e}")
    for w in wc.warnings:
        print(f"  warning: {w}")
    if wc.unsafe_reason:
        print(f"  ! commands are disabled: {wc.unsafe_reason}")
    return 1 if wc.errors else 0


def cmd_enable(value: bool):
    def run(a) -> int:
        cfg = C.load_config()
        wc = W.load(cfg)
        if wc.get(a.model) is None:
            print(f"'{a.model}' is not in {wc.path if wc.source == 'toml' else 'your config'}."
                  f" Known: {', '.join(s.model for s in wc.specs) or 'none'}")
            return 1
        overlay = dict(cfg.get("word_enabled") or {})
        overlay[a.model] = value
        C.save_config({"word_enabled": overlay})
        r = request("reload")
        print(f"{a.model} {'enabled' if value else 'disabled'}" + ("" if r else " (daemon not running)"))
        return 0
    return run


def cmd_add(a) -> int:
    """omawake add <wake word> [--say PHRASE] -- <command ...>"""
    path = W.TOML_FILE
    argv = a.run[1:] if a.run[:1] == ["--"] else a.run
    run = (argv[0] if len(argv) == 1 else __import__("shlex").join(argv)).strip() if argv else ""
    if not run:
        print("nothing to run: omawake add <wake word> [--say \"phrase\"] -- <command>")
        return 2
    if not path.exists():
        cfg = C.load_config()
        order = [m["id"] for m in M.catalog().get("models", [])]
        model, inst = W.starter_model(M.installed(cfg), A.words_in_use(cfg), order)
        W.write_template(path, model, inst)
        print(f"created {path}")
    unsafe = W.check_file_safe(path)
    if unsafe:
        print(f"refused: {unsafe}")
        return 1
    try:
        new = W.add_command(path.read_text(encoding="utf-8"), a.model, run, a.say)
    except ValueError as e:
        print(f"not added: {e}")
        return 1
    C.atomic_write(path, new)
    os.chmod(path, 0o600)
    what = f"“{a.model}”, then “{a.say}”" if a.say else f"“{a.model}”"
    print(f"added: {what} -> {run}")
    if a.model not in M.installed(C.load_config()):
        print(f"  {a.model} is not installed yet: omawake models download {a.model}")
    r = request("reload")
    print("  daemon reloaded" if r is not None else "  (daemon not running; it picks this up on start)")
    return 0


def editor_argv(path) -> list[str]:
    if shutil.which("omarchy-launch-editor"):
        return ["omarchy-launch-editor", str(path)]
    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if ed and shutil.which("xdg-terminal-exec"):
        return ["xdg-terminal-exec", *__import__("shlex").split(ed), str(path)]
    return ["xdg-open", str(path)]


def cmd_config(a) -> int:
    path = W.TOML_FILE
    if a.action == "path":
        print(path)
        return 0
    if a.action in ("init", "edit"):
        cfg = C.load_config()
        skip = A.words_in_use(cfg)
        order = [m["id"] for m in M.catalog().get("models", [])]
        model, inst = W.starter_model(M.installed(cfg), skip, order)
        if W.write_template(path, model, inst):
            print(f"created {path} (wake word: {model})")
            if skip:
                print(f"  skipped {', '.join(sorted(skip))}: Ania Player already listens for it")
            if not inst:
                print(f"  {model} is not installed yet: omawake models download {model}")
            print("  “Hey Omarchy” is coming soon; it is in the file, commented out.")
            print(f"  {M.DONATE_LINE}")
        elif a.action == "init":
            print(f"{path} already exists")
        if a.action == "edit":
            subprocess.Popen(editor_argv(path), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    # check
    wc = W.load(C.load_config(), path)
    if wc.source == "legacy":
        print(f"no {path} (using config.json: {wc.specs[0].model} -> {wc.specs[0].action}). Create one: omawake config init")
        return 0
    print(f"{path}: {len(wc.specs)} wake word(s), {len(wc.errors)} error(s)")
    for e in wc.errors:
        print(f"  error: {e}")
    for w in wc.warnings:
        print(f"  warning: {w}")
    print("  security: " + ("ok, commands may run" if wc.commands_allowed else f"REFUSED: {wc.unsafe_reason}"))
    r = request("reload")
    if r is not None:
        print("  daemon reloaded")
    return 1 if wc.errors or not wc.commands_allowed else 0


def cmd_match(a) -> int:
    """Dry check of the phrase matcher: which command would this text run?"""
    from . import commands as K
    wc = W.load(C.load_config())
    specs = [s for s in wc.specs if s.action == "command" and s.commands and (not a.model or s.model == a.model)]
    if not specs:
        print("no voice-command wake word in the config")
        return 1
    for s in specs:
        hit = K.match(a.text, s.commands, wc.match_cutoff)
        if hit:
            print(f"{s.model}: “{a.text}” -> {hit[0]!r} ({hit[2]:.2f}) -> {hit[1]}  (not run)")
        else:
            print(f"{s.model}: “{a.text}” -> no match" + (" -> omarchy agent prompt" if s.fallback == "agent" else ""))
    return 0


def cmd_simple(name):
    def run(a) -> int:
        kw = {k: getattr(a, k) for k in ("mode", "text", "wake") if getattr(a, k, None)}
        r = request(name, **kw)
        if r is None:
            print("omawake is not running (start it: systemctl --user start omawake, or omawake daemon)")
            return 1
        print(json.dumps(r))
        return 0 if r.get("ok") else 1
    return run


def _test_words(cfg: dict, only: str | None):
    """[(name, bundle, shift, threshold, cooldown)] for the enabled words."""
    from .engine import WordSpec
    have = M.installed(cfg)
    wc = W.load(cfg)
    gshift = C.SENSITIVITY_SHIFT.get(cfg["sensitivity"], 0.0)
    out, missing = [], []
    for s in wc.specs:
        if only and s.model != only:
            continue
        if not only and not s.enabled:
            continue
        if s.model not in have:
            missing.append(s.model)
            continue
        shift = C.SENSITIVITY_SHIFT.get(s.sensitivity, gshift) if s.sensitivity else gshift
        out.append(WordSpec(s.model, have[s.model], shift, s.threshold, s.cooldown_s))
    if only and not out and only in have:
        out.append(WordSpec(only, have[only], gshift))
    if not out and not only and wc.source == "legacy":
        found = M.resolve(cfg)
        if found:
            out.append(WordSpec(found[0], found[1], gshift))
    return out, missing


def cmd_test(a) -> int:
    from .audio import MicStream
    from .engine import MultiDetector, pad_pcm, pcm_chunks
    from .arbiter import Arbiter
    from . import wake_core as ref
    cfg = C.load_config()
    words, missing = _test_words(cfg, a.wake)
    for m in missing:
        print(f"(skipping {m}: not installed)")
    if not words:
        print("No wake word model installed. Run: omawake setup")
        return 1
    if a.selftest:
        rc = 0
        for w in words:
            if not (w.bundle / "golden_stream.json").is_file():
                print(f"selftest {w.name}: skipped (bundle has no golden files)")
                continue
            fails: list = []
            ref._check_stream_golden(w.bundle, fails, True)
            print(f"selftest {w.name}:", "PASS" if not fails else "FAIL")
            for f in fails[:20]:
                print("  ", f)
            rc |= bool(fails)
        return rc
    det = MultiDetector(words)
    if a.wav:
        from pathlib import Path
        pcm = ref.read_wav_int16(Path(a.wav))
        if not a.no_pad:
            pcm = pad_pcm(pcm)
        events = det.run_pcm(pcm)
        print(json.dumps({"words": det.names, "file": a.wav, "events": events}))
        return 0 if events else 3
    wc = W.load(cfg)
    arb = Arbiter(wc.arbitration_ms / 1000.0, wc.settle_ms / 1000.0)
    print(f"Live scores — {det.describe()}")
    print("Say a wake word. Ctrl+C to stop.\n")
    w_name = max(len(s.name) for s in det.slots)
    mic = MicStream(cfg.get("audio_target", ""))
    stream = mic.open()
    hits: dict[str, int] = {s.name: 0 for s in det.slots}
    log_lines: list[str] = []
    t0, k, first = time.monotonic(), 0, True
    tty = sys.stdout.isatty()
    try:
        for chunk in pcm_chunks(stream):
            out = det.process(chunk)
            f = out["features"]
            k += 1
            now = time.monotonic()
            for ev in out["events"]:
                arb.offer(ev["wake"], ev["margin"], now, ev)
            win = arb.poll(now)
            if win:
                hits[win["wake"]] += 1
                extra = f" verifier {win['verifier']:.3f}" if "verifier" in win else ""
                rival = f" (beat {', '.join(win['rivals'])})" if win["rivals"] else ""
                log_lines.append(f"WAKE {win['wake']} at {now - t0:6.1f}s peak {win['peak']:.3f}{extra}{rival}")
                if not tty:
                    print(log_lines[-1])
            if tty and k % 2 == 0:
                vad = f.vad_prob if f.vad_prob is not None else 0.0
                lines = [f"vad {vad:4.2f}  level {f.level_db:6.1f} dB  {now - t0:6.1f}s"]
                for r in out["per_wake"]:
                    sc = r.get("score") or 0.0
                    bar = "█" * int(sc * 30)
                    lines.append(f"{r['wake']:<{w_name}} {sc:5.3f} thr {r['thr_eff']:5.3f} |{bar:<30}| wakes {hits[r['wake']]}")
                lines += log_lines[-3:] + [""] * (3 - len(log_lines[-3:]))
                if not first:
                    sys.stdout.write(f"\x1b[{len(lines)}F")
                sys.stdout.write("".join(f"\x1b[2K{ln}\n" for ln in lines))
                sys.stdout.flush()
                first = False
            if a.seconds and k * 0.08 >= a.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        mic.close()
    print(f"\n{sum(hits.values())} wake(s) in {time.monotonic() - t0:.0f}s: "
          + ", ".join(f"{k} {v}" for k, v in hits.items()))
    return 0


def cmd_models(a) -> int:
    cfg = C.load_config()
    if a.action == "import":
        if not a.id:
            print("usage: omawake models import <pack.zip | folder | manifest.json> [--replace]")
            return 2
        try:
            name, _ = M.import_bundle(Path(a.id), replace=a.replace)
        except (RuntimeError, OSError) as err:
            print(f"omawake: {err}")
            return 1
        request("reload")
        print(f"  use it: omawake add {name} -- <command>   or   omawake config edit (action ask / dictate)")
        return 0
    if a.action == "download":
        if not a.id:
            print("usage: omawake models download <id> [--accept-license]")
            return 2
        e = M.entry(a.id)
        accept = a.accept_license
        if e and e.get("kind") == "upstream-head" and not accept:
            print(M.license_notice(e))
            if sys.stdin.isatty():
                accept = input("Accept this licence and download? [y/N] ").strip().lower() in ("y", "yes")
            if not accept:
                print("Not downloaded. Re-run with --accept-license to accept it.")
                return 1
        try:
            M.download(a.id, accept_license=accept)
        except (PermissionError, RuntimeError, ValueError, OSError) as err:
            print(f"omawake: {err}")
            return 1
        request("reload")
        return 0
    items = M.listing(cfg)
    if a.json:
        print(json.dumps(items))
        return 0
    active = {s.model for s in W.load(cfg).enabled()}
    for m in items:
        cur = "*" if m["id"] in active else " "
        lic = m.get("license") or ""
        print(f"{cur} {m['id']:16} {m['phrase']!s:20} {m['status']:10} {lic:18} {m.get('path') or ''}")
    return 0


def cmd_logs(a) -> int:
    if a.open:
        cmd = f"tail -n 200 -F '{C.LOG_FILE}'"
        subprocess.Popen(["uwsm-app", "--", "xdg-terminal-exec", "--app-id=org.omarchy.omawake-logs",
                          "--title=omawake logs", "-e", "bash", "-c", cmd], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    if a.follow:
        return subprocess.call(["tail", "-n", "100", "-F", str(C.LOG_FILE)])
    try:
        print("".join(C.LOG_FILE.read_text(errors="replace").splitlines(True)[-100:]), end="")
    except OSError:
        print("(no log yet)")
    return 0


def cmd_open_setup(a) -> int:
    """Used by the widget's "Finish setup" button: setup in a floating terminal."""
    from .setup import BIN
    subprocess.Popen(["omarchy-launch-floating-terminal-with-presentation", f"'{BIN}' setup"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser(prog="omawake", description="Wake word for Omarchy (unofficial community plugin)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("daemon", help="run the wake word daemon in the foreground")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="log commands and agent prompts instead of running them")
    s = sub.add_parser("status", help="show state and setup checks")
    s.add_argument("--json", action="store_true")
    for name, hlp in (("pause", "stop listening (mic released)"), ("resume", "listen again"),
                      ("toggle", "pause/resume"), ("cancel", "abort the current turn"),
                      ("reload", "re-read config.json and config.toml")):
        sub.add_parser(name, help=hlp).set_defaults(fn=cmd_simple(name))
    s = sub.add_parser("trigger", help="act as if the wake word was heard")
    s.add_argument("--mode", choices=W.ACTIONS)
    s.add_argument("--wake", help="act as this wake word (its action and commands)")
    s.add_argument("--text", help="testing: skip the first recording and send this text")
    s.set_defaults(fn=cmd_simple("trigger"))
    s = sub.add_parser("mode", help="ask | dictate")
    s.add_argument("value", choices=C.MODES)
    s.set_defaults(fn=lambda a: _set("mode", a.value))
    s = sub.add_parser("sensitivity", help="low | normal | high")
    s.add_argument("value", choices=C.SENSITIVITIES)
    s.set_defaults(fn=lambda a: _set("sensitivity", a.value))
    s = sub.add_parser("wake", help="choose the wake word (see `omawake models`)")
    s.add_argument("value")
    s.set_defaults(fn=lambda a: _set("wake_word", a.value))
    s = sub.add_parser("conversation", help="keep listening after a reply: on | off")
    s.add_argument("value", choices=("on", "off"))
    s.set_defaults(fn=lambda a: _set("conversation", a.value == "on"))
    s = sub.add_parser("models", help="list / download / import wake words")
    s.add_argument("action", nargs="?", choices=("list", "download", "import"), default="list")
    s.add_argument("id", nargs="?", help="wake word id, or for import: a pack .zip / folder / manifest.json")
    s.add_argument("--replace", action="store_true", help="import: overwrite an installed wake word")
    s.add_argument("--json", action="store_true")
    s.add_argument("--accept-license", action="store_true",
                   help="accept a non-commercial upstream licence (hey_jarvis, alexa, hey_mycroft)")
    s.set_defaults(fn=cmd_models)
    s = sub.add_parser("words", help="wake words in your config and their actions")
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="also list voice command phrases")
    s.set_defaults(fn=cmd_words)
    for name, val in (("enable", True), ("disable", False)):
        s = sub.add_parser(name, help=f"turn a wake word {'on' if val else 'off'}")
        s.add_argument("model")
        s.set_defaults(fn=cmd_enable(val))
    s = sub.add_parser("add", help="run a command when a wake word (and optional phrase) is heard",
                       description="Adds a Linux command to config.toml. Examples:\n"
                       "  omawake add hey_jarvis -- omarchy-launch-browser\n"
                       "  omawake add computer --say \"open notes\" -- obsidian\n"
                       "  omawake add computer --say \"deploy\" -- ~/bin/deploy.sh staging",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("model", help="wake word id (see `omawake models`)")
    s.add_argument("--say", help="phrase to say after the wake word (voice command); omit to run on the wake word alone")
    s.add_argument("run", nargs="*", help="command and its arguments (put them after --)")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("config", help="config.toml: path | init | edit | check")
    s.add_argument("action", nargs="?", choices=("path", "init", "edit", "check"), default="check")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("match", help="which voice command would this text run? (nothing is run)")
    s.add_argument("text")
    s.add_argument("--model")
    s.set_defaults(fn=cmd_match)
    s = sub.add_parser("test", help="live scores for all active wake words")
    s.add_argument("--wake", help="only this wake word")
    s.add_argument("--seconds", type=float, default=0)
    s.add_argument("--selftest", action="store_true", help="check the model against its golden files")
    s.add_argument("--wav", help="score a 16 kHz mono WAV file instead of the mic")
    s.add_argument("--no-pad", action="store_true",
                   help="with --wav: don't add 1.5 s of silence before and after the clip")
    s.set_defaults(fn=cmd_test)
    s = sub.add_parser("setup", help="first-run setup (idempotent)")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--no-service", action="store_true")
    s.add_argument("--no-link", action="store_true")
    s = sub.add_parser("open-setup", help="run setup in a floating terminal")
    s.set_defaults(fn=cmd_open_setup)
    s = sub.add_parser("uninstall", help="remove service, runtime and models")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--keep-config", action="store_true")
    s = sub.add_parser("logs", help="show the log")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--open", action="store_true", help="open in a terminal window")
    s.set_defaults(fn=cmd_logs)

    a = p.parse_args(argv)
    if a.cmd == "daemon":
        from .daemon import main as dmain
        return dmain(a.verbose, a.dry_run)
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "setup":
        from .setup import run_setup
        return run_setup(a.yes, not a.no_service, not a.no_link)
    if a.cmd == "uninstall":
        from .setup import run_uninstall
        return run_uninstall(a.yes, a.keep_config)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
