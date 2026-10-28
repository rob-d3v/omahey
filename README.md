# omawake

**Say “Hey Omarchy” and talk to your AI agent, hands-free.**

omawake is a wake word for [Omarchy](https://omarchy.org). Say the wake word
and it opens your Omarchy default agent (Codex, Claude Code, OpenCode, …),
listens through Omarchy's own dictation (Voxtype), types what you said and
presses Enter. Then it keeps listening for your follow-up until you say
“that's all”. There's also a Dictate mode that types at your cursor.

> **Unofficial community plugin, not affiliated with Omarchy or 37signals.**

<!-- DEMO: replace with a 30 s GIF (docs/demo.gif) -->
_Demo GIF coming soon._

- Runs 100% locally. No cloud, no account, no telemetry.
- Uses what Omarchy already ships: Voxtype for speech-to-text, `omarchy agent`
  for your agent, `wtype` for typing, PipeWire for the mic.
- A bar widget in the native style: mic state, click to pause, right click for settings.
- English first.

## Install

```bash
omarchy plugin add https://github.com/rob-d3v/omawake --enable
```

Then click the omawake mic icon in the bar and press **Finish setup**. The
setup runs in a terminal, and you can run it again any time:

```bash
~/.config/omarchy/plugins/io.github.rob-d3v.omawake/bin/omawake setup
```

Setup checks each step and only acts when something is missing:

1. **Voxtype.** If it's missing, it offers to run `omarchy-voxtype-install`
   (which asks for your password), and starts the Voxtype daemon if it's stopped.
2. **Default agent.** Checks `omarchy default agent`. Ask mode needs one.
3. **Python runtime.** Runs `uv sync --frozen` into `~/.local/share/omawake/venv`.
   It offers `omarchy-pkg-add uv` if uv is missing.
4. **Wake word model.** Downloads the model from this repo's GitHub releases
   and checks its sha256.
5. **Command line.** Links `~/.local/bin/omawake`.
6. **Background service.** Installs and starts the `omawake.service` systemd user unit.

### Requirements and dependencies

| What | Why | Shipped by Omarchy? |
|---|---|---|
| Voxtype ≥ 1.1 | speech-to-text, OSD | via `omarchy-voxtype-install` |
| `wtype` | types into the agent window | yes |
| PipeWire (`pw-record`, `pw-play`) | microphone, chimes | yes |
| `hyprctl` | finds and focuses the agent window | yes |
| `uv` | locked Python environment | offered by setup |
| Python ≥ 3.11 + `numpy`, `onnxruntime` | wake word engine (locked in `uv.lock`) | installed by setup into its own venv |
| A wake word bundle (~2 MB) | the model | downloaded by setup |

## How it works

```
mic ─ pw-record ─► wake core (openWakeWord head + mel + embedding,
                    Silero VAD gate, optional verifier) ──► wake!
                                                             │
          ┌───────────── chime, bar icon lights up ◄─────────┘
          ▼
  voxtype record start --file  ──►  you talk  ──►  omawake's VAD hears silence
          │                                               │
  omarchy agent opens/focuses (in parallel)   voxtype record stop --wait
          │                                               │
          └──────►  wtype types the transcript into the agent, Enter
                     └─► conversation: listen again, no wake word needed
```

- The wake engine is the wake-core runtime from Ania Player: an
  openWakeWord-compatible ONNX head on top of the frozen melspectrogram and
  embedding models, a Silero VAD gate, an adaptive threshold and an optional
  second-stage `verifier.onnx`. It uses about 7 % of one CPU core and about 90 MB of RAM (measured with Silero and the verifier on).
- Voxtype stays in charge of speech-to-text. omawake drives it with
  `voxtype record start/stop`, so your Voxtype model, language, GPU setting
  and OSD all apply.
- omawake never edits `~/.config/voxtype/config.toml` or your keybindings. It
  uses per-recording flags such as `--file`, `--type` and `--no-auto-submit`.
- The daemon writes `$XDG_RUNTIME_DIR/omawake/state.json`. The bar widget
  watches that file and calls the `omawake` CLI, which talks to the daemon
  over a user-only unix socket.

## Modes

**Ask (default).** Say “Hey Omarchy”, then your request:

1. A chime plays, the mic icon lights up, and Voxtype starts recording right away.
2. Your agent window (`omarchy agent`, class `org.omarchy.agent`) opens, or
   gets focus if it's already open.
3. After about 1 s of silence, Voxtype transcribes. omawake types the text
   into the agent and presses Enter.
4. **Conversation:** omawake keeps listening without the wake word. Talk again
   to send a follow-up. Say “that's all”, “dismiss”, “goodbye” or “never mind”
   to stop. It also stops after 20 s of silence.

**Dictate.** Say the wake word, then talk. Voxtype types at your cursor in
whatever app has focus, and stops when you pause.

Switch modes in the widget, or from the command line:

```bash
omawake mode ask|dictate
omawake sensitivity low|normal|high
omawake wake <id>              # see: omawake models
omawake conversation on|off
omawake status | pause | resume | toggle
omawake test                   # live scores from your mic
omawake test --selftest        # check the model against its golden files
omawake logs [-f | --open]
```

Settings live in `~/.config/omawake/config.json`. Other options there:
`conversation_timeout_s`, `end_silence_ms`, `first_speech_timeout_s`,
`dismiss_phrases`, `chime`, `submit_delay_ms`, `audio_target` (a PipeWire
source name) and `model_dirs`.

## Wake words

| id | phrase | status |
|---|---|---|
| `hey_omarchy` | “Hey Omarchy” | training, coming with the first release |
| `omarchy` | “Omarchy” | training |

The catalog is in [`models.json`](models.json). The model files are GitHub
release assets and are never committed to this repo. If you already use
Ania Player, omawake also picks up its wake words from
`~/.local/share/aniaplayer/wakeword/`.

**Create your own wake word.** The widget menu links to
[aniamodels.shop/create-wakeword](https://aniamodels.shop/create-wakeword).
To use the model you get there, drop the bundle folder into
`~/.local/share/omawake/models/` and select it in the widget.

## Privacy

- All audio stays on your machine. omawake reads the mic through PipeWire,
  checks it for the wake word in memory, and keeps no audio.
- Speech-to-text is your local Voxtype. Transcripts go only to the agent
  window you chose, plus a line in the local log.
- The only network access is the model download during setup, over HTTPS
  with a sha256 check.
- Setup only installs things after you confirm: Voxtype through
  `omarchy-voxtype-install` and uv through `omarchy-pkg-add`. It also manages
  the `omawake.service` systemd user unit. The plugin never runs code
  downloaded at runtime; model bundles are data files (ONNX + JSON).
- Pause from the widget, or run `omawake pause`. This closes the mic stream
  completely, and the `omawake` stream disappears from PipeWire.

## Troubleshooting

| Symptom | Try |
|---|---|
| The icon shows a gear | Setup isn't finished: click it, or run `omawake setup` |
| It never wakes | Run `omawake test` and watch the score while you say the wake word. Try `omawake sensitivity high`. Check that the right mic is the default source, or set `audio_target` |
| It wakes by itself | `omawake sensitivity low` |
| “Voxtype is not running” | `systemctl --user status voxtype`, or re-run `omawake setup` |
| “No default agent yet” | `omarchy default agent codex` (or claude, opencode, …) |
| The text arrives but isn't sent | Raise `submit_delay_ms` (some TUIs treat fast input followed by Enter as a paste) |
| The first word of a follow-up is lost | Pause briefly after the agent answers |
| Anything else | `omawake logs --open`, `journalctl --user -u omawake` |

## Uninstall

```bash
omawake uninstall                                  # service, venv, models, link
omarchy plugin remove io.github.rob-d3v.omawake    # the bar widget
```

`omawake uninstall` asks before it deletes your settings in `~/.config/omawake`.
It doesn't touch Voxtype or your agent.

## Development

```bash
git clone https://github.com/rob-d3v/omawake && cd omawake
UV_PROJECT_ENVIRONMENT=~/.local/share/omawake/venv uv sync --frozen
PYTHONPATH=src ~/.local/share/omawake/venv/bin/python -m unittest discover -s tests
omarchy plugin validate .
./bin/omawake daemon -v                            # foreground
./bin/omawake trigger --text "hello"               # simulate a wake + transcript
```

## Credits

- Wake engine: wake-core from [Ania Player](https://aniamodels.shop), by the same author.
- [openWakeWord](https://github.com/dscripka/openWakeWord) front-end models (Apache-2.0).
- [Silero VAD](https://github.com/snakers4/silero-vad) (MIT).
- [Voxtype](https://voxtype.io) for speech-to-text, and Omarchy for the agent launcher and the shell.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
Omarchy is a trademark of its owner. This project is unofficial and is not
affiliated with or endorsed by Omarchy or 37signals.
