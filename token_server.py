import json
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.txt")
PORT = 5000

lock = threading.Lock()
token_queue = deque()


def load_existing_tokens():
    if not os.path.exists(TOKEN_FILE):
        return
    with open(TOKEN_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or "u_asig=" not in line:
                continue
            try:
                parts = dict(p.split("=", 1) for p in line.split(" | ") if "=" in p)
                tok = parts.get("u_atoken", "").strip()
                cert = parts.get("u_asig", "").strip()
                if tok and cert and tok != "N/A" and cert != "N/A":
                    token_queue.append((tok, cert))
            except Exception:
                pass


class TokenHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/get/token"):
            with lock:
                if token_queue:
                    tok, cert = token_queue.popleft()
                    raw = f"u_atoken={tok}&u_asig={cert}"
                    self._respond(200, raw, "text/plain")
                else:
                    self._respond(200, "NO_TOKENS", "text/plain")
        elif self.path == "/status":
            with lock:
                count = len(token_queue)
            self._respond(200, json.dumps({"tokens_available": count}), "application/json")
        else:
            self._respond(404, "not found", "text/plain")

    def do_POST(self):
        if self.path in ("/api/submit-token", "/submit-token", "/submit/token", "/submit", "/api/token", "/tokens"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                tok = data.get("u_atoken") or data.get("token") or ""
                cert = data.get("u_asig") or data.get("asig") or ""
                if tok and cert and str(tok).upper() not in ("N/A", "NONE", "NULL", "UNDEFINED", "") and str(cert).upper() not in ("N/A", "NONE", "NULL", "UNDEFINED", ""):
                    with lock:
                        token_queue.append((str(tok), str(cert)))
                    with open(TOKEN_FILE, "a", encoding="utf-8") as f:
                        f.write(f"TIMESTAMP={int(time.time())} | u_atoken={tok} | u_asig={cert}\n")
                    self._respond(200, json.dumps({"ok": True, "queued": len(token_queue)}), "application/json")
                else:
                    self._respond(400, json.dumps({"ok": False, "error": "missing token or asig"}), "application/json")
            except Exception as e:
                self._respond(400, json.dumps({"ok": False, "error": str(e)}), "application/json")
        else:
            self._respond(404, "not found", "text/plain")

    def _respond(self, code, body, content_type):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    load_existing_tokens()
    server = HTTPServer(("0.0.0.0", PORT), TokenHandler)
    print(f"Token server running on port {PORT} — {len(token_queue)} tokens loaded")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
