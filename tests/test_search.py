#!/usr/bin/env python3
"""Offline tests for search-by-text: python3 tests/test_search.py
A local server stands in for the source's search API, and commands run through tests/cgi_adapter.py (v_* vars)."""
import json, os, subprocess, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
HITS = [{"songId": 320, "artist": "Johann Sebastian Bach", "title": "Bourre In E Minor", "tracks": []},
        {"songId": 321, "artist": "Johann Sebastian Bach", "title": "Bourre In E Minor (live)"}]
SEEN = []


class Fake(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        SEEN.append((u.path, q))
        body = json.dumps([] if q["pattern"] == ["nothing"] else HITS[: int(q["size"][0])]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Search(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.data = tempfile.mkdtemp()  # a fresh job store per test, so no job is reused from another test

    def cgi(self, command, **params):
        env = {**os.environ, "DATA_DIR": self.data, "UPSTREAM_DIR": str(ROOT / "tests" / "fake_upstream"), "UPSTREAM_PYTHON": sys.executable, "SOURCE_URL": f"http://127.0.0.1:{self.srv.server_port}",
               **{f"v_{k}": v for k, v in params.items()}}
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "cgi_adapter.py"), command], capture_output=True, env=env, timeout=30)
        head, _, body = p.stdout.partition(b"\n\n")
        status = int(head.split(b"\n")[0].split()[1])
        return status, body.decode()

    def test_search_lists_hits(self):
        status, body = self.cgi("api-search", q="johann sebastian bach bourre in e minor")
        self.assertEqual(status, 200)
        hits = json.loads(body)
        self.assertEqual([h["song"] for h in hits], ["320", "321"])
        self.assertEqual(hits[0]["artist"], "Johann Sebastian Bach")
        self.assertEqual(SEEN[-1][1]["pattern"], ["johann sebastian bach bourre in e minor"])

    def test_search_needs_text(self):
        self.assertEqual(self.cgi("api-search")[0], 400)

    def test_text_alone_starts_nothing_and_lists_the_matches(self):
        status, body = self.cgi("api-create", song="johann sebastian bach bourre in e minor")
        self.assertEqual(status, 409, body)
        doc = json.loads(body)
        self.assertEqual([m["song"] for m in doc["matches"]], ["320", "321"])
        self.assertEqual((doc["matches"][0]["artist"], doc["matches"][0]["title"]), ("Johann Sebastian Bach", "Bourre In E Minor"))
        self.assertEqual(doc["query"], "johann sebastian bach bourre in e minor")
        self.assertEqual(self.cgi("api-list")[1].strip(), "[]", "no job was created")

    def test_first_takes_the_first_result_and_says_what_it_was(self):
        status, body = self.cgi("api-create", song="johann sebastian bach bourre in e minor", first="1")
        self.assertEqual(status, 202, body)
        job = json.loads(body)
        self.assertEqual(job["song"], "320")
        self.assertEqual(job["matched"], {"query": "johann sebastian bach bourre in e minor", "artist": "Johann Sebastian Bach", "title": "Bourre In E Minor"})

    def test_q_works_as_an_alias(self):
        status, body = self.cgi("api-create", q="johann sebastian bach bourre in e minor", first="1", format="gp")
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["song"], "320")

    def test_confirming_with_the_id_starts_the_job(self):
        status, body = self.cgi("api-create", song="321")
        self.assertEqual((status, json.loads(body)["song"]), (202, "321"))
        self.assertNotIn("matched", json.loads(body))

    def test_id_and_url_do_not_search(self):
        n = len(SEEN)
        status, body = self.cgi("api-create", song="https://tabs.example.test/a/wsa/x-tab-s555")
        self.assertEqual((status, json.loads(body)["song"]), (202, "555"))
        self.assertEqual(len(SEEN), n)

    def test_no_match_is_a_400(self):
        status, body = self.cgi("api-create", song="nothing")
        self.assertEqual(status, 400)
        self.assertIn("No song matches", json.loads(body)["error"])

    def test_form_shows_results_for_text_and_starts_for_an_id(self):
        status, body = self.cgi("start", song="johann sebastian bach bourre in e minor")
        self.assertEqual(status, 200)
        self.assertIn("Bourre In E Minor (live)", body)
        self.assertIn('name="song" value="321"', body)
        status, _ = self.cgi("start", song="320")
        self.assertEqual(status, 303)


if __name__ == "__main__":
    unittest.main()
