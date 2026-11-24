#!/usr/bin/env python3
"""Offline tests for the optional access token: python3 tests/test_auth.py
Commands run through tests/cgi_adapter.py (HTTP_* headers and v_* parameters in the environment)."""
import base64, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "s3cret-token"
JOB = "23063-abc123"


class Auth(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        out = self.data / "jobs" / JOB / "out"
        out.mkdir(parents=True)
        (out / "x_synced.gp").write_bytes(b"GPDATA")
        (self.data / "jobs" / JOB / "job.json").write_text(json.dumps(
            {"id": JOB, "song": "23063", "kind": "gp", "status": "done", "created": "2026-01-01T00:00:00Z",
             "file": "x_synced.gp", "audio": True}))

    def cgi(self, command, token=TOKEN, auth=None, token_param=None, cookie=None, **params):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HTTP_", "v_", "AUTH_"))}
        env.update(DATA_DIR=str(self.data), UPSTREAM_DIR=str(ROOT / "tests" / "fake_upstream"), UPSTREAM_PYTHON=sys.executable,
                   OPEN_LINK_TEMPLATE="app://open?url={url}", PUBLIC_BASE_URL="http://h")
        if token:
            env["AUTH_TOKEN"] = token
        if auth:
            env["HTTP_AUTHORIZATION"] = auth
        if cookie:
            env["HTTP_COOKIE"] = cookie
        if token_param is not None:
            params["token"] = token_param
        env.update({f"v_{k}": v for k, v in params.items()})
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "cgi_adapter.py"), command], capture_output=True, env=env, timeout=30)
        head, _, body = p.stdout.partition(b"\n\n")
        lines = head.decode().split("\n")
        return int(lines[0].split()[1]), lines[1:], body

    basic = staticmethod(lambda user, pw: "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())

    def test_open_when_no_token_is_set(self):
        self.assertEqual(self.cgi("api-list", token="")[0], 200)
        self.assertEqual(self.cgi("index", token="")[0], 200)

    def test_api_needs_the_token(self):
        status, headers, body = self.cgi("api-list")
        self.assertEqual(status, 401)
        self.assertFalse(any(h.startswith("WWW-Authenticate") for h in headers), "no sign-in prompt")
        self.assertEqual(self.cgi("api-search", q="x")[0], 401)
        self.assertEqual(self.cgi("api-create", song="23063")[0], 401)

    def test_token_as_a_url_parameter(self):
        self.assertEqual(self.cgi("api-list", token_param=TOKEN)[0], 200)
        self.assertEqual(self.cgi("api-list", token_param="nope")[0], 401)
        self.assertEqual(self.cgi("api-search", token_param=TOKEN, q="")[0], 400, "authorized, then the request is judged")
        self.assertEqual(self.cgi("api-create", token_param=TOKEN, song="")[0], 400)

    def test_a_page_opened_with_the_token_sets_a_cookie_that_works_after(self):
        status, headers, _ = self.cgi("index", token_param=TOKEN)
        self.assertEqual(status, 200)
        cookie = next(h for h in headers if h.startswith("Set-Cookie: gpsync_token="))
        self.assertIn("HttpOnly", cookie)
        value = cookie.split(": ", 1)[1].split(";")[0]
        self.assertEqual(self.cgi("index", cookie=value)[0], 200)
        self.assertEqual(self.cgi("index", cookie="gpsync_token=wrong")[0], 401)
        # the API sets no cookie
        self.assertFalse(any(h.startswith("Set-Cookie") for h in self.cgi("api-list", token_param=TOKEN)[1]))

    def test_pages_need_the_token_in_the_url_and_never_prompt(self):
        for page in ("index", "job", "start"):
            status, headers, _ = self.cgi(page, id=JOB)
            self.assertEqual(status, 401, page)
            self.assertFalse(any(h.startswith("WWW-Authenticate") for h in headers), page)

    def test_a_token_in_a_header_is_not_accepted(self):
        for header in ("Bearer " + TOKEN, self.basic("anyone", TOKEN), self.basic("", TOKEN)):
            for command in ("api-list", "index", "health"):
                if command == "health":
                    self.assertEqual(json.loads(self.cgi(command, auth=header)[2]), {"ok": True}, "no extra detail")
                else:
                    self.assertEqual(self.cgi(command, auth=header)[0], 401, (command, header[:6]))

    def test_a_token_with_a_colon_works_in_the_url(self):
        self.assertEqual(self.cgi("api-list", token="a:b:c", token_param="a:b:c")[0], 200)

    def test_file_link_carries_its_own_signature_not_the_token(self):
        status, _, body = self.cgi("api-job", token_param=TOKEN, id=JOB)
        v = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("&sig=", v["file_url"])
        self.assertNotIn(TOKEN, v["file_url"] + v["open_url"])
        sig = v["file_url"].split("&sig=")[1]
        status, _, body = self.cgi("api-file", id=JOB, sig=sig)
        self.assertEqual((status, body), (200, b"GPDATA"))
        self.assertEqual(self.cgi("api-file", id=JOB, sig="0" * 32)[0], 401)
        self.assertEqual(self.cgi("api-file", id=JOB)[0], 401)
        self.assertEqual(self.cgi("api-file", token="other", id=JOB, sig=sig)[0], 401, "a new token voids old links")
        self.assertEqual(self.cgi("api-job", id=JOB, sig=sig)[0], 401, "the signature opens the file only")

    def test_no_signature_without_a_token(self):
        _, _, body = self.cgi("api-job", token="", id=JOB)
        self.assertNotIn("sig=", json.loads(body)["file_url"])

    def test_health_stays_open_but_says_little(self):
        status, _, body = self.cgi("health")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertIn("release", json.loads(self.cgi("health", token_param=TOKEN)[2]))


if __name__ == "__main__":
    unittest.main()
