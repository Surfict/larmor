#!/usr/bin/env python3
"""Two Larmor servers, one microphone: the lease, the takeover and the ghost ear.

    python tests/selftest_mic_lease.py

Loads larmor_server.py twice as two independent modules (A and B) against one scratch
~/.larmor, with `sleep` processes standing in for the agents and the ears. Checks: a
session inside a long listen() keeps its lease, so a second start_voice is refused; once
the lease really is stale the second session takes over, kills the ear the first one
left behind, and the first server notices and stops listening; hand-offs are read only
from a server's own ear; end_voice() from the loser does not switch off the winner.
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="larmor-lease-")
os.environ.update(HOME=TMP, LARMOR_BACKEND="local", LARMOR_LEASE_S="1.5", LARMOR_LEASE_RENEW_S="0.2",
                  LARMOR_WATCH_S="0.3", LARMOR_IDLE_OFF_S="600", LARMOR_SILENCE_OFF_S="600")
HOME = os.path.join(TMP, ".larmor")
FAILS = []
PROCS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def sleeper():
    p = subprocess.Popen(["sleep", "1000"])
    PROCS.append(p)
    return p


def load(name):
    """A fresh copy of the server module, owned by its own stand-in agent process."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "larmor_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.OWNER = sleeper().pid
    mod.NEEDS_ENGINE = False

    def start_ear():
        if mod._ear_alive():
            return
        mod._ear["proc"] = sleeper()
        mod.EAR_INFO.write_text(json.dumps({"port": 0, "pid": mod._ear["proc"].pid}))
    mod._start_ear = start_ear
    return mod


def listen(mod, timeout_s):
    return asyncio.run(mod.listen(timeout_s))


def session():
    return json.loads(open(os.path.join(HOME, "session")).read())


def hand_off(ear_pid, text):
    with open(os.path.join(HOME, "turns.jsonl"), "a") as f:
        f.write(json.dumps({"type": "turn", "channel": "mic", "text": text, "is_final": True,
                            "is_turn_end": True, "wall_clock": time.time(), "ear": ear_pid}) + "\n")


def main():
    A, B = load("larmor_server_a"), load("larmor_server_b")

    print("A starts voice")
    check("ON" in A.start_voice(), "A.start_voice() -> ON")
    ear_a = A._ear_pid()
    check(session().get("owner") == A.OWNER and session().get("ear_pid") == ear_a, "session owned by A, ear_pid recorded")
    hand_off(ear_a, "hello from A's ear")
    check(listen(A, 0.5) == "hello from A's ear", "A hears its own ear")

    print("B tries while A is inside a long listen()")
    got = {}
    t = threading.Thread(target=lambda: got.update(r=listen(A, 2.5)))
    t.start()
    time.sleep(2.0)                     # longer than LEASE_S: only the in-listen renewal keeps the lease alive
    check("another agent" in B.start_voice(), "B.start_voice() refused while A listens")
    check(A._ear_alive(), "A's ear untouched")
    t.join()
    check(got.get("r") == "(silence)", "A's listen() ended quietly")

    print("A goes quiet; B takes over")
    time.sleep(2.0)                     # A makes no voice call: its lease goes stale
    check("ON" in B.start_voice(), "B.start_voice() -> ON once the lease is stale")
    check(session().get("owner") == B.OWNER, "session now owned by B")
    time.sleep(0.3)
    check(not A._pid_alive(ear_a), "B killed the ear A left behind")
    time.sleep(1.0)
    check(not A._ear_alive(), "A's watch noticed and dropped its ear")
    check(A._calls["off_why"] == "another agent took over voice mode", "A remembers why")
    check("took over" in listen(A, 0.2), "A.listen() says so instead of waiting")
    check("took over" in A.speak("x"), "A.speak() says so too")

    print("only B's ear reaches B")
    hand_off(ear_a, "ghost says end voice")
    hand_off(B._ear_pid(), "real hand-off for B")
    check(listen(B, 0.5) == "real hand-off for B", "B ignores the ghost's hand-off")

    print("the loser cannot switch the winner off")
    A.end_voice()
    check(session().get("on") and session().get("owner") == B.OWNER, "A.end_voice() left B's session on")
    B.end_voice()
    check(not session().get("on"), "B.end_voice() switched it off")
    check(not B._ear_alive(), "B's ear stopped")

    print("A can come back")
    check("ON" in A.start_voice(), "A.start_voice() -> ON again")
    A.end_voice()


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in PROCS:
            if p.poll() is None:
                p.kill()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("all ok")
