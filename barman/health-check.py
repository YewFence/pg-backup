#!/usr/bin/env python3
"""Barman health check HTTP server.

Periodically runs `barman check` and caches the result.
Exposes an HTTP endpoint that returns 200 (OK) or 503 (failing/stale).

Environment variables:
    BARMAN_SERVER_NAME  - server name to check (default: streaming-backup-server)
    HEALTH_CHECK_PORT   - HTTP port (default: 8000)
    CHECK_INTERVAL      - seconds between checks (default: 300)
    FAIL_THRESHOLD      - consecutive failures before marking unhealthy (default: 3)
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SERVER_NAME = os.environ.get("BARMAN_SERVER_NAME", "streaming-backup-server")
PORT = int(os.environ.get("HEALTH_CHECK_PORT", "8000"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
FAIL_THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "3"))
STALE_THRESHOLD = CHECK_INTERVAL * 2

last_result = None
last_check_time = 0
fail_count = 0
lock = threading.Lock()


def run_check_loop():
    global last_result, last_check_time, fail_count
    while True:
        try:
            proc = subprocess.run(
                ["barman", "check", SERVER_NAME],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0:
                with lock:
                    fail_count = 0
                    last_result = {"ok": True, "output": proc.stdout.strip()}
                    last_check_time = time.time()
            else:
                with lock:
                    fail_count += 1
                    if fail_count >= FAIL_THRESHOLD:
                        last_result = {"ok": False, "output": proc.stdout.strip()}
                        last_check_time = time.time()
                    print(f"barman check failed ({fail_count}/{FAIL_THRESHOLD})")
        except Exception as e:
            with lock:
                fail_count += 1
                if fail_count >= FAIL_THRESHOLD:
                    last_result = {"ok": False, "output": str(e)}
                    last_check_time = time.time()
                print(f"barman check error ({fail_count}/{FAIL_THRESHOLD}): {e}")
        time.sleep(CHECK_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            if last_result is None:
                status, body = 503, {"status": "pending", "message": "first check not yet completed"}
            else:
                now = time.time()
                age = int(now - last_check_time)

                if age > STALE_THRESHOLD:
                    status, body = 503, {"status": "stale", "seconds_ago": age, "last_check": last_result}
                elif not last_result["ok"]:
                    status, body = 503, {"status": "failing", "seconds_ago": age, "last_check": last_result}
                else:
                    status, body = 200, {"status": "ok", "seconds_ago": age}

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=run_check_loop, daemon=True).start()
    print(f"Health check server listening on :{PORT} (checking {SERVER_NAME} every {CHECK_INTERVAL}s)")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
