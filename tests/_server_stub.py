"""larmor_server.py as a stdio MCP server with a stand-in ear, for tests/selftest_listen_cancel.py.

HOME must point at a scratch directory. Voice mode starts on and owned by this process; the ear
is a thread serving /say and /state the way larmor_ear_gemini.py does, and records what it was
told to say in ~/.larmor/ear_said.txt.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import larmor_server as srv  # noqa: E402


class _Ear(BaseHTTPRequestHandler):
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        text = json.loads(self.rfile.read(int(self.headers["Content-Length"]))).get("text", "")
        with open(srv.HOME / "ear_said.txt", "a") as f:
            f.write(text + "\n")
        self._json({"ok": True, "queued": 0})

    def do_GET(self):
        self._json({"speaking": False, "queued": 0})

    def log_message(self, *_):
        pass


class _FakeProc:
    pid = os.getpid()

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass


httpd = HTTPServer(("127.0.0.1", 0), _Ear)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
srv.HOME.mkdir(parents=True, exist_ok=True)
srv.EAR_INFO.write_text(json.dumps({"port": httpd.server_address[1], "pid": os.getpid()}))
srv.SESSION.write_text(json.dumps({"on": True, "since": time.time(), "owner": srv.OWNER, "backend": "local",
                                   "last_listen": time.time()}))
srv._ear["proc"] = _FakeProc()
srv._calls["heard"] = srv._calls["last"] = time.time()
srv._calls["watch"] = threading.Thread(target=srv._watch, daemon=True)
srv._calls["watch"].start()
srv.mcp.run()
