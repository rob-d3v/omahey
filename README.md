# omawake

**Say a wake word and talk to your AI agent, hands-free.**

> Works today with `hey_jarvis`, `alexa` or `hey_mycroft` (openWakeWord,
> non-commercial licence), or a wake word you make yourself such as
> `ok_ania`. A free “Hey Omarchy” model (Apache-2.0) is coming soon; it is
> still training and can't be downloaded yet.

omawake is a wake word for [Omarchy](https://omarchy.org). Say the wake word
and it opens your Omarchy default agent (Codex, Claude Code, OpenCode, …),
listens through Omarchy's own dictation (Voxtype), types what you said and
presses Enter. Then it keeps listening for your follow-up until you say
“that's all”. There's also a Dictate mode that types at your cursor.

You can run several wake words at once and give each one its own job: for
example “Hey Jarvis” asks the agent, “Ok Ania” dictates, and “Hey Jarvis,
lock screen” runs `omarchy-system-lock` (once “Hey Omarchy” and “Omarchy”
ship, they can take those jobs). See [Wake words and actions](#wake-words-and-actions).

> **Unofficial community plugin, not affiliated with Omarchy or 37signals.**

<!-- DEMO: replace with a 30 s GIF (docs/demo.gif) -->
_Demo GIF coming soon._

- Runs 100% locally. No cloud, no account, no telemetry.
- Uses what Omarchy already ships: Voxtype for speech-to-text, `omarchy agent`
  for your agent, `wtype` for typing, PipeWire for the mic.
- A bar widget in the native style: mic state, click to pause, right click
  for your wake words, their actions and on/off switches.
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

> **“Hey Omarchy” and “Omarchy” are still training** and can't be downloaded
> yet. Until they ship, setup shows two ways to get going: `hey_jarvis`
> (an openWakeWord model with a non-commercial licence, see
> [Wake words](#wake-words)), or a wake word you make yourself. If you also
> run Ania Player (or [omaania](https://github.com/rob-d3v/omaania)),
> omawake leaves the player's wake word to the player, see
> [Ania Player](#ania-player-shared-wake-words).

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
| Python ≥ 3.11 `tomllib` | reads `config.toml` | stdlib |

## How it works

```
mic ─ pw-record ─► wake core (shared mel + embedding, N openWakeWord heads,
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
  second-stage `verifier.onnx`. It uses about 8 % of one CPU core and about 80 MB of RAM (measured with Silero and the verifier on), and stays there with five wake words.
- Voxtype stays in charge of speech-to-text. omawake drives it with
  `voxtype record start/stop`, so your Voxtype model, language, GPU setting
  and OSD all apply.
- omawake never edits `~/.config/voxtype/config.toml` or your keybindings. It
  uses per-recording flags such as `--file`, `--type` and `--no-auto-submit`.
- The daemon writes `$XDG_RUNTIME_DIR/omawake/state.json`. The bar widget
  watches that file and calls the `omawake` CLI, which talks to the daemon
  over a user-only unix socket.

## Wake words and actions

Without a config file, omawake listens for one wake word and uses the mode
from the widget (below). To run several wake words, each with its own
action, create `~/.config/omawake/config.toml`:

```bash
omawake config init     # writes a commented starter file (mode 0600) that works today
omawake config edit     # opens it with omarchy-launch-editor
omawake config check    # validates it, checks its permissions, reloads the daemon
```

`config init` picks a wake word you can use right now: `hey_omarchy` if its
model is installed, else one you already have (and that Ania Player is not
using), else `hey_jarvis` with the command to download it. `hey_omarchy` is
in the file commented out as "coming soon". Without a `config.toml`,
omawake switches to `hey_omarchy` by itself once its model is installed.

```toml
[[wake]]
model = "hey_omarchy"
action = "ask"              # ask | dictate | command | agent-prompt

[[wake]]
model = "omarchy"
action = "dictate"
sensitivity = "high"        # optional, per word: low | normal | high
# threshold = 0.6           # optional: replaces the model's calibrated threshold
# cooldown_s = 2            # optional: how soon this word may fire again

[[wake]]
model = "hey_jarvis"
action = "command"
command = "omarchy-launch-browser"     # runs right away, no speech needed

[[wake]]
model = "hey_mycroft"
action = "command"                     # voice commands: wake word, then a phrase
pack = "omarchy"                       # the built-in command pack (below)
commands = { "next background" = "omarchy-theme-bg-next", "files" = "omarchy-launch-nautilus" }
fallback = "agent"                     # unknown phrase -> omarchy agent prompt "<what you said>"
```

| action | what happens after the wake word |
|---|---|
| `ask` | Ask mode: the agent window opens, what you say is typed into it, conversation follows |
| `dictate` | Dictate mode: Voxtype types what you say at the cursor |
| `agent-prompt` | Voxtype transcribes what you say and runs `omarchy agent prompt "<text>"` (a new agent) |
| `command` + `command` | runs that one command |
| `command` + `commands` / `pack` | Voxtype transcribes a short phrase, omawake fuzzy-matches it against your phrase list and runs the match. Nothing is spoken back; the result shows in the widget and as a notification. No match: notification, or `fallback = "agent"` |

Phrase matching ignores case, punctuation and filler (“could you lock the
screen please” matches `lock screen`). When two phrases for different
commands are about equally close, nothing runs. Try it without a mic:
`omawake match "turn on the night light"` prints what would run.

Top-level options: `arbitration_ms` (default 400), `settle_ms` (1000),
`command_timeout_s` (15), `match_cutoff` (0.72); per word `timeout_s`,
`label`, `enabled = false`.

The widget lists the wake words with their actions and an on/off switch
for each, plus **Edit config…**. The switches are stored in `config.json`
(`word_enabled`), so omawake never rewrites your `config.toml`.

```bash
omawake words [-v]                 # active wake words, actions (and phrases with -v)
omawake enable|disable <model>
omawake test                       # live scores for every active head
omawake trigger --wake hey_jarvis --text "lock screen"   # simulate a wake word + phrase
omawake daemon --dry-run           # log commands instead of running them
```

### Built-in command pack (`pack = "omarchy"`)

Only commands that ship in `/usr/share/omarchy/bin`. An entry whose program
is missing on your system is skipped.

| say | runs |
|---|---|
| lock / lock screen | `omarchy-system-lock` |
| screenshot / take a screenshot | `omarchy-capture-screenshot` |
| screen recording | `omarchy-capture-screenrecording` (start/stop) |
| next background | `omarchy-theme-bg-next` |
| change theme | `omarchy-theme-switcher` |
| night light | `omarchy-toggle-nightlight` |
| open browser / browser | `omarchy-launch-browser` |
| open terminal / terminal | `omarchy-launch-terminal` |
| open files | `omarchy-launch-nautilus` |
| volume up / louder | `omarchy-audio-output-volume raise` |
| volume down / quieter | `omarchy-audio-output-volume lower` |
| mute | `omarchy-audio-output-volume mute-toggle` |
| do not disturb | `omarchy-toggle-notification-silencing` |
| stay awake | `omarchy-toggle-idle` |
| open menu | `omarchy-menu` |

Your own `commands` entries win over the pack.

### Several wake words at once

All wake words share one microphone stream and one melspectrogram +
embedding front-end (and one Silero VAD); only the small per-word heads and
verifiers are added. When two words fire within `arbitration_ms`, the one
whose peak went furthest over its own threshold wins, then nothing else
fires for `settle_ms` (about 1 s), and the winner waits for its cooldown.

Measured with the real daemon on this machine (30 s, idle room, Silero on,
one verifier, `--dry-run`):

| active wake words | daemon CPU (one core) | RSS |
|---|---|---|
| 1 | 7.9 % | 76 MB |
| 3 | 7.4 % | 78 MB |
| 5 | 8.2 % | 80 MB |

Offline, per 80 ms frame: 2.3 ms with 1 word, 2.65 ms with 3 or 5. A head
costs well under 0.1 ms; the shared front-end is nearly all of the work.
`pw-record` adds about 0.2 %.

## Modes

**Ask (default).** Say your wake word (e.g. “Hey Jarvis”), then your request:

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
omawake test --wav clip.wav    # score a 16 kHz mono WAV (padded with 1.5 s of silence; --no-pad)
omawake logs [-f | --open]
```

These set the single wake word used when there is no `config.toml`.
Settings live in `~/.config/omawake/config.json`. Other options there:
`conversation_timeout_s`, `end_silence_ms`, `first_speech_timeout_s`,
`dismiss_phrases`, `chime`, `submit_delay_ms`, `audio_target` (see below)
and `model_dirs`.

`audio_target` picks what omawake listens to (empty = the default source):

```jsonc
"audio_target": ""                                   // default mic
"audio_target": "alsa_input.usb-Blue_Yeti-00.analog-stereo"  // a mic: `pactl list short sources`
"audio_target": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"  // what a sink plays
"audio_target": "test"                               // a sink name works too
```

A sink or its `.monitor` is recorded with `stream.capture.sink=true` (plain
`pw-record --target X.monitor` records silence). Handy for testing with a
null sink: `pactl load-module module-null-sink sink_name=test`, set
`"audio_target": "test"`, `omawake reload`, then `paplay -d test clip.wav`.

## Wake words

| id | phrase | status | licence |
|---|---|---|---|
| `hey_omarchy` | “Hey Omarchy” | training, coming with the first release | Apache-2.0 |
| `omarchy` | “Omarchy” | training | Apache-2.0 |
| `hey_ania` | “Hey Ania” | your own model (see below) | yours |
| `hey_jarvis` | “Hey Jarvis” | download from upstream | CC BY-NC-SA 4.0 |
| `alexa` | “Alexa” | download from upstream | CC BY-NC-SA 4.0 |
| `hey_mycroft` | “Hey Mycroft” | download from upstream | CC BY-NC-SA 4.0 |

The catalog is in [`models.json`](models.json). omawake's own models are
GitHub release assets and are never committed to this repo. If you already
use Ania Player, omawake also picks up its wake words from
`~/.local/share/aniaplayer/wakeword/`.

### Ania Player: shared wake words

Ania Player (`aniaplayer.service`) has its own wake word engine. If both
listened for the same word, one “Ok Ania” would wake the avatar *and* your
agent. So while Ania Player is running with its wake word on, omawake skips
every word the player uses (read from the player's
`~/.config/aniaplayer/plugins/voice_assistant/voice_config.json`:
`wake_word.model_name`, `model_name_2`, `extra_wake_words`). It logs and
notifies once, `omawake status`/`omawake words` mark the word
“skipped: Ania Player uses this word”, and without a `config.toml` omawake
falls back to another installed word. It checks again every 20 seconds, so
starting, stopping or reconfiguring the player is picked up on its own.

To listen for the player's word anyway, set
`"skip_ania_player_words": false` in `~/.config/omawake/config.json` and run
`omawake reload`.

**openWakeWord pre-trained models.** `hey_jarvis`, `alexa` and
`hey_mycroft` are pre-trained heads from
[openWakeWord](https://github.com/dscripka/openWakeWord) by David Scripka.
Its code is Apache-2.0, but the pre-trained models are licensed
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
(non-commercial, attribution, share-alike). So omawake does **not** bundle
or redistribute them. It downloads the head from the upstream v0.5.1
release (pinned sha256) only after you accept the licence, and wraps it
into a local bundle with a `LICENSE-NOTICE.txt`:

```bash
omawake models                                    # list, with licences
omawake models download hey_jarvis                # shows the licence and asks
omawake models download hey_jarvis --accept-license
```

The front-end comes from a bundle you already have (bit-identical to
upstream's), so these heads share the same stream as your other words.

**Create your own wake word.** The widget menu links to
[aniamodels.shop/create-wakeword](https://aniamodels.shop/create-wakeword).
To use the model you get there, unpack the download into
`~/.local/share/omawake/models/` so that its `manifest.json` ends up at
`~/.local/share/omawake/models/<id>/manifest.json`, then select it in the
widget or run `omawake wake <id>`.

## Security

Voice commands run programs, so omawake is strict about where they come from.

- **Commands come only from your own `config.toml`.** Speech only *selects*
  one of the phrases you wrote; transcribed text is never run, never
  passed to a shell, and never added to a command's arguments.
- The one place speech reaches a program is `agent-prompt` (and
  `fallback = "agent"`): it runs `omarchy-agent-prompt "<text>"` with the
  transcript as a single argument. Note that `omarchy agent` starts your
  agent in its unattended mode, so only use these actions if you are
  comfortable with what your agent may do on a spoken prompt.
- Commands run as an argv list (`shlex.split`, `shell=False`): `;`, `|`,
  `$(…)`, `&&`, globs and redirects are plain text. Need a pipeline? Put it in
  a script you own and call the script.
- Each command has a timeout (`command_timeout_s`, default 15 s; per word
  `timeout_s`). It runs in its own process group with no stdin, and the
  whole group is stopped on timeout. Launchers such as `omarchy-launch-*`
  return right away, so GUI apps are not affected.
- Every run is logged (`omawake logs`): the argv, which wake word and phrase
  chose it, the exit status and the time it took.
- **Permission check.** omawake refuses to run *any* command from
  `config.toml` unless the file (after following symlinks) is a regular file
  owned by you and not writable by group or others, and its folder is owned
  by you (or root) and not writable by others. Otherwise the widget and
  `omawake config check` show why, and `ask` / `dictate` keep working. Fix it
  with `chmod go-w ~/.config/omawake/config.toml`. `omawake config init`
  creates the file with mode 0600.
- omawake only reads `config.toml`, and only creates it when you run
  `config init`/`config edit`. The daemon's control socket is user-only
  (0600).
- To check a config without risk: `omawake match "<phrase>"` (runs nothing)
  and `omawake daemon --dry-run`.

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
| It wakes by itself | `omawake sensitivity low`, or `sensitivity = "low"` / a higher `threshold` for that word in `config.toml` |
| “Commands disabled” in the widget | `config.toml` is writable by others: `chmod go-w ~/.config/omawake/config.toml` |
| A voice command runs the wrong thing | `omawake match "what you said"`; add your own phrase, or raise `match_cutoff` |
| Status says a word is “skipped: Ania Player uses this word” | Ania Player listens for it; pick another word (`omawake config edit`), or see [Ania Player](#ania-player-shared-wake-words) |
| The wrong wake word wins | Lower that word's `sensitivity`, or raise its `threshold` |
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
  The optional `hey_jarvis`, `alexa` and `hey_mycroft` heads are openWakeWord
  pre-trained models (CC BY-NC-SA 4.0), downloaded by you from upstream, not
  distributed here.
- [Silero VAD](https://github.com/snakers4/silero-vad) (MIT).
- [Voxtype](https://voxtype.io) for speech-to-text, and Omarchy for the agent launcher and the shell.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
Omarchy is a trademark of its owner. This project is unofficial and is not
affiliated with or endorsed by Omarchy or 37signals.
