#!/usr/bin/env python3
"""Larmor — an MCP server that gives a coding agent ears and a voice.

This process is plumbing: no LLM, it never answers for the agent.
  - speak(text)  hands a line to the ear, which plays it through Apple's echo
                 canceller; returns in ~10 ms while the audio plays.
  - listen()     blocks until the user finishes a spoken turn and returns the
                 transcript. Turns spoken while the agent was busy are buffered,
                 so listen() returns at once with them: speech is never lost.

The ear owns the microphone while voice mode is on and writes finished turns to
~/.larmor/turns.jsonl, which this server tails. Two ears, picked by LARMOR_BACKEND:
  - local  (larmor_ear.py): Parakeet, Chatterbox, Silero and Smart Turn on this Mac.
  - gemini (larmor_ear_gemini.py): Gemini Live is the voice. It listens, chats, hands
           work to the agent as turns, and says the agent's speak() lines in its voice.
           Default whenever GEMINI_API_KEY (or GOOGLE_API_KEY) is set, in the environment
           or in the .env file next to this script (see larmor_env.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import requests
from mcp.server import MCPServer

import larmor_env  # .env next to this script -> os.environ, before anything reads a setting

HOME = Path(os.path.expanduser("~/.larmor"))
HOME.mkdir(exist_ok=True)
TURNS = HOME / "turns.jsonl"
TURNS.touch()
SESSION = HOME / "session"          # voice mode on/off + who owns the mic; read by the hooks
METRICS = HOME / "metrics.jsonl"
EAR_INFO = HOME / "ear.json"
# Newest turn already handed to the agent, by listen() or by the heard hook (which
# delivers speech mid-work at tool boundaries). Shared so nothing arrives twice.
DELIVERED = HOME / "delivered_until"
EAR_LOG = HOME / "ear.log"
ENGINE = os.getenv("LARMOR_ENGINE_URL", "http://127.0.0.1:8160")
HAS_KEY = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
BACKEND = os.getenv("LARMOR_BACKEND") or ("gemini" if HAS_KEY else "local")
EAR_SCRIPT = str(Path(__file__).resolve().parent / ("larmor_ear_gemini.py" if BACKEND == "gemini" else "larmor_ear.py"))
# In gemini mode the local engine is only needed when the agent's lines go through Chatterbox.
NEEDS_ENGINE = BACKEND == "local" or os.getenv("LARMOR_AGENT_VOICE") == "local"

POLL_S = 0.02                       # 20 ms: the transport should never be the bottleneck
# A mic claim is a lease, not a lock: sessions get abandoned (escape, closed terminal)
# without end_voice(), and a lock would then block every other agent forever. The
# owner renews it on every speak() and every LEASE_RENEW_S while inside listen(), so
# a session that is merely waiting for the user never looks abandoned; a stale lease
# means no voice call at all for LEASE_S, and is free for the taking. The taker kills
# any ear the old owner left behind, and the old owner's watch notices the takeover
# within WATCH_S and stops its own ear too: two ears on one microphone means two
# assistants answering, and their hand-offs landing in whichever session listens.
LEASE_S = float(os.getenv("LARMOR_LEASE_S", "90"))
LEASE_RENEW_S = float(os.getenv("LARMOR_LEASE_RENEW_S", "15"))
WATCH_S = float(os.getenv("LARMOR_WATCH_S", "5"))
# Claude Code gives a local MCP server five minutes per tool call. The default wait
# has to come back before that, or the client errors out just before listen() returns.
LISTEN_S = float(os.getenv("LARMOR_LISTEN_S", "240"))
# Voice mode also has to let go of the mic on its own. While it's on, the ear keeps the
# mic open through Apple's echo canceller, which turns down every other app's audio
# whenever it hears speech. Two ways a session gets abandoned with voice still on:
#   - the agent stops calling the tools at all (a Claude Desktop chat that was left;
#     no Stop hook there to keep it listening): no speak()/listen() for IDLE_OFF_S;
#   - the agent keeps calling listen() but nobody has spoken for SILENCE_OFF_S.
IDLE_OFF_S = float(os.getenv("LARMOR_IDLE_OFF_S", "600"))
SILENCE_OFF_S = float(os.getenv("LARMOR_SILENCE_OFF_S", "1200"))
# A third way, and the fastest to go wrong: the user interrupts the agent (Escape, a
# typed message) while it sits in listen(). The client cancels the call and the turn
# ends, and no Stop hook runs on an interrupt, so nothing brings the agent back to
# listen(): the ear keeps taking hand-offs that nobody will read, "end voice" included.
# If no voice call follows the cancelled one within INTERRUPT_OFF_S, say so and switch off.
INTERRUPT_OFF_S = float(os.getenv("LARMOR_INTERRUPT_OFF_S", "90"))
AUTO_OFF_NOTE = ("voice mode switched itself off: {why}, so Larmor let go of the microphone. "
                 "Don't call listen() again. If the user wants to talk, call start_voice().")

# Sent to every MCP client at connect, so any agent gets the speaking rules even where
# the larmor skill isn't installed. The skill (skills/larmor/SKILL.md) is the long form.
INSTRUCTIONS = """\
Larmor lets the user talk with you by voice. None of this applies until they ask for voice
mode (or type /larmor or $larmor). Then:

- Call start_voice() once. Loop: speak(reply) -> listen() -> work -> speak(reply) -> listen().
  Never end your turn without calling listen(). Call end_voice() only when they say they're done,
  and say "Quitting." first so they hear that it landed.
  On "(silence)", just listen() again.
- Talk like a colleague: finish the thought in one speak() call, then listen(). Pause only when
  you ask them something or need a decision; don't stop after every sentence to check they
  follow. Lead with the answer. Never speak markdown, code, file paths or URLs; put detail on
  the terminal and say one line about it. Round numbers.
- What the user says while you work comes back in the result of your next speak() call (and,
  in agents with Larmor's hooks, after your next tool call). Treat it as their turn: acknowledge
  it and change course if it redirects you. It will not come through listen() again.
- Don't write on-screen summaries of what you just said aloud. Your reply is the speak() call.
"""
if BACKEND == "gemini":
    INSTRUCTIONS += """\
- A voice assistant (Gemini Live) sits between you and the user: it chats with them, hands you
  work, and says your speak() lines in its own voice, word for word. It cannot shorten them: cap
  a spoken answer at three or four sentences and offer the rest rather than sending it.
- listen() returns what the assistant handed you, in its words, sometimes in the third person.
  It already told the user "passing that on", so speak a receipt only when it says what you're
  doing ("checking the config"), never a bare "on it". Speak a short progress beat at each
  milestone of long work and poll listen(timeout_s=0.1) between beats.
- Hand-offs may carry a task name, as "[tests] run the suite". Run long named tasks in the
  background so several can run at once, start every spoken line about a task with its name in
  square brackets, and treat "[tests] stop" as stop that task only; a bare "stop" stops
  everything, and the whole reply is "Okay.".
- Being talked over is ordinary conversation here: don't apologise for a cut-off, just answer.
"""
else:
    INSTRUCTIONS += """\
- A speak() call can be as long as the thought needs. Before your first tool call on any real
  work, speak a short receipt ("checking the config"). During long work, speak a short progress
  beat at each milestone, and poll listen(timeout_s=0.1) between beats to catch them.
- If listen() says you were cut off, open with a couple of words owning it, then answer.
"""

mcp = MCPServer("larmor", instructions=INSTRUCTIONS)
OWNER = os.getpid()                 # each agent spawns its own server, so the pid identifies it
_state = {"cursor": None, "buffered": [], "was_away": False, "gen": 0}
_turns_lock = threading.Lock()      # one reader of turns.jsonl at a time: a stale listen() and a live one
_ear = {"proc": None}
_calls = {"in_flight": 0, "last": 0.0, "heard": 0.0, "off_why": None, "watch": None, "cancelled": 0.0}
_calls_lock = threading.Lock()


class _call:
    """Counts a voice tool call as activity for the idle watch, for its whole duration."""
    def __enter__(self):
        with _calls_lock:
            _calls["in_flight"] += 1
            _calls["last"] = time.time()
            _calls["cancelled"] = 0.0   # the agent is back after an interrupt
            _state["gen"] += 1          # a newer voice call supersedes any listen() still polling

    def __exit__(self, *exc):
        with _calls_lock:
            _calls["in_flight"] -= 1
            _calls["last"] = time.time()


def _metric(kind: str, **fields) -> None:
    fields.update({"kind": kind, "wall": time.time()})
    with open(METRICS, "a") as f:
        f.write(json.dumps(fields) + "\n")


def _read_session() -> dict:
    try:
        return json.loads(SESSION.read_text())
    except (OSError, ValueError):
        return {}


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _owns_mic() -> bool:
    """True if nobody holds the mic, we do, or the holder is gone or idle past the lease."""
    s = _read_session()
    owner = s.get("owner")
    if owner is None or owner == OWNER or not _pid_alive(owner):
        return True
    last = max(float(s.get("last_listen", 0) or 0), float(s.get("last_speak", 0) or 0))
    return (time.time() - last) > LEASE_S


def _lost_mic() -> bool:
    """True once another live agent owns the session: our ear must go."""
    owner = _read_session().get("owner")
    return owner is not None and owner != OWNER and _pid_alive(owner)


def _lost_mic_soon() -> bool:
    """Our ear died: give a taker in the middle of its claim a moment to finish writing."""
    time.sleep(WATCH_S)
    return _lost_mic()


def _touch(field: str) -> None:
    s = _read_session()
    if not s.get("on"):
        return
    s[field] = time.time()
    try:
        SESSION.write_text(json.dumps(s))
    except OSError:
        pass


# ── turns ─────────────────────────────────────────────────────────────────────

def _delivered() -> float:
    try:
        return float(DELIVERED.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _mark_delivered(t: float) -> None:
    if t > _delivered():
        tmp = HOME / "delivered_until.tmp"
        tmp.write_text(repr(t))
        tmp.replace(DELIVERED)


def _skip_turns() -> None:
    """Fast-forward past anything unread: those words were said to another agent."""
    if _state["cursor"] is None:
        _state["cursor"] = open(TURNS)
    _state["cursor"].seek(0, os.SEEK_END)


def _drain() -> list[dict]:
    if _state["cursor"] is None:
        _skip_turns()
    out = []
    done = _delivered()
    while line := _state["cursor"].readline():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = (ev.get("text") or "").strip()
        if ev.get("type") == "heard":                 # the user talking to the voice assistant
            _calls["heard"] = max(_calls["heard"], float(ev.get("wall_clock") or 0))
            continue
        if ev.get("type") != "turn" or not text or float(ev.get("wall_clock") or 0) <= done:
            continue
        if ev.get("ear") is not None and ev.get("ear") != _ear_pid():
            continue                                  # another ear's hand-off (a ghost, or a takeover)
        if ev.get("interrupted_agent") and BACKEND != "gemini":   # with Gemini a cut-off is ordinary conversation
            text = "(you were cut off mid-sentence) " + text
        out.append({"text": text, "t_user_end": ev.get("wall_clock", time.time()),
                    "t_seen": time.time()})
    return out


@mcp.tool()
async def listen(timeout_s: float = LISTEN_S) -> str:
    """Block until the user finishes a spoken turn; return the transcript.

    Returns instantly if the user spoke while you were working (turns are
    buffered — speech is never lost). Multiple buffered turns are joined in
    order. Returns "(silence)" on timeout. Call speak() first if you owe the
    user a reply; call listen() again right after to keep the conversation.
    """
    # async, not a worker thread: when the client cancels the call (the user interrupted
    # the agent) the cancellation lands here at the next await, and the poll stops before
    # it can take a turn that no one is waiting for. A sync tool would run on to the end.
    if _calls["off_why"] and not _ear_alive():
        return AUTO_OFF_NOTE.format(why=_calls["off_why"])
    with _call():
        try:
            return await _listen(timeout_s)
        except anyio.get_cancelled_exc_class():
            _calls["cancelled"] = time.time()        # nothing consumed; the watch takes it from here
            _metric("listen_cancelled")
            raise


async def _listen(timeout_s: float) -> str:
    t_call = time.time()
    if not _owns_mic():
        _state["buffered"].clear()
        _skip_turns()
        _state["was_away"] = True
        await anyio.to_thread.run_sync(_stop_ear)    # their ear has the mic now, not ours
        return ("(another agent holds voice mode — this session is not listening. "
                "Call start_voice() to take over, or stay quiet.)")
    if _state["was_away"]:                       # regained the mic: don't replay their talk
        _state["was_away"] = False
        _state["buffered"].clear()
        _skip_turns()
    _touch("last_listen")
    deadline = t_call + timeout_s
    renew_at = t_call + LEASE_RENEW_S
    gen = _state["gen"]
    turns: list[dict] = []
    while True:
        if time.time() >= renew_at:                  # still here, still ours
            _touch("last_listen")
            renew_at = time.time() + LEASE_RENEW_S
        with _turns_lock:
            _state["buffered"].extend(_drain())
            # the heard hook may have delivered some of these mid-work after we buffered them
            done = _delivered()
            turns = [t for t in _state["buffered"] if t["t_user_end"] > done]
            _state["buffered"] = []
            if _state["gen"] != gen:
                # The client dropped this call (the user typed, or pressed Escape) and the agent
                # has moved on to a newer speak() or listen(): this thread kept polling and would
                # have swallowed the next turn. Leave the turns to the live call.
                _state["buffered"] = turns
                return "(superseded by a newer call; nothing consumed)"
            if turns:
                _calls["heard"] = time.time()
                _mark_delivered(max(t["t_user_end"] for t in turns))
                break
        if time.time() >= deadline:
            break
        await anyio.sleep(POLL_S)
    if not turns:
        # the heard hook delivers mid-work speech too; delivered_until covers both paths
        if _ear_alive() and time.time() - max(_calls["heard"], _delivered()) > SILENCE_OFF_S:
            _auto_off(f"nobody has spoken for {int(SILENCE_OFF_S // 60)} minutes")
            return AUTO_OFF_NOTE.format(why=_calls["off_why"])
        return "(silence)"
    t_ret = time.time()
    for t in turns:
        _metric("listen_turn", total_ms=round((t_ret - t["t_user_end"]) * 1000, 1),
                buffered=t["t_seen"] < t_call)
    return "\n".join(t["text"] for t in turns)


# ── voice ─────────────────────────────────────────────────────────────────────

def _ear_alive() -> bool:
    p = _ear["proc"]
    return p is not None and p.poll() is None


def _ear_pid() -> int | None:
    p = _ear["proc"]
    return p.pid if p is not None else None


def _kill_ghost_ear() -> None:
    """An ear left behind by a server that died or lost its lease: it still holds the
    mic and a billed Gemini session, and would answer the user alongside ours."""
    try:
        pid = int(json.loads(EAR_INFO.read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if pid == _ear_pid() or not _pid_alive(pid):
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return
    for _ in range(30):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    _metric("ghost_ear_killed", pid=pid)


def _ear_port() -> int:
    """The ear writes its port once audio is up (and, for gemini, once the session is
    connected). A file left by an earlier ear is ignored: the pid has to be ours."""
    for _ in range(150):
        try:
            info = json.loads(EAR_INFO.read_text())
            if _ear["proc"] is None or info.get("pid") == _ear["proc"].pid:
                return int(info["port"])
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.1)
    raise RuntimeError("ear control port not available")


def _start_ear() -> None:
    if _ear_alive():
        return
    env = dict(os.environ, LARMOR_PARENT_PID=str(OWNER), LARMOR_ENGINE_URL=ENGINE)
    _ear["proc"] = subprocess.Popen([sys.executable, EAR_SCRIPT], env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=open(EAR_LOG, "a"))


def _stop_ear() -> None:
    p = _ear["proc"]
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    _ear["proc"] = None


def _auto_off(why: str) -> None:
    """end_voice() on the agent's behalf; only flips the session if it's still ours."""
    if _read_session().get("owner") == OWNER:
        SESSION.write_text(json.dumps({"on": False, "since": time.time()}))
    _stop_ear()
    _calls["off_why"] = why
    _metric("voice_auto_off", why=why)


def _tell_user(text: str, wait_s: float = 20.0) -> None:
    """Have the ear say one line, and wait until it has been said.

    Straight to /say, not through _speak(): that would also collect the pending
    hand-offs, and they would be marked delivered to an agent that never saw them.
    """
    try:
        port = _ear_port()
        requests.post(f"http://127.0.0.1:{port}/say", json={"text": text}, timeout=5)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(0.5)
            if not requests.get(f"http://127.0.0.1:{port}/state", timeout=2).json().get("speaking"):
                break
    except Exception:  # noqa: BLE001
        pass


def _watch() -> None:
    """While our ear runs: switch off when the agent went away, stop when another took over."""
    while True:
        time.sleep(min(WATCH_S, IDLE_OFF_S / 4))
        if _lost_mic() or (not _ear_alive() and _lost_mic_soon()):
            _stop_ear()                              # the taker may already have killed our ear
            _calls["off_why"] = "another agent took over voice mode"
            _metric("voice_lost", why=_calls["off_why"])
            return
        if not _ear_alive():
            return
        with _calls_lock:
            quiet = _calls["in_flight"] == 0
            idle = quiet and time.time() - _calls["last"] > IDLE_OFF_S
            interrupted = quiet and _calls["cancelled"] and time.time() - _calls["cancelled"] > INTERRUPT_OFF_S
        if interrupted and _ear_alive():
            _tell_user("Voice mode is switching off: the agent was interrupted and has not come back "
                       "to listen. Type in the chat to carry on.")
            _auto_off("the agent was interrupted and did not come back to listen")
            return
        if idle and _ear_alive():
            _auto_off(f"there was no speak() or listen() call for {int(IDLE_OFF_S // 60)} minutes")
            return


@mcp.tool()
def speak(text: str) -> str:
    """Say a line to the user. Returns immediately (~10ms) while audio plays.

    Keep it to 1-3 spoken sentences — detail belongs on the terminal, not in
    the voice. Never speak markdown, code, or file paths.
    """
    if not _ear_alive():
        if _calls["off_why"]:
            return AUTO_OFF_NOTE.format(why=_calls["off_why"])
        return "voice mode is off: call start_voice() first"
    with _call():
        return _speak(text)


def _speak(text: str) -> str:
    try:
        r = requests.post(f"http://127.0.0.1:{_ear_port()}/say", json={"text": text}, timeout=5)
    except Exception as e:  # noqa: BLE001
        return f"speak failed: {e}"
    _touch("last_speak")
    return f"queued ({r.json().get('queued', '?')} ahead)" + _heard_while_working()


def _heard_while_working() -> str:
    """Anything the user finished saying since the agent last heard them.

    This is how speech reaches the agent mid-work in every MCP client: agents narrate
    long work with speak(), so a turn arrives within one progress beat. Claude Code and
    Codex also get it sooner through the heard hook; both mark delivered_until, so a
    turn only ever arrives once.
    """
    if _state["was_away"] or not _owns_mic():
        return ""                                # listen() resyncs when we regain the mic
    with _turns_lock:
        _state["buffered"].extend(_drain())
        done = _delivered()
        turns = [t for t in _state["buffered"] if t["t_user_end"] > done]
        _state["buffered"] = []
        if not turns:
            return ""
        _calls["heard"] = time.time()
        _mark_delivered(max(t["t_user_end"] for t in turns))
    _metric("heard_on_speak", n=len(turns))
    quoted = "\n".join(f'  "{t["text"]}"' for t in turns)
    who = "the voice assistant handed you this" if BACKEND == "gemini" else "the user said (by voice)"
    return (f"\n\n[Larmor] While you were working, {who}:\n" + quoted + "\n"
            "This will NOT come through listen(). Act on it now: acknowledge it with speak(), "
            "and change course if it redirects your work.")


@mcp.prompt()
def larmor() -> str:
    """Start voice mode: talk with your coding agent through Larmor."""
    return ("Start Larmor voice mode now: call the larmor start_voice tool, greet me in one short "
            "spoken line with speak(), then listen(). Follow the Larmor speaking rules until I end it.")


@mcp.tool()
def start_voice() -> str:
    """Enter voice mode. Call this ONCE when the user asks to talk.

    While voice mode is active a Stop hook will refuse to let you end a turn
    without calling listen(), so the conversation cannot silently die.
    """
    now = time.time()
    if not _owns_mic():
        s = _read_session()
        idle = int(now - float(s.get("last_listen", now) or now))
        return (f"another agent is actively in voice mode (last heard {idle}s ago). "
                f"End voice mode there first, or if that session is gone, wait "
                f"{int(LEASE_S)}s and try again.")
    if BACKEND == "gemini" and not HAS_KEY:
        return (f"Larmor's Gemini backend needs a key: fill in the GEMINI_API_KEY= line of {larmor_env.ENV_FILE} "
                "with an editor and open a new session. Or LARMOR_BACKEND=local there for the on-device models.")
    if NEEDS_ENGINE:
        try:
            if requests.get(f"{ENGINE}/health", timeout=2).json().get("state") != "ready":
                return "the Larmor engine isn't ready yet (models may still be downloading; see the menu-bar icon)"
        except Exception:  # noqa: BLE001
            return "the Larmor engine isn't running: start the Larmor menu-bar app"
    # Claim first, so the old owner's watch sees a takeover rather than a dead ear; then
    # kill whatever ear it left behind, start ours, and record which ear is ours so the
    # heard hook, like _drain(), ignores hand-offs from any other. Both stamps are seeded:
    # a fresh session hasn't failed to listen yet.
    SESSION.write_text(json.dumps({"on": True, "since": now, "owner": OWNER, "backend": BACKEND,
                                   "last_listen": now, "last_speak": now}))
    _kill_ghost_ear()
    _start_ear()
    s = _read_session()
    s["ear_pid"] = _ear_pid()
    SESSION.write_text(json.dumps(s))
    _state["buffered"].clear()
    _skip_turns()
    _state["was_away"] = False
    with _calls_lock:
        _calls.update(last=now, heard=now, off_why=None)
    if _calls["watch"] is None or not _calls["watch"].is_alive():
        _calls["watch"] = threading.Thread(target=_watch, daemon=True)
        _calls["watch"].start()
    _metric("voice_start")
    return "voice mode ON — end every turn with listen(); call end_voice() to exit"


@mcp.tool()
def end_voice() -> str:
    """Leave voice mode. Call when the user says stop / that's all / exit voice."""
    if not _lost_mic():                              # never switch off a session that is now someone else's
        SESSION.write_text(json.dumps({"on": False, "since": time.time()}))
    _stop_ear()
    _calls["off_why"] = None
    _metric("voice_end")
    return "voice mode OFF"


@mcp.tool()
def voice_status() -> str:
    """Health check: engine state, whether voice mode is on, recent listen latency."""
    out = {"backend": BACKEND, "voice_mode_on": bool(_read_session().get("on")), "ear_running": _ear_alive()}
    if BACKEND == "gemini":
        out["gemini_key"] = "set" if HAS_KEY else f"MISSING (fill in GEMINI_API_KEY= in {larmor_env.ENV_FILE})"
    if _ear_alive():
        try:
            out["ear"] = requests.get(f"http://127.0.0.1:{_ear_port()}/state", timeout=2).json()
        except Exception as e:  # noqa: BLE001
            out["ear"] = f"no answer: {e}"
    try:
        h = requests.get(f"{ENGINE}/health", timeout=2).json()
        out["engine"] = {k: h.get(k) for k in ("state", "progress", "detail", "backend")}
    except Exception as e:  # noqa: BLE001
        out["engine"] = f"DOWN: {e}" if NEEDS_ENGINE else "not running (not needed with the gemini backend)"
    try:
        lines = METRICS.read_text().strip().splitlines()
        tot = [json.loads(l)["total_ms"] for l in lines if '"listen_turn"' in l][-20:]
        if tot:
            out["listen_latency_ms"] = {"n": len(tot), "p50": sorted(tot)[len(tot) // 2]}
    except (OSError, ValueError, KeyError):
        pass
    return json.dumps(out, indent=2)


def _shutdown(*_) -> None:
    """The agent is gone (stdin closed, server disabled, SIGTERM): don't leave the session
    marked on, or the next chat's Stop hook blocks on a voice mode nobody is in."""
    s = _read_session()
    if s.get("on") and s.get("owner") == OWNER:
        SESSION.write_text(json.dumps({"on": False, "since": time.time()}))
    _stop_ear()


if __name__ == "__main__":
    import atexit
    import signal
    atexit.register(_shutdown)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))
    try:
        mcp.run()
    finally:
        _shutdown()
