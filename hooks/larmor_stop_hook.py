#!/usr/bin/env python3
"""Stop hook: a turn must never end while voice mode is on.

If voice mode is on, ending the turn leaves the user talking to a process that
isn't listening, and they get no error. The only ways out are listen() (the
conversation continues) or end_voice() (it's over). The state is binary, so the
hook is too: no grace periods or block caps, which only ever went quiet exactly
when they were needed.

Scoped to one session: the claim hook records which session called start_voice,
and only that session is held to it. Other sessions on the same machine are left
alone.

LOOP SAFETY, by our own evidence rather than the harness's `stop_hook_active`
flag. That flag is set once a Stop hook has blocked and then stays set for the
rest of the prompt turn, however many listen() calls happen in between; a voice
turn is one prompt turn, so honouring it meant the hook worked exactly once per
voice session and then let every later stop through (the agent went quiet for
five minutes while the assistant kept saying "passing that on"). Instead: each
block is stamped in ~/.larmor/stop_block, and the server stamps every listen()
in the session file. A stop is blocked if the agent has listened since the last
block (the previous nudge worked, this one will too). If it has not, the agent
ignored the nudge, so blocking again would only loop: let it go. Claude Code's
own cap (8 consecutive blocks with no tool call between them) stays as the
backstop. Any exception exits 0. Escape hatch: end_voice(), or
`rm ~/.larmor/session`.
"""
import json
import os
import sys
import time

HOME = os.path.expanduser("~/.larmor")
SESSION = os.path.join(HOME, "session")
STOP_BLOCK = os.path.join(HOME, "stop_block")    # {"t": <wall clock>, "session_id": <who>}


def _last_block(sid: str) -> float:
    try:
        b = json.loads(open(STOP_BLOCK).read())
        return float(b["t"]) if b.get("session_id") == sid else 0.0
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _stamp_block(sid: str) -> None:
    tmp = STOP_BLOCK + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"t": time.time(), "session_id": sid}, f)
    os.replace(tmp, STOP_BLOCK)


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        session = json.loads(open(SESSION).read())
    except (OSError, ValueError):
        return 0                       # no session file => not in voice mode
    if not session.get("on"):
        return 0
    owner = session.get("session_id")
    if not owner or payload.get("session_id") != owner:
        return 0                       # voice mode belongs to another session (or is unclaimed)
    last_listen = float(session.get("last_listen") or 0)
    if last_listen <= _last_block(owner):
        return 0                       # told to listen after the last stop and didn't: don't loop
    _stamp_block(owner)
    print("Voice mode is ON and you are ending your turn. The user is waiting "
          "and cannot reach you. Call the larmor listen tool now. If the "
          "conversation is genuinely over, call end_voice first.",
          file=sys.stderr)
    return 2                           # exit 2 = block; stderr goes to the agent


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        rc = 0                         # fail open, always
    if rc == 0:
        print("{}")                    # Codex wants JSON on stdout when a Stop hook exits 0
    sys.exit(rc)
