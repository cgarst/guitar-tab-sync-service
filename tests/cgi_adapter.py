#!/usr/bin/env python3
"""Test helper: run one command of the service in a fresh process, the way the older tests expect.

    python3 tests/cgi_adapter.py <command>

Parameters come from `v_<name>` environment variables and request headers from `HTTP_<NAME>` ones. It builds the request the
server would, calls gpsync.dispatch, and prints "Status: N", the headers, a blank line and the body. The environment is read
when gpsync is imported, so each call can use its own DATA_DIR, AUTH_TOKEN, PUBLIC_BASE_URL and so on.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import gpsync  # noqa: E402

params = {k[2:]: v for k, v in os.environ.items() if k.startswith("v_")}
headers = {k[5:].replace("_", "-"): v for k, v in os.environ.items() if k.startswith("HTTP_")}
status, lines, body = gpsync.dispatch(sys.argv[1], params, headers)
if isinstance(body, Path):
    body = body.read_bytes()
out = sys.stdout.buffer
out.write(("\n".join([f"Status: {status}", *lines]) + "\n\n").encode() + body)
out.flush()
