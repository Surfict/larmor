#!/usr/bin/env python3
"""The user interrupts the agent while it sits in listen(): nothing is lost, and voice mode
lets go on its own.

    python tests/selftest_listen_cancel.py

Runs larmor_server.py as a real stdio MCP server (tests/_server_stub.py: a scratch ~/.larmor,
a stand-in ear) and drives it with the MCP client. Checks: a cancelled listen() does not take
the next hand-off, which the next listen() gets; the cancellation is counted; a cancelled
listen() that no voice call follows gets the ear to tell the user and switches voice mode off.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="larmor-cancel-")
HOME = os.path.join(TMP, ".larmor")
FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def hand_off(text):
    with open(os.path.join(HOME, "turns.jsonl"), "a") as f:
        f.write(json.dumps({"type": "turn", "channel": "gemini", "text": text, "is_final": True,
                            "is_turn_end": True, "wall_clock": time.time()}) + "\n")


def metrics(kind):
    try:
        return [json.loads(l) for l in open(os.path.join(HOME, "metrics.jsonl")) if f'"{kind}"' in l]
    except OSError:
        return []


async def main():
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "_server_stub.py")],
                                   env={"HOME": TMP, "LARMOR_BACKEND": "local", "LARMOR_WATCH_S": "0.3",
                                        "LARMOR_INTERRUPT_OFF_S": "1.5", "LARMOR_IDLE_OFF_S": "600",
                                        "LARMOR_SILENCE_OFF_S": "600"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            text = lambda r: r.content[0].text  # noqa: E731

            print("a hand-off waiting is returned at once")
            check(text(await s.call_tool("listen", {"timeout_s": 0.2})) == "(silence)", "listen() -> (silence)")
            hand_off("first")
            r = text(await s.call_tool("listen", {"timeout_s": 2}))
            check(r == "first", f"listen() -> first: {r[:80]!r}")

            print("the user interrupts a listen(), then the assistant hands something off")
            with anyio.move_on_after(0.5):
                await s.call_tool("listen", {"timeout_s": 10})
            await anyio.sleep(0.3)
            hand_off("said after the interrupt")
            await anyio.sleep(0.5)                       # the old code's thread would take it here
            r = text(await s.call_tool("listen", {"timeout_s": 2}))
            check(r == "said after the interrupt", f"next listen() gets the hand-off the cancelled one did not take: {r[:80]!r}")
            check(len(metrics("listen_cancelled")) == 1, "the cancellation was counted")
            check(json.load(open(os.path.join(HOME, "session"))).get("on"), "voice mode still on: the agent came back")

            print("the user interrupts and does not come back")
            with anyio.move_on_after(0.5):
                await s.call_tool("listen", {"timeout_s": 10})
            await anyio.sleep(3.0)
            sess = json.load(open(os.path.join(HOME, "session")))
            check(not sess.get("on"), "voice mode switched itself off")
            said = open(os.path.join(HOME, "ear_said.txt")).read() if os.path.exists(os.path.join(HOME, "ear_said.txt")) else ""
            check("interrupted" in said, f"the ear told the user first: {said[:60]!r}")
            off = metrics("voice_auto_off")
            check(off and "interrupted" in off[-1]["why"], "auto-off says why")
            r = text(await s.call_tool("listen", {"timeout_s": 1}))
            check("interrupted" in r and "start_voice" in r, "listen() now tells the agent voice mode is off")


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("all ok")
