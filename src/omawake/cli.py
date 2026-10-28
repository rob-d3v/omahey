# SPDX-License-Identifier: Apache-2.0
"""omawake command line. Only `daemon` and `test` need the venv (numpy/onnx)."""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time

from . import config as C
from . import models as M


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
               "wake_word": cfg["wake_word"], "conversation": cfg["conversation"], "paused": False}
    out["setup"] = ready
    out["setup_ok"] = bool(ready["voxtype"] and ready["venv"] and ready["model"]
                           and (ready["service"] or out["running"]))
    out["wake_words"] = [{"id": m["id"], "phrase": m["phrase"], "status": m["status"]}
                         for m in M.listing(cfg)]
    if a.json:
        print(json.dumps(out))
        return 0
    print(f"omawake: {out['state']}" + (f" — {out.get('message')}" if out.get("message") else ""))
    print(f"  mode {out['mode']} · sensitivity {out['sensitivity']} · wake word {out.get('wake_word')}"
          f" · conversation {'on' if out.get('conversation') else 'off'}")
    if live and live.get("detector"):
        print(f"  detector {live['detector']}")
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


def cmd_simple(name):
    def run(a) -> int:
        kw = {k: getattr(a, k) for k in ("mode", "text") if getattr(a, k, None)}
        r = request(name, **kw)
        if r is None:
            print("omawake is not running (start it: systemctl --user start omawake, or omawake daemon)")
            return 1
        print(json.dumps(r))
        return 0 if r.get("ok") else 1
    return run


def cmd_test(a) -> int:
    from .audio import MicStream
    from .engine import Detector, pcm_chunks
    from . import wake_core as ref
    cfg = C.load_config()
    if a.wake:
        cfg["wake_word"] = a.wake
    found = M.resolve(cfg)
    if not found:
        print("No wake word model installed. Run: omawake setup")
        return 1
    wid, path = found
    if a.wav:
        from pathlib import Path
        det = Detector(path, C.SENSITIVITY_SHIFT.get(cfg["sensitivity"], 0.0))
        events, _ = det.run_pcm(ref.read_wav_int16(Path(a.wav)))
        print(json.dumps({"wake": wid, "file": a.wav, "events": events}))
        return 0 if events else 3
    if a.selftest:
        fails: list = []
        ref._check_stream_golden(path, fails, True)
        print("selftest:", "PASS" if not fails else "FAIL")
        for f in fails[:20]:
            print("  ", f)
        return 0 if not fails else 1
    det = Detector(path, C.SENSITIVITY_SHIFT.get(cfg["sensitivity"], 0.0))
    print(f"Live scores for {wid} — {det.describe()}")
    print("Say the wake word. Ctrl+C to stop.\n")
    mic = MicStream(cfg.get("audio_target", ""))
    stream = mic.open()
    hits, t0, k = 0, time.monotonic(), 0
    try:
        for chunk in pcm_chunks(stream):
            out = det.process(chunk)
            r, f = out["per_wake"][0], out["features"]
            sc = r.get("score") or 0.0
            k += 1
            bar = "█" * int(sc * 30)
            vad = f.vad_prob if f.vad_prob is not None else 0.0
            sys.stdout.write(f"\rscore {sc:5.3f} thr {r['thr_eff']:5.3f} vad {vad:4.2f} "
                             f"lvl {f.level_db:6.1f} dB |{bar:<30}| wakes {hits}")
            sys.stdout.flush()
            if out["event"]:
                hits += 1
                ev = out["event"]
                extra = f" verifier {ev['verifier']:.3f}" if "verifier" in ev else ""
                print(f"\n  WAKE at {time.monotonic() - t0:6.1f}s  peak {ev['peak']:.3f}{extra}")
            if a.seconds and k * 0.08 >= a.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        mic.close()
    print(f"\n{hits} wake(s) in {time.monotonic() - t0:.0f}s")
    return 0


def cmd_models(a) -> int:
    cfg = C.load_config()
    if a.action == "download":
        if not a.id:
            print("usage: omawake models download <id>")
            return 2
        M.download(a.id)
        request("reload")
        return 0
    items = M.listing(cfg)
    if a.json:
        print(json.dumps(items))
        return 0
    for m in items:
        cur = "*" if m["id"] == cfg.get("wake_word") else " "
        print(f"{cur} {m['id']:16} {m['phrase']!s:20} {m['status']:10} {m.get('path') or ''}")
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
    s = sub.add_parser("status", help="show state and setup checks")
    s.add_argument("--json", action="store_true")
    for name, hlp in (("pause", "stop listening (mic released)"), ("resume", "listen again"),
                      ("toggle", "pause/resume"), ("cancel", "abort the current turn")):
        sub.add_parser(name, help=hlp).set_defaults(fn=cmd_simple(name))
    s = sub.add_parser("trigger", help="act as if the wake word was heard")
    s.add_argument("--mode", choices=C.MODES)
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
    s = sub.add_parser("models", help="list / download wake words")
    s.add_argument("action", nargs="?", choices=("list", "download"), default="list")
    s.add_argument("id", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_models)
    s = sub.add_parser("test", help="live wake word scores from the mic")
    s.add_argument("--wake")
    s.add_argument("--seconds", type=float, default=0)
    s.add_argument("--selftest", action="store_true", help="check the model against its golden files")
    s.add_argument("--wav", help="score a 16 kHz mono WAV file instead of the mic")
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
        return dmain(a.verbose)
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
