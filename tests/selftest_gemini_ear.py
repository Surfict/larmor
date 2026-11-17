#!/usr/bin/env python3
"""Drive larmor_ear_gemini.py end to end with no microphone and no network.

    python tests/selftest_gemini_ear.py

tests/fake_audio.py stands in for the Swift binary, and a scripted fake Live session stands
in for Gemini. Checks: mic audio reaches the session; a transcript becomes a "heard" line; a
send_to_agent call becomes a "turn" line and gets a SILENT tool response; Gemini's audio
reaches the speaker; POST /say becomes a tagged note; a barge-in during the relay flushes
the speaker and flags the next hand-off; GoAway reconnects with the resumption handle;
/state and /stop answer; stopping unlinks ear.json.
"""
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="larmor-selftest-")
os.environ.update(LARMOR_AUDIO_BIN=os.path.join(HERE, "fake_audio.py"),
                  LARMOR_TURNS=os.path.join(TMP, "turns.jsonl"),
                  LARMOR_EAR_INFO=os.path.join(TMP, "ear.json"),
                  LARMOR_FAKE_SPEAKER=os.path.join(TMP, "speaker.pcm"),
                  LARMOR_FAKE_LEAD_S="0.3", LARMOR_FAKE_REPEAT_S="3")
os.environ.pop("LARMOR_PARENT_PID", None)

import numpy as np  # noqa: E402
import aiohttp  # noqa: E402
from google.genai import types  # noqa: E402

import larmor_ear_gemini as ear_mod  # noqa: E402

FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def tone(seconds=1.0):
    t = np.arange(int(24000 * seconds)) / 24000
    return (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16).tobytes()


class FakeSession:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.audio_chunks = 0
        self.audio_bytes = 0
        self.stream_ends = 0
        self.tool_responses = []
        self.client_content = []

    async def send_realtime_input(self, *, audio=None, audio_stream_end=None, **_):
        if audio_stream_end:
            self.stream_ends += 1
            return
        self.audio_chunks += 1
        self.audio_bytes += len(audio.data)

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)

    async def send_client_content(self, *, turns=None, turn_complete=True):
        self.client_content.append((turns, turn_complete))

    async def receive(self):
        while True:
            msg = await self.inbox.get()
            yield msg
            if msg.server_content and msg.server_content.turn_complete:
                return

    def push(self, **kw):
        self.inbox.put_nowait(types.LiveServerMessage(**kw))


def content(**kw):
    return types.LiveServerContent(**kw)


def audio_part(data):
    return types.Content(parts=[types.Part(inline_data=types.Blob(data=data, mime_type="audio/pcm"))])


async def main():
    sessions, configs = [], []

    @contextlib.asynccontextmanager
    async def connect(config):
        configs.append(config)
        s = FakeSession()
        sessions.append(s)
        yield s

    ear_task = asyncio.create_task(ear_mod.main(connect))

    async def wait_for(pred, what, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if pred():
                return True
            if ear_task.done():
                check(False, f"the ear stopped while waiting for {what}: {ear_task.exception()!r}")
                raise SystemExit(1)
            await asyncio.sleep(0.05)
        check(False, f"timed out waiting for {what}")
        return False

    def turns():
        try:
            return [json.loads(l) for l in open(os.environ["LARMOR_TURNS"])]
        except FileNotFoundError:
            return []

    def speaker_bytes():
        try:
            return os.path.getsize(os.environ["LARMOR_FAKE_SPEAKER"])
        except OSError:
            return 0

    await wait_for(lambda: sessions and sessions[0].audio_chunks >= 20, "mic audio in the session")
    s = sessions[0]
    check(s.audio_bytes >= 20 * 640, f"mic audio streamed: {s.audio_chunks} chunks, {s.audio_bytes} bytes")
    await wait_for(lambda: s.stream_ends >= 1, "the gate closes after the sentence (audio_stream_end)")
    n0 = s.audio_chunks
    await asyncio.sleep(0.5)
    check(s.audio_chunks - n0 <= 1, f"silence is not streamed while the gate is closed ({s.audio_chunks - n0} frames in 0.5s)")
    cfg = configs[0]
    check(cfg.tools and cfg.tools[0].function_declarations[0].name == "send_to_agent", "send_to_agent tool declared")
    check(str(cfg.tools[0].function_declarations[0].behavior).endswith("NON_BLOCKING"), "tool is NON_BLOCKING")
    check(cfg.session_resumption is not None and cfg.context_window_compression is not None,
          "session resumption and context compression configured")
    check(cfg.input_audio_transcription is not None, "input transcription on")

    info_path = os.environ["LARMOR_EAR_INFO"]
    await wait_for(lambda: os.path.exists(info_path), "ear.json")
    info = json.load(open(info_path))
    check(info.get("pid") == os.getpid() and info.get("port"), f"ear.json has pid and port: {info}")
    base = f"http://127.0.0.1:{info['port']}"

    # 0. before any hand-off the agent's line goes in as a tagged note
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base}/say", json={"text": "Hi, I'm here."}) as resp:
            check(resp.status == 200, "POST /say before any hand-off accepted")
    await wait_for(lambda: s.client_content, "greeting note")
    note, complete = s.client_content[-1]
    check(note.parts[0].text.startswith(ear_mod.AGENT_NOTE) and complete, "greeting travels as a tagged note")

    # 1. the user talks: transcript -> "heard" line
    s.push(server_content=content(input_transcription=types.Transcription(text="please list the files", finished=False)))
    s.push(server_content=content(input_transcription=types.Transcription(text=" in this folder", finished=True)))
    await wait_for(lambda: any(t["type"] == "heard" for t in turns()), "heard line")
    heard = [t for t in turns() if t["type"] == "heard"]
    check(heard and heard[-1]["text"] == "please list the files in this folder", f"heard text: {heard[-1]['text'] if heard else None}")

    # 2. Gemini hands off -> "turn" line, SILENT tool response
    s.push(tool_call=types.LiveServerToolCall(function_calls=[
        types.FunctionCall(id="c1", name="send_to_agent", args={"message": "list the files in this folder"})]))
    await wait_for(lambda: any(t["type"] == "turn" for t in turns()), "turn line")
    turn = [t for t in turns() if t["type"] == "turn"][-1]
    check(turn["text"] == "list the files in this folder" and turn["interrupted_agent"] is False, f"turn line: {turn}")
    await asyncio.sleep(0.3)
    check(not s.tool_responses, "the hand-off call is left open (no immediate response)")

    # 3. Gemini talks -> speaker
    before = speaker_bytes()
    s.push(server_content=content(model_turn=audio_part(tone(0.5)), output_transcription=types.Transcription(text="On it.")))
    s.push(server_content=content(turn_complete=True))
    await wait_for(lambda: speaker_bytes() > before + 10000, "gemini audio on the speaker", 5)

    # 4. the agent speaks -> a WHEN_IDLE result of the open call, call kept open
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base}/say", json={"text": "Done, three files."}) as resp:
            check(resp.status == 200 and (await resp.json())["ok"], "POST /say accepted")
        await wait_for(lambda: s.tool_responses, "agent line as a tool result")
        r = s.tool_responses[-1]
        check(r.id == "c1" and str(r.scheduling).endswith("WHEN_IDLE") and r.will_continue is True
              and r.response.get("report") == "Done, three files.",
              f"result: id={r.id} scheduling={r.scheduling} will_continue={r.will_continue} {r.response}")

        # 4b. "stop" is its own tool: a "stop" turn, answered SILENT at once
        n_before = len(s.tool_responses)
        s.push(tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(id="s1", name="stop_agent", args={})]))
        await wait_for(lambda: len(s.tool_responses) > n_before, "stop_agent response")
        r = s.tool_responses[-1]
        stop_turns = [t for t in turns() if t["type"] == "turn" and t["text"] == "stop"]
        check(r.id == "s1" and str(r.scheduling).endswith("SILENT") and stop_turns and stop_turns[-1].get("stop") is True,
              "stop_agent -> a 'stop' turn for the agent and a SILENT response")

        # 5. the relay plays, the user barges in -> flush, flag the next hand-off
        before = speaker_bytes()
        s.push(server_content=content(model_turn=audio_part(tone(2.0))))
        await wait_for(lambda: speaker_bytes() > before + 10000, "relay audio playing", 5)
        s.push(server_content=content(interrupted=True, turn_complete=True))
        await wait_for(lambda: any(t["type"] == "bargein" for t in turns()), "bargein line")
        await asyncio.sleep(0.3)
        after = speaker_bytes()
        await asyncio.sleep(0.5)
        check(speaker_bytes() - after < 2000, f"speaker went quiet after the barge-in ({speaker_bytes() - after} bytes in 0.5 s)")
        s.push(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="c2", name="send_to_agent", args={"message": "never mind, continue"})]))
        await wait_for(lambda: len([t for t in turns() if t["type"] == "turn"]) >= 3, "third turn line")
        turn = [t for t in turns() if t["type"] == "turn"][-1]
        check(turn["text"] == "never mind, continue" and turn["interrupted_agent"] is True, f"hand-off after barge-in flags the cut: {turn}")

        # 5b. named tasks: each task's lines go to its own call; a named stop targets one task
    async with aiohttp.ClientSession() as http:
        s.push(tool_call=types.LiveServerToolCall(function_calls=[
            types.FunctionCall(id="t1", name="send_to_agent", args={"message": "run the tests", "task": "Tests"}),
            types.FunctionCall(id="t2", name="send_to_agent", args={"message": "weather in Tokyo", "task": "weather tokyo"})]))
        await wait_for(lambda: len([t for t in turns() if t["type"] == "turn" and t.get("task")]) >= 2, "two named turns")
        named = [t for t in turns() if t["type"] == "turn" and t.get("task")]
        check(named[-2]["text"] == "[tests] run the tests" and named[-1]["text"] == "[weather tokyo] weather in Tokyo",
              f"named hand-offs carry the task in the text: {[t['text'] for t in named[-2:]]}")
        n = len(s.tool_responses)
        async with http.post(f"{base}/say", json={"text": "[Tests] all green."}) as resp:
            check(resp.status == 200, "tagged line accepted")
        await wait_for(lambda: len(s.tool_responses) > n, "tagged line routed")
        r = s.tool_responses[-1]
        check(r.id == "t1" and r.response.get("report") == "all green.", f"'[Tests]' line went to the tests call, tag stripped: id={r.id} {r.response}")
        n = len(s.tool_responses)
        async with http.post(f"{base}/say", json={"text": "still looking at the forecast"}) as resp:
            pass
        await wait_for(lambda: len(s.tool_responses) > n, "untagged line routed")
        check(s.tool_responses[-1].id == "t2", f"untagged line goes to the latest task: id={s.tool_responses[-1].id}")
        n = len(s.tool_responses)
        s.push(tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(id="s2", name="stop_agent", args={"task": "the tests"})]))
        await wait_for(lambda: len(s.tool_responses) > n, "named stop response")
        stops = [t for t in turns() if t["type"] == "turn" and t.get("stop")]
        check(stops[-1]["text"] == "[tests] stop" and stops[-1]["task"] == "tests", f"named stop maps 'the tests' to the tests task: {stops[-1]['text']}")

    # 6. /state and /stop
        async with http.get(f"{base}/state") as resp:
            st = await resp.json()
            check(resp.status == 200 and st["connected"] and "speaking" in st, f"/state: {st}")
        async with http.post(f"{base}/stop") as resp:
            check(resp.status == 200 and (await resp.json())["ok"], "POST /stop")
        async with http.get(f"{base}/state", headers={"Origin": "http://evil.example"}) as resp:
            check(resp.status == 403, "a browser-looking request is refused")

    # 7. resumption handle, then GoAway -> reconnect with it
    s.push(session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle="h1", resumable=True))
    await asyncio.sleep(0.2)
    s.push(go_away=types.LiveServerGoAway(time_left="5s"))
    await wait_for(lambda: len(sessions) >= 2, "reconnect after GoAway")
    check(len(configs) >= 2 and configs[-1].session_resumption.handle == "h1", "reconnected with the resumption handle")
    await wait_for(lambda: sessions[-1].audio_chunks >= 5, "mic audio flows into the new session")

    # 8. stop -> ear.json gone, task ends
    os.kill(os.getpid(), 15)
    try:
        await asyncio.wait_for(ear_task, 10)
        check(True, "ear exits on SIGTERM")
    except asyncio.TimeoutError:
        check(False, "ear exits on SIGTERM")
    check(not os.path.exists(info_path), "ear.json removed")


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failure(s); files in {TMP}")
    sys.exit(1 if FAILS else 0)
