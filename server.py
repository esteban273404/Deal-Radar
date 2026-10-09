#!/usr/bin/env python3
"""Deal Radar helper: serves the page at http://localhost:8766 and runs
fetch_deals.py when the page's "Update now" button asks for it.

Usage:  python3 server.py   (kept running by launchd: com.dealradar.server)
Only listens on this Mac (127.0.0.1). Only uses the Python standard library.
"""
import fcntl
import json
import subprocess
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8766
# pages allowed to start an update: this server, and deals.html opened as a file
ALLOWED_ORIGINS = {"null", "http://localhost:%d" % PORT, "http://127.0.0.1:%d" % PORT}

job = {"proc": None, "started": None, "last_exit": None}


def running():
    p = job["proc"]
    if p is not None and p.poll() is not None:
        job["last_exit"], job["proc"] = p.returncode, None
    if job["proc"] is not None:
        return True
    # the scheduled refresh holds this lock while it runs
    try:
        with open(ROOT / "data" / ".refresh.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def status():
    try:
        updated = json.loads((ROOT / "data" / "deals.json").read_text())["updated"]
    except Exception:
        updated = None
    return {"running": running(), "started": job["started"], "last_exit": job["last_exit"],
            "updated": updated}


class Handler(SimpleHTTPRequestHandler):
    def cors(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/deals.html")
            self.end_headers()
        elif self.path == "/status":
            self.send_json(200, status())
        else:
            super().do_GET()

    def do_OPTIONS(self):
        # preflight for the page's JSON POST to /watchlist
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def save_watchlist(self):
        """The page sends its watchlist here, so scheduled updates can alert on matches."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            items = json.loads(self.rfile.read(min(n, 50000)) or b"[]")
            clean = [{"term": str(w["term"])[:120],
                      "max_price": float(w["max_price"]) if w.get("max_price") is not None else None}
                     for w in items if isinstance(w, dict) and str(w.get("term", "")).strip()][:100]
        except Exception:
            return self.send_json(400, {"error": "bad watchlist"})
        (ROOT / "data" / "watchlist.json").write_text(json.dumps(clean, indent=1))
        self.send_json(200, {"saved": len(clean)})

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return self.send_json(403, {"error": "origin not allowed"})
        if self.path == "/watchlist":
            return self.save_watchlist()
        if self.path != "/refresh":
            return self.send_json(404, {"error": "not found"})
        if not running():
            log = open(ROOT / "data" / "refresh.log", "a")
            log.write("\n--- manual update %s\n" % time.strftime("%Y-%m-%d %H:%M"))
            log.flush()
            job["proc"] = subprocess.Popen([sys.executable, "-u", str(ROOT / "fetch_deals.py")],
                                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            job["started"] = time.time()
        self.send_json(202, status())

    def end_headers(self):
        if self.path.endswith(".html"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(ROOT))).serve_forever()
