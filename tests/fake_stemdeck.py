#!/usr/bin/env python3
"""Fake StemDeck for offline tests: tests/fake_stemdeck.py <port>. Implements only the calls app/stemdeck.py makes.
A job is "separating" for its first two polls, then done. An upload named for song 888 ends in error."""
import json, sys, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JOBS = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/health":
            return self.send(200, {"name": "StemDeck", "version": "0.0-fake", "demucs_device": "cpu",
                                   "export_formats": {"mp3": True, "flac": True}})
        parts = p.strip("/").split("/")
        job = JOBS.get(parts[2]) if len(parts) >= 3 and parts[:2] == ["api", "jobs"] else None
        if not job:
            return self.send(404, {"detail": "job not found"})
        if len(parts) == 3:
            job["polls"] += 1
            if job["fail"]:
                return self.send(200, {"status": "error", "progress": 30, "stage": "", "error": "separation blew up"})
            done = job["polls"] > 2
            return self.send(200, {"status": "done" if done else "separating",
                                   "progress": 100 if done else 50, "stage": "Separating"})
        if parts[3].startswith("mixdown."):
            stem = self.path.split("stems=")[1].split("&")[0]
            return self.send(200, f"ID3fake-{stem}".encode(), "audio/mpeg")
        self.send(404, {"detail": "no such route"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path == "/api/jobs":
            assert b'["vocals", "drums", "bass", "guitar", "piano", "other"]' in body, "full stem set not requested"
            job_id = uuid.uuid4().hex[:12]
            JOBS[job_id] = {"polls": 0, "fail": b'filename="Fake%20Artist%20-%20Song%20888_raw.mp3"' in body}
            return self.send(200, {"job_id": job_id})
        self.send(200, {})

    def do_DELETE(self):
        JOBS.pop(self.path.rsplit("/", 1)[-1], None)
        self.send(200, {})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
