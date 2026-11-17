# Larmor

Talk to your coding agent, and have it talk back while it works.

Larmor gives any agent that speaks MCP a voice and ears: Claude Code, Claude Desktop, Codex,
Gemini CLI, Antigravity, or anything else that can add an MCP server. You can talk over it and it stops; say
"mm-hmm" and it carries on. What you say while it's busy editing files reaches it mid-task.

Two voices, your choice at install:

- **Gemini Live** (default when a `GEMINI_API_KEY` is in Larmor's `.env` file): Gemini listens, talks, and chats with
  you while your agent works, and copes with a non-native accent. It hands your requests to the
  agent and says the agent's replies in its own voice. About $0.50 an hour on your Google API
  key; your audio goes to Google.
- **Local**: Parakeet and Chatterbox on your Mac. No audio leaves it, no API keys, 3.7 GB of models.

[larmor.dev](https://larmor.dev)

## Install

Apple Silicon Mac, macOS 14 or later. With the local voice: about 6 GB of free disk, and the
engine uses about 3 GB of memory while it runs (16 GB Macs are comfortable, 8 GB works but is
tight). With Gemini: under 1 GB of either.

```bash
curl -fsSL https://larmor.dev/install.sh | bash
```

That one-liner downloads this repo as a tarball from larmor.dev and runs [`install.sh`](install.sh).
If you'd rather read it first, or run it straight from source:

```bash
git clone https://github.com/naveenvasou/larmor.git ~/.larmor/app
~/.larmor/app/install.sh
```

The installer asks which voice you want (`LARMOR_BACKEND=gemini` or `local` skips the question),
asks for an email (optional, Enter skips it), finds the agents on your Mac and lets you pick
which ones get Larmor, then, for the local voice, downloads about 3.7 GB of models with a
progress bar. The first time you use it, macOS asks your terminal for microphone access.

For Gemini, the installer creates a `.env` file in Larmor's own folder (`~/.larmor/app/.env`
for the one-line install, the checkout's root if you run from source), readable by you only,
with an empty `GEMINI_API_KEY=` line: fill it in with your editor. Every Larmor process loads that file at
start, so nothing goes in a shell profile or an agent's config, and the installer never asks
for the key on the terminal, where it would end up in a history or a log. Any `LARMOR_*`
setting can live in that file too. Use a paid-tier key: Google's free tier may train on what
you say.

## Start talking

Open a new session in your agent:

- **Claude Code:** type `/larmor`
- **Claude Desktop:** quit and reopen it (⌘Q), then ask for "voice mode" in a new chat
- **Codex:** type `$larmor`. Run `/hooks` once and trust Larmor's hooks.
- **Anything else:** ask it for "voice mode"

For an agent the installer doesn't know, add the output of `~/.larmor/app/install.sh --print`
to its MCP settings.

## What the installer changes on your Mac

| Where | What |
|---|---|
| `~/.larmor/` | The app (with its `.env`: your voice choice and, for Gemini, your API key, mode 600), its Python environment, your email, and logs |
| `~/Library/LaunchAgents/dev.larmor.menubar.plist` | The menu-bar app, started at login. It runs the speech engine on `127.0.0.1:8160` and remembers your voice choice (`LARMOR_BACKEND`) |
| `~/.cache/huggingface/hub/` | The voice models (Parakeet, Chatterbox), local voice only |
| `~/.local/bin/uv` | [uv](https://github.com/astral-sh/uv), only if you don't have it already |
| Claude Code | `claude mcp add larmor -s user`, the skill in `~/.claude/skills/larmor`, hooks in `~/.claude/settings.json` |
| Claude Desktop | `mcpServers.larmor` in `~/Library/Application Support/Claude/claude_desktop_config.json` (backed up once to `claude_desktop_config.json.bak-larmor`) |
| Codex | A `[mcp_servers.larmor]` block in `~/.codex/config.toml` (backed up once to `config.toml.bak-larmor`), hooks in `~/.codex/hooks.json`, the skill in `~/.agents/skills/larmor` |
| Gemini CLI | `mcpServers.larmor` in `~/.gemini/settings.json`, the skill in `~/.agents/skills/larmor` |
| Antigravity | `mcpServers.larmor` in `~/.gemini/config/mcp_config.json`, the skill in `~/.gemini/skills/larmor` |

Only the agents you pick are touched, and only Larmor's own entries in their config files.
A config file that isn't valid JSON is left alone.

The hooks are small Python scripts in [`hooks/`](hooks). One keeps a voice turn from ending
without listening, one records which session turned voice mode on, and one hands the agent
anything you said while it was working. They do nothing in sessions where voice mode is off.

## What leaves your Mac

- **Your email**, once, at install, only if you leave one. It's how you hear about updates while
  Larmor is in beta.
- **Feedback**, only if you type it into "Send feedback…" in the menu bar.

Both go to one small endpoint whose entire code is [`backend/signup.py`](backend/signup.py).
Larmor itself sends nothing else: no audio, no transcripts, no usage data. The only other
network traffic is downloading the installer, Python packages and the models.

**With the Gemini voice**, while voice mode is on, your microphone audio streams to Google's
Gemini Live API under your own key, and the lines your agent speaks go there as text so Gemini
can say them. Nothing about your code goes there unless you or the agent say it aloud. Google's
terms for your key apply; the free tier may use the data for training, the paid tier doesn't.

## How it works

- **Ears (local):** Apple's VoiceProcessingIO echo canceller, Silero VAD, Smart Turn v3 for
  end-of-turn, and Parakeet (MLX) for speech-to-text.
- **Voice (local):** Chatterbox Turbo (MLX), played through the same echo canceller, so the mic
  hears you and not the agent.
- **Gemini:** one Gemini Live session replaces all of that. The echo-cancelled mic streams to it,
  it decides when you're done and stops when you talk over it, and it has one tool,
  `send_to_agent`, which hands your request to your coding agent exactly like a spoken turn.
  The agent's `speak()` lines go to Gemini as notes it says in its own voice (or, with
  `LARMOR_AGENT_VOICE=local`, through Chatterbox word for word). You can keep talking to Gemini
  while the agent works; what it hands off mid-task reaches the agent the usual way. The
  session reconnects itself across Google's connection resets. Settings, all environment
  variables: `LARMOR_GEMINI_MODEL` (`gemini-3.8-live`), `LARMOR_GEMINI_VOICE` (`Kore`),
  `LARMOR_GEMINI_LANGUAGE` (`English`: the language you speak; Gemini is told to expect an
  accent and never to switch, and its transcriber and voice are pinned to that language's tag,
  `LARMOR_GEMINI_LANGUAGE_CODE` to set it by hand), `LARMOR_GEMINI_SILENCE_MS` (1000, how long a pause ends your
  turn; raise it if it cuts you off, 1500 suits a speaker who pauses to think), `LARMOR_GEMINI_END_SENSITIVITY` (`high` or `low`; `low` lets a
  breath or a thinking pause pass), `LARMOR_GEMINI_COMPRESS_TRIGGER` / `LARMOR_GEMINI_COMPRESS_TARGET`
  (8000 / 4000: Gemini re-bills its whole context on every reply, so its memory is trimmed to the
  last few minutes of talk; the agent keeps the real context), `LARMOR_GEMINI_GATE=0` to send the
  mic to Gemini unfiltered (by default Silero decides locally what is speech and only that is
  sent, so the echo residue of Gemini's own voice can't make it cut itself off, and silence isn't
  billed), `LARMOR_BACKEND=local` to switch back. Put them in Larmor's `.env` file.
- **Interruptions:** your voice pauses playback at once. Keep talking, or say something real like
  "stop", and it stops. A backchannel like "mm-hmm" resumes the sentence.
- **Talk while it works:** what you say mid-task reaches the agent with its next spoken progress
  update, in any agent. In Claude Code and Codex it also arrives after the next tool call, usually
  within seconds.
- **One session at a time:** voice mode belongs to the session that turned it on. Your other
  terminals behave normally.
- **Lets go of the mic:** voice mode switches itself off after 10 minutes without the agent
  using it, or 20 minutes of nobody speaking, so a forgotten chat can't keep your mic open.
  Interrupting the agent while it listens (Escape, or typing a message) cancels that listen
  without losing what you say next; if the agent doesn't come back to listening within 90
  seconds, the voice tells you and voice mode switches off.

The audio front end is a small Swift program, [`native/larmor_audio.swift`](native/larmor_audio.swift).
The repo ships it prebuilt; to build it yourself:

```bash
cd native && cp larmor_audio.swift main.swift && swiftc -O main.swift mic_arbiter.swift -o larmor_audio && rm main.swift
```

## Uninstall

```bash
~/.larmor/app/install.sh --uninstall   # stops the app and removes the login item
claude mcp remove larmor -s user       # Claude Code
```

Then, for the agents you set up, delete the `larmor` skill folders and Larmor's entries in the
config files listed above. To free the disk space, delete `~/.larmor` and, if you used the local
voice, the Parakeet and Chatterbox folders in `~/.cache/huggingface/hub`.

Can't see the menu-bar icon? A full menu bar hides new icons. Hold ⌘ and drag some out.

## Licence

[MIT](LICENSE). The bundled models and the ones downloaded at first launch keep their own
licences, and the Gemini voice runs under Google's API terms; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
