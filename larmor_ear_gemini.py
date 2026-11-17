"""Larmor's Gemini ear: Gemini Live is the voice, your coding agent is still the brain.

    mic ─▶ larmor_audio (Apple VoiceProcessingIO: echo-cancelled) ─▶ Gemini Live
           Gemini listens, decides when you're done, talks, and stops when you talk over it.
    Gemini calls send_to_agent(message) ─▶ ~/.larmor/turns.jsonl ─▶ the agent's listen()
    speak(text) ─▶ POST /say ─▶ a note to Gemini, which says it in its own voice ─▶ speaker

Same contract as the local ear (larmor_ear.py): the MCP server starts it on start_voice(),
it appends finished turns to turns.jsonl, serves /say, /stop and /state on a port it writes
to ~/.larmor/ear.json once it's connected, and exits when its parent goes away.

Why the echo canceller still matters: Gemini's turn detection runs on the server, on whatever
we send. Gemini's own voice goes out through larmor_audio's reference input, so the mic feed
never contains it, any speech Gemini hears is the user, and barge-in is its call.

What the agent sees: only what Gemini hands off, as "turn" lines. The user's raw words are
logged as "heard" lines (listen() ignores them) so the silence timer and the ear log know
someone is talking.

How the agent's lines travel back: the send_to_agent call Gemini made stays open (it is
NON_BLOCKING, so Gemini keeps chatting), and every speak() line goes back as a result of that
call, `will_continue` so the call stays open for the next one, scheduled WHEN_IDLE so Gemini
says it at the next pause and never over the user. Before any hand-off (the greeting) there is
no call to answer, so the line goes in as a tagged note instead. "Stop", "cancel" and "wait"
are a tool of their own, stop_agent, so a spoken stop can never be mistaken for a request.
LARMOR_AGENT_VOICE=local says the lines word for word through the local engine instead, and
tells Gemini what was said.

    GEMINI_API_KEY=... python larmor_ear_gemini.py          # LARMOR_GEMINI_MODEL, _VOICE, _SILENCE_MS
"""
import asyncio
import json
import os
import re
import signal
import socket
import threading
import time

import numpy as np
import soxr
from aiohttp import web

import larmor_env  # noqa: F401  (the .env next to this file -> os.environ, before the settings below are read)
from larmor_ear import FRAME, INFO, LEAD_S, VAD_MODEL, Audio, Vad, emit, is_local, log, parent_alive, render_tts

MODEL = os.getenv("LARMOR_GEMINI_MODEL", "gemini-3.8-live")
VOICE = os.getenv("LARMOR_GEMINI_VOICE", "Kore")
AGENT_VOICE = os.getenv("LARMOR_AGENT_VOICE", "gemini")   # gemini: it relays the agent's lines; local: Chatterbox says them verbatim
SILENCE_MS = int(os.getenv("LARMOR_GEMINI_SILENCE_MS", "1000"))  # Google suggests 500-800, but that cuts a thinking pause
# Low end-of-speech sensitivity: a breath or a thinking pause mid-sentence is less likely to end the turn.
END_SENSITIVITY = os.getenv("LARMOR_GEMINI_END_SENSITIVITY", "high").lower()     # high | low
START_SENSITIVITY = os.getenv("LARMOR_GEMINI_START_SENSITIVITY", "high").lower() # high | low (low ignores short sounds)
AFFECTIVE = os.getenv("LARMOR_GEMINI_AFFECTIVE", "") == "1"
# Gemini re-bills the whole session context on every reply, so a long session costs by turns x
# history, not by minutes. The sliding window keeps Gemini's memory to the last few minutes of
# talk (the agent holds the real context). Raise these if Gemini forgets too much.
COMPRESS_TRIGGER = int(os.getenv("LARMOR_GEMINI_COMPRESS_TRIGGER", "8000"))   # compress once the context passes this
COMPRESS_TARGET = int(os.getenv("LARMOR_GEMINI_COMPRESS_TARGET", "4000"))     # ... down to about this many tokens
LANGUAGE = os.getenv("LARMOR_GEMINI_LANGUAGE", "English")   # what the user speaks; accents are expected
# The same language as a BCP-47 tag, for the transcriber and the voice. Left to detect the
# language itself, Gemini heard an accented English afternoon as German and Spanish, answered
# "please repeat that in English" against its instructions, and handed nothing to the agent.
_CODES = {"english": "en-US", "french": "fr-FR", "german": "de-DE", "spanish": "es-ES", "italian": "it-IT",
          "portuguese": "pt-BR", "dutch": "nl-NL", "japanese": "ja-JP", "korean": "ko-KR", "chinese": "cmn-CN"}
LANGUAGE_CODE = os.getenv("LARMOR_GEMINI_LANGUAGE_CODE") or _CODES.get(LANGUAGE.strip().lower())
SR_IN, SR_OUT = 16000, 24000                 # what the Live API takes and gives
PARENT = int(os.getenv("LARMOR_PARENT_PID", "0"))
USER_QUIET_S = 1.2                           # no transcript piece or VAD signal for this long: they've stopped
RECONNECT_MAX_S = 10.0
MIC_QUEUE = 40                               # about a second of 32 ms frames: bridges a reconnect, then drops the oldest
# The mic gate. Gemini's own detector hears everything we send, including the echo residue of
# its voice that Apple's canceller leaves behind, and cuts itself off on it. So Silero decides
# locally what is speech: only that reaches Gemini (plus a pre-roll and a hangover of silence so
# its turn detection still works), and silence costs nothing. Google calls this hybrid VAD.
GATE = os.getenv("LARMOR_GEMINI_GATE", "1") != "0"
GATE_OPEN_P, GATE_CLOSE_P = 0.6, 0.35       # Silero hysteresis, as in the local ear
GATE_OPEN_PLAYING_P = 0.9                    # while our speaker plays, echo residue must not pass
# Consecutive frames (32 ms each) above the bar. A cough is a 150-250 ms burst that Silero scores
# as speech; during playback a word has to last longer than that to interrupt.
GATE_OPEN_FRAMES, GATE_OPEN_PLAYING_FRAMES = 2, 9
GATE_PREROLL_S = 0.4                         # sent when the gate opens, so the first syllable isn't lost
GATE_HANGOVER_S = SILENCE_MS / 1000 + 0.3    # silence streamed after speech, so Gemini sees the pause
KEEPALIVE_S = float(os.getenv("LARMOR_GEMINI_KEEPALIVE_S", "8"))   # a silent frame now and then while closed; 0: none

# Tagged so Gemini can tell the agent's words from the user's. It is asked to relay, not to
# comment: the agent's line is already the answer.
AGENT_NOTE = ("[Report from the coding agent, not from the user. Say it to the user in one breath, "
              "keeping its meaning and its facts; add nothing and don't comment on it.] ")
AGENT_SAID = "[The coding agent just said this aloud to the user. Don't repeat it; it's context.] "

INSTRUCTIONS = f"""You are the voice of a coding agent working in the user's project. You know nothing about
their code, files, tests or commands, and you never guess about them. The agent is not limited to
the project: it searches the web, answers general questions, checks services, and it owns this
voice setup (your voice, your settings, the rules).

Any request for information or action, on the project or on anything else, including questions
about your own voice, settings or configuration: say one short sentence that you're passing it
on, and call send_to_agent with the user's request in their own words. One call per request;
never answer such a request yourself, never confirm or deny anything about your voice or
settings, and never claim work is done unless the agent reported it.

Give every hand-off a short task name. The agent runs named tasks side by side, and each one
answers through the results of its own call, in several parts: a receipt, progress notes, then its
answer. Each part is the agent's own words: say it to the user naturally, once, without adding
claims or comments. Reports from different tasks may interleave; say which task a report is about
when more than one is running. If the user asks what the agent is doing, answer from the latest
reports. Anything else about the conversation so far (what they asked, the last task, what the
agent said earlier) goes to the agent through send_to_agent: it has the history, you don't. Reports tagged as coming from the coding agent are the same thing and get the same treatment.

"End voice", "exit voice", "voice off", "quit voice", "that's all" and anything close to them
(a transcript like "voice out" or "eyes out" is one of these): call send_to_agent with exactly
"end voice", say nothing, and never ask what they meant. The agent closes the session.

"Stop", "cancel", "wait", "hold on" and the like: call stop_agent at once, nothing else, then say
one word; name the task if the user did, otherwise everything stops. "No, the other one" or a
corrected request is a new send_to_agent. If the user takes a
stop back within a few seconds ("no, it's okay", "go on", "I didn't mean that"), call send_to_agent
with "never mind, continue" and say one word.

While the agent is working, don't speak unprompted: no filler, no "still waiting" on your own. The
user can still talk to you and you answer them as usual. If they ask what's taking so long, answer
in one short sentence, once; don't repeat an apology or a promise to notify them. The agent's
report will come through you when it's ready.

If you didn't understand the user, ask one short clarifying question and nothing else. Never repeat
what you just said, never summarize the situation, never relist options unless asked. Exception:
"repeat", "say that again", "what did you say" means say your last utterance again word for word,
including a report from the agent you were relaying.

Noise is not a question. A transcript that is a lone word, a number out of context, a fragment,
or a few words in another language with nothing around them means nobody spoke: a cough, a
keyboard, an echo. Say nothing at all, and if it cut you off while relaying the agent's words,
continue those words from where you were cut.

Small talk, and clarifying what the user meant, are yours. Finish a thought; don't stop
mid-explanation to check if they follow. Keep it to two sentences unless you're relaying a report.
Spoken words only: no lists, no markdown, no code, no file paths. Don't greet first; the agent
opens the conversation.

The user speaks {LANGUAGE}, with an accent that is not native. Always communicate in {LANGUAGE},
no exceptions: a word that sounds like another language is still {LANGUAGE} said with an accent,
and technical words (file names, commands, product names) are likely. Never switch language, never
answer in another language, unless they explicitly ask you to. If a whole sentence comes out in
another language, you misheard {LANGUAGE}: never tell the user to speak {LANGUAGE} or that you only
understand it; ask them to say it again, once, in {LANGUAGE}. A fragment is noise (see above)."""


def _task_name(raw) -> str:
    """Task names as Gemini and the agent write them, normalised: lower case, single spaces, no brackets."""
    return re.sub(r"\s+", " ", str(raw or "").strip().strip("[]").lower())[:40]


class Speaker:
    """Feeds larmor_audio's reference input, paced to real time so a flush loses at most LEAD_S.
    Gemini sends audio faster than real time; the ring in larmor_audio would hold 10 s of it,
    and a barge-in has to silence it in one render block."""

    def __init__(self, audio: Audio):
        self.audio = audio
        self.q: asyncio.Queue = asyncio.Queue()
        self.gen = 0                          # bumped on flush: anything older is dropped
        self.play_until = 0.0                 # wall time our queued audio finishes playing
        self.rs = None
        self.agent_line = False               # what's playing is the agent's words (for "you were cut off")

    @property
    def speaking(self) -> bool:
        return time.time() < self.play_until or not self.q.empty()

    def feed(self, pcm24: bytes) -> None:
        if self.rs is None:
            self.rs = soxr.ResampleStream(SR_OUT, self.audio.ref_rate, 1, dtype="float32")
        x = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32) / 32768.0
        y = self.rs.resample_chunk(x)
        if len(y):
            self.q.put_nowait((self.gen, (np.clip(y, -1, 1) * 32767).astype(np.int16)))

    def feed_ready(self, pcm: np.ndarray) -> None:
        """int16 already at the speaker's rate (the local engine's voice)."""
        self.q.put_nowait((self.gen, pcm))

    def flush(self) -> bool:
        was = self.speaking
        self.gen += 1
        while not self.q.empty():
            self.q.get_nowait()
        self.rs = None
        self.audio.flush()
        self.play_until = time.time()
        self.agent_line = False
        return was

    async def run(self) -> None:
        rate = self.audio.ref_rate
        step = int(rate * 0.02)                                   # 20 ms writes
        while True:
            gen, pcm = await self.q.get()
            if gen != self.gen:
                continue
            if self.play_until < time.time():
                self.play_until = time.time()
            i = 0
            while i < len(pcm) and gen == self.gen:
                ahead = self.play_until - time.time()
                if ahead > LEAD_S:
                    await asyncio.sleep(ahead - LEAD_S)
                    continue
                chunk = pcm[i:i + step]
                self.audio.play(chunk.tobytes())
                self.play_until += len(chunk) / rate
                i += step


class _Reconnect(Exception):
    pass


class Ear:
    def __init__(self, audio: Audio):
        self.audio = audio
        self.speaker = Speaker(audio)
        self.mic: asyncio.Queue = asyncio.Queue(maxsize=MIC_QUEUE)
        self.say: asyncio.Queue = asyncio.Queue()
        self.stop = asyncio.Event()
        self.reconnect = asyncio.Event()
        self.connected = asyncio.Event()
        self.handle = None                    # session resumption handle, carried across connections
        self.user_until = 0.0                 # the user counts as talking until this wall time
        self.user_text = ""                   # input transcript of the utterance in progress
        self.said_text = ""                   # output transcript of the turn in progress
        self.model_turn = False               # Gemini is generating
        self.relay_next = False               # the next model turn is the agent's words
        self.call = None                      # (id, name) of the latest open send_to_agent call
        self.calls = {}                       # task name -> (id, name): each task's lines answer its own call
        self.interrupted_agent = False        # a barge-in cut the agent's words: tell it so
        self.heard_at = 0.0
        self.usage = {"in": 0, "out": 0, "context": 0, "turns": 0}   # in/out: billed totals; context: latest
        self.gate = {"on": GATE, "open": False, "opens": 0, "blocked_while_playing": 0}

    # ── config ────────────────────────────────────────────────────────────────

    def config(self):
        from google.genai import types
        tool = types.Tool(function_declarations=[types.FunctionDeclaration(
            name="send_to_agent",
            description=("Hand a request, question or instruction to the coding agent that works in the "
                         "user's project. It runs in the background and reports back through you. Use it "
                         "for anything about the code, files, tests, commands, or the task. Not for 'stop', "
                         "'cancel' or 'wait': those are stop_agent."),
            parameters=types.Schema(type="OBJECT", properties={
                "message": types.Schema(type="STRING", description="The user's request, in their own words."),
                "task": types.Schema(type="STRING", description=(
                    "A short name for this piece of work, one or two words ('tests', 'weather tokyo'). The agent "
                    "runs named tasks side by side and reports on each by name. Reuse a name when the user "
                    "refers to that task again; pick a new one for new work."))},
                required=["message", "task"]),
            behavior=types.Behavior.NON_BLOCKING), types.FunctionDeclaration(
            name="stop_agent",
            description=("The user wants the coding agent to stop work right now: 'stop', 'cancel', 'wait', "
                         "'hold on'. Call this instead of send_to_agent for those. Name the task if the user "
                         "did ('stop the tests'); without a name, everything stops."),
            parameters=types.Schema(type="OBJECT", properties={
                "task": types.Schema(type="STRING", description="The task to stop, by the name it was given; omit to stop all.")}),
            behavior=types.Behavior.NON_BLOCKING)])
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)), language_code=LANGUAGE_CODE),
            system_instruction=types.Content(parts=[types.Part(text=INSTRUCTIONS)]),
            tools=[tool],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=SILENCE_MS, prefix_padding_ms=300,
                    end_of_speech_sensitivity=(types.EndSensitivity.END_SENSITIVITY_LOW if END_SENSITIVITY == "low"
                                               else types.EndSensitivity.END_SENSITIVITY_HIGH),
                    start_of_speech_sensitivity=(types.StartSensitivity.START_SENSITIVITY_LOW if START_SENSITIVITY == "low"
                                                 else types.StartSensitivity.START_SENSITIVITY_HIGH))),
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=[LANGUAGE_CODE] if LANGUAGE_CODE else None),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # together these lift the 15-minute audio cap and survive the ~10-minute connection reset
            session_resumption=types.SessionResumptionConfig(handle=self.handle),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=COMPRESS_TRIGGER,
                sliding_window=types.SlidingWindow(target_tokens=COMPRESS_TARGET)),
            enable_affective_dialog=True if AFFECTIVE else None,
        )

    # ── mic ───────────────────────────────────────────────────────────────────

    def mic_thread(self, loop: asyncio.AbstractEventLoop) -> None:
        """The Swift binary's echo-cancelled mic, as int16 16 kHz frames for Gemini, through the
        Silero gate. Runs on its own thread; frames() blocks on the pipe. A None on the mic queue
        means "audio_stream_end": the gate closed."""
        try:
            self._gate_loop(loop)
        finally:
            loop.call_soon_threadsafe(self.stop.set)        # the mic is gone: nothing left to do

    def _gate_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        import collections
        vad = Vad(VAD_MODEL) if GATE else None
        put = lambda b: loop.call_soon_threadsafe(self._put_mic, b)   # noqa: E731
        ring: collections.deque = collections.deque(maxlen=max(1, int(GATE_PREROLL_S * SR_IN / FRAME)))
        buf = np.zeros(0, dtype=np.float32)
        fs = FRAME / SR_IN
        above = 0                     # consecutive frames over the opening bar
        quiet_s = 0.0                 # silence streamed since speech ended (hangover)
        closed_s = 0.0                # silence since the gate closed (keepalive)
        opened_at = 0.0
        for chunk in self.audio.frames():
            buf = np.concatenate([buf, chunk])
            while len(buf) >= FRAME:
                fr, buf = buf[:FRAME], buf[FRAME:]
                b = (np.clip(fr, -1, 1) * 32767).astype(np.int16).tobytes()
                if vad is None:
                    put(b)
                    continue
                p = vad(fr)
                playing = self.speaker.speaking
                if self.gate["open"]:
                    put(b)
                    if p >= GATE_CLOSE_P:
                        quiet_s = 0.0
                    else:
                        quiet_s += fs
                        if quiet_s >= GATE_HANGOVER_S:
                            self.gate["open"] = False
                            closed_s = 0.0
                            put(None)                                    # audio_stream_end
                            log(f"gate: closed after {time.time() - opened_at:.1f}s")
                    continue
                ring.append(b)
                bar = GATE_OPEN_PLAYING_P if playing else GATE_OPEN_P
                need = GATE_OPEN_PLAYING_FRAMES if playing else GATE_OPEN_FRAMES
                if p >= bar:
                    above += 1
                elif playing and p >= GATE_OPEN_P:
                    above = 0
                    self.gate["blocked_while_playing"] += 1             # would have passed with nobody talking
                else:
                    above = 0
                if above >= need:
                    self.gate["open"] = True
                    self.gate["opens"] += 1
                    above, quiet_s, opened_at = 0, 0.0, time.time()
                    for old in ring:                                     # the pre-roll, this frame included
                        put(old)
                    ring.clear()
                    log(f"gate: open (p={p:.2f}{', speaker playing' if playing else ''})")
                    continue
                closed_s += fs
                if KEEPALIVE_S and closed_s >= KEEPALIVE_S:               # Google closes idle sockets
                    closed_s = 0.0
                    put(bytes(FRAME * 2))

    def _put_mic(self, b) -> None:
        if self.mic.full():
            self.mic.get_nowait()                            # keep the newest second, not the oldest
        self.mic.put_nowait(b)

    @property
    def user_speaking(self) -> bool:
        return time.time() < self.user_until

    # ── session ───────────────────────────────────────────────────────────────

    async def run(self, connect) -> None:
        """Keep one Live session up for as long as voice mode lasts. Gemini resets the
        connection every ~10 minutes (GoAway first); we reconnect with the resumption handle,
        and the mic queue bridges the gap."""
        backoff = 1.0
        while not self.stop.is_set():
            self.reconnect.clear()
            self.call = None                  # call ids from a previous connection can't be answered
            self.calls = {}
            failed = False
            try:
                async with connect(self.config()) as session:
                    self.connected.set()
                    backoff = 1.0
                    log(f"gemini session up ({MODEL}, voice {VOICE}{', resumed' if self.handle else ''})")
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send(session))
                        tg.create_task(self._receive(session))
                        tg.create_task(self._say(session))
                        tg.create_task(self._until_reconnect())
            except* _Reconnect:
                pass
            except* Exception as eg:
                for e in eg.exceptions:
                    log(f"gemini session failed: {type(e).__name__}: {e}")
                failed = True
            finally:
                self.model_turn = False
            if failed and not self.stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)

    async def _until_reconnect(self) -> None:
        await asyncio.wait([asyncio.ensure_future(self.reconnect.wait()),
                            asyncio.ensure_future(self.stop.wait())], return_when=asyncio.FIRST_COMPLETED)
        raise _Reconnect()

    async def _send(self, session) -> None:
        from google.genai import types
        while True:
            b = await self.mic.get()
            if b is None:                                    # the gate closed: flush, Gemini finalises at once
                await session.send_realtime_input(audio_stream_end=True)
                continue
            await session.send_realtime_input(audio=types.Blob(data=b, mime_type=f"audio/pcm;rate={SR_IN}"))

    async def _receive(self, session) -> None:
        while True:                                          # receive() ends at each turn_complete
            async for msg in session.receive():
                await self._handle(session, msg)

    async def _handle(self, session, msg) -> None:
        if msg.tool_call and msg.tool_call.function_calls:
            for fc in msg.tool_call.function_calls:
                await self._tool(session, fc)
        if msg.go_away:
            log(f"gemini says go away (time left {msg.go_away.time_left}); reconnecting")
            self.reconnect.set()
        u = msg.session_resumption_update
        if u and u.resumable and u.new_handle:
            self.handle = u.new_handle
        if msg.usage_metadata:
            # one report per reply; prompt_token_count is the whole context, billed again each time
            self.usage["context"] = msg.usage_metadata.prompt_token_count or 0
            self.usage["in"] += self.usage["context"]
            self.usage["out"] += msg.usage_metadata.response_token_count or 0
            self.usage["turns"] += 1
        va = msg.voice_activity_detection_signal
        if va and va.vad_signal_type:
            self._voice(str(va.vad_signal_type).endswith("SOS"))
        va = msg.voice_activity
        if va and va.voice_activity_type:
            self._voice(str(va.voice_activity_type).endswith("START"))
        sc = msg.server_content
        if not sc:
            return
        if sc.interrupted:
            self._interrupted()
        t = sc.input_transcription
        if t and t.text:
            self.user_text += t.text
            self.user_until = time.time() + USER_QUIET_S
            if t.finished:
                self._flush_heard()
        t = sc.output_transcription
        if t and t.text:
            self.said_text += t.text
        if sc.model_turn:
            if not self.model_turn:                           # a turn starts: the user's turn is over
                self.model_turn = True
                self.speaker.agent_line = self.relay_next
                self.relay_next = False
                self.user_until = 0.0
                self._flush_heard()
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    self.speaker.feed(part.inline_data.data)
        if sc.turn_complete:
            self.model_turn = False
            if self.said_text.strip():
                log(f"gemini: {self.said_text.strip()[:120]!r}")
            self.said_text = ""

    def _voice(self, start: bool) -> None:
        self.user_until = (time.time() + 3600) if start else time.time()

    def _interrupted(self) -> None:
        """The user talked over Gemini: it has already stopped generating and kept only what it
        sent. Drop what we haven't played. If those were the agent's words, the next hand-off
        tells the agent it was cut off, as the local ear does."""
        cut_agent = self.speaker.agent_line
        was = self.speaker.flush()
        if cut_agent:
            self.interrupted_agent = True
        self.model_turn = False
        log(f"barge-in: stopped {'the agent' if cut_agent else 'gemini'}{'' if was else ' (nothing playing)'}")
        emit({"type": "bargein", "channel": "gemini", "wall_clock": time.time()})

    def _flush_heard(self) -> None:
        text = self.user_text.strip()
        self.user_text = ""
        if not text:
            return
        self.heard_at = time.time()
        emit({"type": "heard", "channel": "gemini", "text": text, "wall_clock": self.heard_at})
        log(f"heard: {text[:120]!r}")

    def _turn(self, text: str, **extra) -> None:
        emit({"type": "turn", "channel": "gemini", "text": text, "is_final": True, "is_turn_end": True,
              "wall_clock": time.time(), "aec": True, "interrupted_agent": self.interrupted_agent,
              "dur_s": 0, **extra})
        log(f"turn{' (barge-in)' if self.interrupted_agent else ''}: {text}")
        self.interrupted_agent = False

    async def _tool(self, session, fc) -> None:
        from google.genai import types
        if fc.name == "send_to_agent":
            text = str((fc.args or {}).get("message") or "").strip()
            task = _task_name((fc.args or {}).get("task"))
            if text:
                self._turn(f"[{task}] {text}" if task else text, task=task)
                self.call = (fc.id, fc.name)      # left open: the agent's lines come back as its results
                if task:
                    self.calls[task] = self.call
                return
            out = {"status": "ignored: empty message"}
        elif fc.name == "stop_agent":
            task = _task_name((fc.args or {}).get("task"))
            if task and task not in self.calls:    # Gemini named something we never started
                near = [t for t in self.calls if task in t or t in task]
                task = near[0] if len(near) == 1 else task
            self._turn(f"[{task}] stop" if task else "stop", stop=True, task=task)
            if not task:                           # a bare stop also silences what's queued or playing
                self.speaker.flush()
                while not self.say.empty():
                    self.say.get_nowait()
            out = {"status": f"stop handed to the agent for task {task!r}" if task else "stop handed to the agent for all tasks"}
        else:
            out = {"error": f"unknown tool {fc.name}"}
        # SILENT: Gemini already said its word when it made the call; nothing to add about it.
        await session.send_tool_response(function_responses=[types.FunctionResponse(
            id=fc.id, name=fc.name, response=out, scheduling=types.FunctionResponseScheduling.SILENT)])

    async def _quiet(self) -> None:
        while self.user_speaking or self.model_turn or self.speaker.speaking:
            await asyncio.sleep(0.05)

    async def _say(self, session) -> None:
        """The agent's lines. With an open send_to_agent call they go back as that call's results,
        WHEN_IDLE, and Gemini picks the moment. Without one (the greeting, or right after a
        reconnect) they go in as a tagged note once nobody is talking. Lines that queued up
        together travel as one."""
        from google.genai import types
        while True:
            lines = [await self.say.get()]
            if self.call is None or AGENT_VOICE == "local":
                await self._quiet()
            while not self.say.empty():
                lines.append(self.say.get_nowait())
            text = " ".join(lines)
            if AGENT_VOICE == "local":
                pcm = await asyncio.to_thread(render_tts, text, self.audio.ref_rate)
                if pcm is not None:
                    self.speaker.agent_line = True
                    self.speaker.feed_ready(pcm)
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=AGENT_SAID + text)]),
                    turn_complete=False)
            elif self.call is not None:
                cid, name = self.call
                m = re.match(r"\s*\[([^\]]{1,40})\]\s*(.*)", text, re.S)   # "[tests] all green" -> the tests call
                if m and _task_name(m.group(1)) in self.calls:
                    cid, name = self.calls[_task_name(m.group(1))]
                    text = m.group(2).strip() or text
                self.relay_next = True
                await session.send_tool_response(function_responses=[types.FunctionResponse(
                    id=cid, name=name, response={"report": text}, will_continue=True,
                    scheduling=types.FunctionResponseScheduling.WHEN_IDLE)])
            else:
                self.relay_next = True
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=AGENT_NOTE + text)]),
                    turn_complete=True)
            log(f"say ({'result' if self.call and AGENT_VOICE != 'local' else AGENT_VOICE}): {text[:120]!r}")

    # ── control ───────────────────────────────────────────────────────────────

    def web_app(self) -> web.Application:
        @web.middleware
        async def local_only(req: web.Request, handler):
            if not is_local(req.headers):
                return web.json_response({"error": "local programs only"}, status=403)
            return await handler(req)

        async def say(req: web.Request):
            body = await req.json() if req.can_read_body else {}
            text = str(body.get("text") or "").strip()
            if text:
                self.say.put_nowait(text)
            return web.json_response({"ok": True, "queued": self.say.qsize()})

        async def stop(_: web.Request):
            while not self.say.empty():
                self.say.get_nowait()
            return web.json_response({"ok": True, "was_speaking": self.speaker.flush()})

        async def state(_: web.Request):
            return web.json_response({"speaking": self.speaker.speaking or not self.say.empty(),
                                      "queued": self.say.qsize(), "last_heard": self.heard_at,
                                      "connected": self.connected.is_set(), "model": MODEL,
                                      "tokens": self.usage, "gate": self.gate})

        app = web.Application(middlewares=[local_only])
        app.router.add_post("/say", say)
        app.router.add_post("/stop", stop)
        app.router.add_get("/state", state)
        return app


def default_connect():
    from google import genai
    # The SDK's default API version has no Live endpoint (the WebSocket upgrade answers 404);
    # the Live API lives under v1beta.
    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
                          http_options={"api_version": os.getenv("LARMOR_GEMINI_API_VERSION", "v1beta")})
    return lambda config: client.aio.live.connect(model=MODEL, config=config)


async def main(connect=None) -> None:
    if connect is None:
        if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
            raise SystemExit(f"GEMINI_API_KEY is not set (fill it in {larmor_env.ENV_FILE})")
        connect = default_connect()
    loop = asyncio.get_running_loop()
    audio = Audio()
    ear = Ear(audio)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, ear.stop.set)
    mic = threading.Thread(target=ear.mic_thread, args=(loop,), daemon=True, name="mic")
    mic.start()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runner = web.AppRunner(ear.web_app())
    await runner.setup()
    await web.SockSite(runner, sock).start()

    async def announce():
        await ear.connected.wait()
        INFO.parent.mkdir(exist_ok=True)
        INFO.write_text(json.dumps({"port": port, "pid": os.getpid(), "backend": "gemini"}))
        log(f"up: mic {audio.mic_rate} Hz, speaker {audio.ref_rate} Hz, control :{port}")

    async def watch_parent():
        while not ear.stop.is_set():
            await asyncio.sleep(2)
            if not parent_alive():
                log("parent gone, exiting")
                ear.stop.set()

    tasks = [asyncio.create_task(ear.speaker.run()), asyncio.create_task(announce()),
             asyncio.create_task(watch_parent())]
    try:
        await ear.run(connect)
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        audio.close()
        mic.join(2)                                 # let the resampler go with the thread, not at exit
        try:
            INFO.unlink()
        except OSError:
            pass
        log(f"stopped ({ear.usage['turns']} replies; billed tokens in {ear.usage['in']}, out {ear.usage['out']}; "
            f"context at the end {ear.usage['context']})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
