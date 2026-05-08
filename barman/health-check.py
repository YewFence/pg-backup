#!/usr/bin/env python3
"""Barman health check HTTP server.

Periodically runs `barman check` and caches the result.
Exposes an HTTP endpoint that returns 200 (OK) or 503 (failing/stale).

Environment variables:
    HEALTH_CHECK_PORT   - HTTP port
    CHECK_INTERVAL      - seconds between checks
    FAIL_THRESHOLD      - consecutive failures before marking unhealthy
"""

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PORT = int(os.environ.get("HEALTH_CHECK_PORT", "8000"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
FAIL_THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "3"))
STALE_THRESHOLD = CHECK_INTERVAL * 2
CONFIG_DIR = Path(os.environ.get("BARMAN_CONFIG_DIR", "/etc/barman.d"))
IGNORED_SECTIONS = {"barman", "global"}

results = {}
lock = threading.Lock()


def discover_server_names():
    names = []
    section_re = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*$")
    for conf_file in sorted(CONFIG_DIR.glob("*.conf")):
        try:
            for line in conf_file.read_text(encoding="utf-8").splitlines():
                match = section_re.match(line)
                if match:
                    section_name = match.group(1)
                    if section_name not in IGNORED_SECTIONS and section_name not in names:
                        names.append(section_name)
        except OSError as exc:
            print(f"could not read {conf_file}: {exc}")

    return names or ["streaming-backup-server"]


SERVER_NAMES = discover_server_names()


def check_server(server_name):
    proc = subprocess.run(
        ["barman", "check", server_name],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode == 0, (proc.stdout or proc.stderr).strip()


def run_check_loop():
    while True:
        for server_name in SERVER_NAMES:
            try:
                ok, output = check_server(server_name)
                with lock:
                    current = results.setdefault(server_name, {"fail_count": 0})
                    if ok:
                        current.update({
                            "ok": True,
                            "output": output,
                            "last_check_time": time.time(),
                            "fail_count": 0,
                        })
                    else:
                        current["fail_count"] = current.get("fail_count", 0) + 1
                        if current["fail_count"] >= FAIL_THRESHOLD:
                            current.update({
                                "ok": False,
                                "output": output,
                                "last_check_time": time.time(),
                            })
                        print(
                            f"barman check failed for {server_name} "
                            f"({current['fail_count']}/{FAIL_THRESHOLD})"
                        )
            except Exception as e:
                with lock:
                    current = results.setdefault(server_name, {"fail_count": 0})
                    current["fail_count"] = current.get("fail_count", 0) + 1
                    if current["fail_count"] >= FAIL_THRESHOLD:
                        current.update({
                            "ok": False,
                            "output": str(e),
                            "last_check_time": time.time(),
                        })
                    print(
                        f"barman check error for {server_name} "
                        f"({current['fail_count']}/{FAIL_THRESHOLD}): {e}"
                    )
        time.sleep(CHECK_INTERVAL)


def current_status():
    now = time.time()
    servers = {}
    pending = []
    stale = []
    failing = []

    for server_name in SERVER_NAMES:
        result = results.get(server_name)
        if not result or "last_check_time" not in result:
            pending.append(server_name)
            servers[server_name] = {"status": "pending"}
            continue

        age = int(now - result["last_check_time"])
        server_body = {
            "seconds_ago": age,
            "ok": result.get("ok", False),
        }

        if age > STALE_THRESHOLD:
            stale.append(server_name)
            server_body["status"] = "stale"
        elif not result.get("ok", False):
            failing.append(server_name)
            server_body["status"] = "failing"
            server_body["output"] = result.get("output", "")
        else:
            server_body["status"] = "ok"

        servers[server_name] = server_body

    if pending:
        return 503, {"status": "pending", "pending": pending, "servers": servers}
    if stale:
        return 503, {"status": "stale", "stale": stale, "servers": servers}
    if failing:
        return 503, {"status": "failing", "failing": failing, "servers": servers}
    return 200, {"status": "ok", "servers": servers}


def server_status(server_name):
    if server_name not in SERVER_NAMES:
        return 404, {"status": "not_found", "server": server_name}

    now = time.time()
    result = results.get(server_name)
    if not result or "last_check_time" not in result:
        return 503, {"status": "pending", "server": server_name}

    age = int(now - result["last_check_time"])
    body = {
        "server": server_name,
        "seconds_ago": age,
        "ok": result.get("ok", False),
    }

    if age > STALE_THRESHOLD:
        body["status"] = "stale"
        return 503, body
    if not result.get("ok", False):
        body["status"] = "failing"
        body["output"] = result.get("output", "")
        return 503, body

    body["status"] = "ok"
    return 200, body


def status_for_path(path):
    parsed_path = unquote(urlparse(path).path)
    if parsed_path in ("", "/"):
        return current_status()

    server_name = parsed_path.strip("/")
    if "/" in server_name or not server_name:
        return 404, {"status": "not_found", "path": parsed_path}

    return server_status(server_name)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            status, body = status_for_path(self.path)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=run_check_loop, daemon=True).start()
    print(
        f"Health check server listening on :{PORT} "
        f"(checking {', '.join(SERVER_NAMES)} every {CHECK_INTERVAL}s)"
    )
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
