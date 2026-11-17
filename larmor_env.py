""".env next to this file (Larmor's own folder, ~/.larmor/app/.env for a normal install):
settings and keys for Larmor, one KEY=value per line.

    GEMINI_API_KEY=...          # the Gemini voice
    LARMOR_BACKEND=gemini       # or local; optional, the key alone picks gemini
    LARMOR_GEMINI_VOICE=Kore    # any LARMOR_* setting works here too

Every Larmor process loads it at start (the MCP server, the menu-bar app, the engine; the
ears inherit the server's environment), so a key never has to live in a shell profile or an
agent's config. A variable already in the environment wins over the file. Blank lines and #
comments are ignored, a leading "export " and surrounding quotes are allowed. The installer
creates it (mode 600) with an empty GEMINI_API_KEY line for the user to fill in with an
editor; the key is never typed into a terminal, so it never lands in a shell history or log.
"""
import os
from pathlib import Path

ENV_FILE = Path(os.getenv("LARMOR_ENV_FILE") or Path(__file__).resolve().parent / ".env").expanduser()


def read() -> dict:
    out = {}
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load() -> None:
    """Put the file's variables into os.environ unless they are already set."""
    for k, v in read().items():
        os.environ.setdefault(k, v)


load()
