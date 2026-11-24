#!/usr/bin/env python3
"""The HTTP server: `python3 server.py`. Stdlib only (ThreadingHTTPServer, one thread per connection).

It maps a method and path to a command of gpsync.py (ROUTES), reads the query string and a form body into one dict of
parameters, and sends back what the handler stored with `emit`. Every request is logged as `METHOD /path STATUS`, without
the query string, because the access token travels there.

At start it fails the jobs a previous container left half-done and sweeps expired files; a thread then sweeps every
10 minutes (a container can stay up for months).
"""
import os
import signal
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import gpsync

ROUTES = [  # (method, path, command)
    ("GET", "/", "index"),
    ("POST", "/get", "start"),
    ("GET", "/get", "start"),
    ("GET", "/job", "job"),
    ("POST", "/delete", "delete"),
    ("POST", "/delete-all", "delete-all"),
    ("POST", "/api/jobs", "api-create"),
    ("GET", "/api/get", "api-create"),
    ("GET", "/api/jobs", "api-list"),
    ("GET", "/api/job", "api-job"),
    ("DELETE", "/api/job", "api-delete"),
    ("GET", "/api/job/file", "api-file"),
    ("GET", "/api/search", "api-search"),
    ("GET", "/openapi.json", "openapi"),
    ("GET", "/opensearch.xml", "opensearch"),
    ("GET", "/healthz", "health"),
]
BY_ROUTE = {(m, p): c for m, p, c in ROUTES}
PATHS = {p for _, p, _ in ROUTES}
MAX_BODY = 64 * 1024   # a form is a few hundred bytes
FORM = "application/x-www-form-urlencoded"
SWEEP_EVERY = 600      # seconds


def parse_params(query, body="", content_type=""):
    """Query-string values and, for a form body, its values. The first value of a name wins, and the body comes first."""
    params = {}
    pairs = parse_qsl(body, keep_blank_values=True) if content_type.split(";")[0].strip().lower() == FORM else []
    for k, v in pairs + parse_qsl(query, keep_blank_values=True):
        params.setdefault(k, v)
    return params


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60   # an idle or stalled client does not hold a thread for ever
    server_version = "gpsync"
    sys_version = ""

    def log_request(self, code="-", size="-"):
        print(f"{self.command} {urlsplit(self.path).path} {code}", file=sys.stderr, flush=True)

    def log_message(self, fmt, *args):   # protocol errors
        print(fmt % args, file=sys.stderr, flush=True)

    def send_plain(self, status, text, extra=()):
        body = text.encode()
        self.send_response(status)
        for line in ("Content-Type: text/plain; charset=utf-8", f"Content-Length: {len(body)}", *extra):
            self.send_header(*line.split(": ", 1))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self):
        url = urlsplit(self.path)
        raw = self.headers.get("Content-Length", "0")
        length = int(raw) if raw.isdigit() else -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True   # the body is not read, so the connection cannot be reused
            return self.send_plain(413, "Request body too large.", ["Connection: close"])
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""   # always read it, for keep-alive
        command = BY_ROUTE.get((self.command, url.path))
        if not command:
            if url.path in PATHS:
                allow = ", ".join(sorted({m for m, p, _ in ROUTES if p == url.path}))
                return self.send_plain(405, "Method not allowed.", [f"Allow: {allow}"])
            return self.send_plain(404, "Not found.")
        params = parse_params(url.query, body, self.headers.get("Content-Type", ""))
        try:
            status, lines, payload = gpsync.dispatch(command, params, dict(self.headers.items()))
        except Exception:
            traceback.print_exc()
            return self.send_plain(500, "Internal error.")
        self.send_response(status)
        for line in lines:
            self.send_header(*line.split(": ", 1))
        self.end_headers()
        if isinstance(payload, bytes):
            self.wfile.write(payload)
        else:   # a file: stream it
            with open(payload, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)

    def do_any(self):
        try:
            self.handle_request()
        except (ConnectionError, TimeoutError):
            self.close_connection = True   # the client went away

    do_GET = do_POST = do_DELETE = do_any


def sweep_forever():
    while True:
        time.sleep(SWEEP_EVERY)
        try:
            gpsync.sweep()
        except Exception:
            traceback.print_exc()


def make_server(port, host="0.0.0.0"):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    gpsync.JOBS.mkdir(parents=True, exist_ok=True)
    gpsync.recover()   # fail jobs a previous container left unfinished
    gpsync.sweep()
    threading.Thread(target=sweep_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    port = int(os.environ.get("PORT", "8080"))
    print(f"listening on :{port}", file=sys.stderr, flush=True)
    make_server(port).serve_forever()


if __name__ == "__main__":
    main()
