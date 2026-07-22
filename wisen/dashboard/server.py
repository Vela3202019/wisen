"""
Wisen dashboard server — stdlib only, no dependencies, for hosting on a VPS.

Serves the reflection history (JSON files synced from the machine that runs
synthesis) and accepts manual reflection triggers, which are queued as files
for the synthesis machine to pick up.

  GET  /             dashboard UI
  GET  /api/logs     all reflections, newest first
  GET  /api/usage    subscription usage snapshot (usage.json, synced from laptop)
  GET  /api/queue    pending trigger dates
  POST /api/trigger  {"date": "YYYY-MM-DD"} → queue a reflection request

Env:
  DASH_PASSWORD   required — HTTP Basic auth password (user: wisen)
  DASH_BIND       default 0.0.0.0 (set 127.0.0.1 when behind a reverse proxy)
  DASH_PORT       default 8722
  DASH_DATA_DIR   default ~/wisen-dash/data   (reflection JSON files)
  DASH_QUEUE_DIR  default ~/wisen-dash/queue  (trigger request files)
"""
import base64
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PASSWORD = os.environ.get("DASH_PASSWORD", "")
BIND = os.environ.get("DASH_BIND", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8722"))
DATA_DIR = Path(os.environ.get("DASH_DATA_DIR", "~/wisen-dash/data")).expanduser()
QUEUE_DIR = Path(os.environ.get("DASH_QUEUE_DIR", "~/wisen-dash/queue")).expanduser()
INDEX_HTML = Path(__file__).parent / "index.html"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Handler(BaseHTTPRequestHandler):
    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"wisen:{PASSWORD}".encode()).decode()
        return bool(PASSWORD) and header == expected

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="wisen"')
        self.end_headers()

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._authed():
            return self._deny()
        if self.path == "/" or self.path == "/index.html":
            body = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/logs":
            logs = []
            for f in sorted(DATA_DIR.glob("*.json"), reverse=True):
                if not _DATE_RE.match(f.stem):
                    continue
                try:
                    logs.append(json.loads(f.read_text()))
                except Exception:
                    continue
            self._json(logs)
        elif self.path == "/api/usage":
            try:
                self._json(json.loads((DATA_DIR / "usage.json").read_text()))
            except Exception:
                self._json({})
        elif self.path == "/api/queue":
            self._json(sorted(f.stem for f in QUEUE_DIR.glob("*.req")))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authed():
            return self._deny()
        if self.path != "/api/trigger":
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            date = json.loads(self.rfile.read(length)).get("date", "")
        except Exception:
            date = ""
        if not _DATE_RE.match(date):
            return self._json({"error": "date must be YYYY-MM-DD"}, 400)
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        (QUEUE_DIR / f"{date}.req").touch()
        self._json({"queued": date})

    def log_message(self, fmt, *args) -> None:
        pass  # keep systemd journal quiet


def main() -> None:
    if not PASSWORD:
        raise SystemExit("DASH_PASSWORD env var is required")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"wisen dashboard on {BIND}:{PORT}, data={DATA_DIR}, queue={QUEUE_DIR}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
