#!/usr/bin/env python3
"""Offline tests for /openapi.json, the Link header and GET /api/get: python3 tests/test_openapi.py
Commands run through tests/cgi_adapter.py (v_* parameters in the environment). A local server stands in for the source's
search, and the fake upstream stands in for the sync."""
import json, os, re, subprocess, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, UPSTREAM_DIR=str(ROOT / "tests" / "fake_upstream"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import gpsync  # noqa: E402
from test_progress import HEAD, DOWNLOAD  # noqa: E402

SPEC = json.loads((ROOT / "app" / "openapi.json").read_text())
from server import ROUTES  # noqa: E402
PAGES = {("GET", "/"), ("POST", "/get"), ("GET", "/get"), ("GET", "/job"), ("POST", "/delete"), ("POST", "/delete-all")}   # the HTML pages are not part of the API
LINK = '</openapi.json>; rel="service-desc"; type="application/json"'
SEARCH_LINK = '</opensearch.xml>; rel="search"; type="application/opensearchdescription+xml"'
TOKEN = "s3cret-token"
HITS = [{"songId": 320, "artist": "Johann Sebastian Bach", "title": "Bourre In E Minor"},
        {"songId": 321, "artist": "Johann Sebastian Bach", "title": "Bourre In E Minor (live)"}]


class Source(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(HITS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def pointer(doc, ptr):
    """RFC 6901. Returns (found, value)."""
    cur = doc
    for part in [p.replace("~1", "/").replace("~0", "~") for p in ptr.split("/")[1:]]:
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Source)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def cgi(self, command, token="", token_param=None, data=None, public_base="http://h", **params):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HTTP_", "v_", "AUTH_"))}
        env.update(DATA_DIR=data or DATA, UPSTREAM_DIR=str(ROOT / "tests" / "fake_upstream"), UPSTREAM_PYTHON=sys.executable,
                   SOURCE_URL=f"http://127.0.0.1:{self.srv.server_port}", PUBLIC_BASE_URL=public_base)
        if token:
            env["AUTH_TOKEN"] = token
        if token_param is not None:
            params["token"] = token_param
        env.update({f"v_{k}": v for k, v in params.items()})
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "cgi_adapter.py"), command], capture_output=True, env=env, timeout=30)
        head, _, body = p.stdout.partition(b"\n\n")
        lines = head.decode().split("\n")
        return int(lines[0].split()[1]), lines[1:], body

    def assertLink(self, fragment, headers, msg=None):
        """One Link line holds both relations: shell2http keeps only the last of several lines with the same name."""
        lines = [h for h in headers if h.lower().startswith("link:")]
        self.assertEqual(len(lines), 1, (msg, lines))
        self.assertIn(fragment, lines[0], msg)

    def wait_done(self, job_id, data=None, **auth):
        for _ in range(100):
            status, _, body = self.cgi("api-job", data=data, id=job_id, **auth)
            if json.loads(body)["status"] in ("done", "failed"):
                return
            time.sleep(0.1)


class Document(Base):
    def test_served_with_and_without_a_token(self):
        for token in ("", TOKEN):
            status, headers, body = self.cgi("openapi", token=token)   # no token sent
            self.assertEqual(status, 200, token)
            self.assertIn("Content-Type: application/json", headers)
            self.assertIn("Access-Control-Allow-Origin: *", headers)
            self.assertIn("Cache-Control: no-store", headers)
            self.assertEqual(json.loads(body), SPEC)

    def test_basics(self):
        self.assertTrue(SPEC["openapi"].startswith("3.1"))
        self.assertTrue(SPEC["info"]["title"] and SPEC["info"]["version"])
        self.assertNotIn("servers", SPEC)
        self.assertLess(len(json.dumps(SPEC)), 256 * 1024)
        self.assertNotIn(TOKEN, json.dumps(SPEC))
        self.assertEqual(SPEC["security"], [{"tokenQuery": []}])
        self.assertEqual(SPEC["components"]["securitySchemes"]["tokenQuery"], {"type": "apiKey", "in": "query", "name": "token"})
        self.assertNotIn("tokenBearer", SPEC["components"]["securitySchemes"], "a token works only in the query")
        self.assertNotIn("bearer", json.dumps(SPEC).lower())
        self.assertEqual(SPEC["paths"]["/healthz"]["get"]["security"], [])
        self.assertEqual(SPEC["paths"]["/openapi.json"]["get"]["security"], [])

    def test_routes_and_document_agree(self):
        routes = {(m, p) for m, p, _ in ROUTES} - PAGES
        documented = {(m.upper(), p) for p, ops in SPEC["paths"].items() for m in ops}
        self.assertEqual(routes, documented)
        self.assertIn(("GET", "/openapi.json"), routes)

    def test_refs_are_local_and_resolve(self):
        refs = []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "$ref":
                        refs.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(SPEC)
        self.assertTrue(refs)
        for ref in refs:
            self.assertTrue(ref.startswith("#/"), ref)
            found, _ = pointer(SPEC, ref[1:])
            self.assertTrue(found, ref)

    def test_extensions(self):
        for path, method in (("/api/get", "get"), ("/api/jobs", "post")):
            self.assertEqual(SPEC["paths"][path][method]["x-async-job"], {
                "statusLink": "status", "resultLink": "file", "state": "/status",
                "states": {"pending": ["queued"], "running": ["running"], "done": ["done"], "failed": ["failed"]},
                "progress": {"percent": "/progress/percent", "step": "/progress/step", "steps": "/progress/steps", "label": "/progress/name"},
                "error": "/error"})
        self.assertEqual(SPEC["components"]["parameters"]["SongText"]["name"], "q")
        self.assertNotIn("x-search-text", json.dumps(SPEC), "the search text is named by /opensearch.xml")
        self.assertEqual(SPEC["components"]["parameters"]["First"]["schema"]["default"], "1")
        self.assertNotIn("Format", SPEC["components"]["parameters"], "there is no format choice: every job makes everything that applies")


def operation(spec, operation_id):
    return next(op for ops in spec["paths"].values() for op in ops.values() if op.get("operationId") == operation_id)


class JobLinks(unittest.TestCase):
    """The standard OpenAPI links say where a job's status and file are; x-async-job only names them."""

    def resolve(self, node):
        while isinstance(node, dict) and "$ref" in node:
            node = pointer(SPEC, node["$ref"][1:])[1]
        return node

    def test_every_job_response_links_to_status_and_file(self):
        for path, method in (("/api/get", "get"), ("/api/jobs", "post"), ("/api/job", "get")):
            for code in ("200", "202"):
                response = SPEC["paths"][path][method]["responses"].get(code)
                if not response or "content" not in response:
                    continue
                links = {k: self.resolve(v) for k, v in response["links"].items()}
                self.assertEqual(set(links), {"status", "file"}, (path, code))
                self.assertEqual(links["status"]["operationId"], "getJob")
                self.assertEqual(links["file"]["operationId"], "downloadJobFile")

    def test_the_names_in_x_async_job_exist_and_the_parameters_are_real(self):
        for path, method in (("/api/get", "get"), ("/api/jobs", "post")):
            ext = SPEC["paths"][path][method]["x-async-job"]
            links = SPEC["paths"][path][method]["responses"]["202"]["links"]
            for key in ("statusLink", "resultLink"):
                self.assertIn(ext[key], links)
                link = self.resolve(links[ext[key]])
                target = operation(SPEC, link["operationId"])
                names = {self.resolve(p)["name"] for p in target["parameters"]}
                self.assertTrue(set(link["parameters"]) <= names)
                self.assertEqual(link["parameters"]["id"], "$response.body#/id")


class OpenSearch(Base):
    def templates(self, **kw):
        status, headers, body = self.cgi("opensearch", **kw)
        self.assertEqual(status, 200)
        self.assertIn("Content-Type: application/opensearchdescription+xml", headers)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(body)
        ns = {"o": "http://a9.com/-/spec/opensearch/1.1/"}
        return headers, {u.get("type"): u.get("template") for u in root.findall("o:Url", ns)}

    def test_two_templates_for_the_same_search_text(self):
        _, found = self.templates(public_base="http://svc.test:8080")
        self.assertEqual(found["text/html"], "http://svc.test:8080/get?q={searchTerms}")
        self.assertEqual(found["application/json"], "http://svc.test:8080/api/get?q={searchTerms}&first=1")
        self.assertIn("/api/get", SPEC["paths"])
        self.assertEqual(SPEC["components"]["parameters"]["SongText"]["name"], "q")   # the parameter the template fills

    def test_it_follows_the_proxy_prefix_and_needs_no_token(self):
        _, found = self.templates(public_base="https://tools.example/gpsync", token=TOKEN)
        self.assertEqual(found["application/json"], "https://tools.example/gpsync/api/get?q={searchTerms}&first=1")

    def test_every_response_points_to_it(self):
        for got, headers, _ in (self.cgi("openapi"), self.cgi("api-list", token=TOKEN), self.cgi("index")):
            self.assertLink(SEARCH_LINK, headers)
        self.assertLink('</gpsync/opensearch.xml>; rel="search"; type="application/opensearchdescription+xml"',
                        self.cgi("openapi", public_base="https://tools.example/gpsync")[1])


class Choices(Base):
    """x-choices on the 409 must land on real fields of the answer, and the parameter it fills must exist."""

    def test_the_409_pointers_and_parameter_are_real(self):
        status, _, body = self.cgi("api-create", data=tempfile.mkdtemp(), q="johann sebastian bach bourre in e minor")
        self.assertEqual(status, 409)
        answer = json.loads(body)
        for path, method in (("/api/get", "get"), ("/api/jobs", "post")):
            ext = SPEC["paths"][path][method]["responses"]["409"]["x-choices"]
            found, items = pointer(answer, ext["items"])
            self.assertTrue(found and items)
            for item in items:
                for key in ("label", "detail", "value"):
                    self.assertTrue(pointer(item, ext[key])[0] and pointer(item, ext[key])[1], key)
            names = {SPEC["components"]["parameters"][p["$ref"].split("/")[-1]]["name"]
                     for p in SPEC["paths"][path][method].get("parameters", []) if "$ref" in p}
            if method == "get":
                self.assertTrue({ext["parameter"], ext["replaces"]} <= names)


class JobPointers(unittest.TestCase):
    """The x-async-job pointers must land on real fields of the job JSON that view() makes."""

    def make(self, jid, status, log="", **extra):
        d = Path(DATA) / "jobs" / jid
        (d / "out").mkdir(parents=True, exist_ok=True)
        (d / "log.txt").write_text(log)
        job = {"id": jid, "song": "5", "status": status, "created": "2026-01-01T00:00:00Z", **extra}
        if status != "queued":
            job["started"] = "2026-01-01T00:00:01Z"
        (d / "job.json").write_text(json.dumps(job))
        return gpsync.view(job)

    def setUp(self):
        self.ext = SPEC["paths"]["/api/get"]["get"]["x-async-job"]

    def state_of(self, v):
        found, state = pointer(v, self.ext["state"])
        self.assertTrue(found)
        return next(k for k, vals in self.ext["states"].items() if state in vals)

    def test_queued(self):
        v = self.make("5-queued", "queued")
        self.assertEqual(self.state_of(v), "pending")
        self.assertEqual(pointer(v, "/id"), (True, "5-queued"))   # what the status and file links use
        self.assertFalse(pointer(v, self.ext["error"])[0] and pointer(v, self.ext["error"])[1])
        self.assertFalse(pointer(v, self.ext["progress"]["percent"])[0])

    def test_running_with_and_without_percent(self):
        v = self.make("5-run-pct", "running", HEAD + DOWNLOAD)
        self.assertEqual(self.state_of(v), "running")
        found, pct = pointer(v, self.ext["progress"]["percent"])
        self.assertEqual((found, pct), (True, 61.6))
        self.assertEqual(pointer(v, self.ext["progress"]["step"]), (True, 4))
        self.assertEqual(pointer(v, self.ext["progress"]["steps"]), (True, 6))
        self.assertTrue(pointer(v, self.ext["progress"]["label"])[1])
        v = self.make("5-run-nopct", "running", "[1/6] Fetching song metadata...\n")
        self.assertEqual(pointer(v, self.ext["progress"]["percent"]), (True, None))
        self.assertEqual(pointer(v, self.ext["progress"]["step"]), (True, 1))

    def test_done(self):
        v = self.make("5-done", "done", HEAD, file="a_synced.gp", files=["a_synced.gp"], audio=True, finished="2026-01-01T00:01:00Z")
        self.assertEqual(self.state_of(v), "done")
        self.assertEqual(pointer(v, "/id"), (True, "5-done"))
        self.assertFalse(pointer(v, self.ext["error"])[1])

    def test_failed(self):
        v = self.make("5-failed", "failed", HEAD, error="boom", finished="2026-01-01T00:01:00Z")
        self.assertEqual(self.state_of(v), "failed")
        self.assertEqual(pointer(v, self.ext["error"]), (True, "boom"))


class LinkHeader(Base):
    def test_on_200_401_404_409(self):
        cases = {200: self.cgi("openapi"),
                 401: self.cgi("api-list", token=TOKEN),
                 404: self.cgi("api-job", id="1-nope"),
                 409: self.cgi("api-create", q="johann sebastian bach bourre in e minor", data=tempfile.mkdtemp())}
        for status, (got, headers, _) in cases.items():
            self.assertEqual(got, status)
            self.assertLink(LINK, headers, status)
            self.assertLink(SEARCH_LINK, headers, status)
        self.assertLink(LINK, self.cgi("index")[1], "pages too")

    def test_the_target_follows_the_proxy_prefix_and_never_names_a_host(self):
        for base, target in (("http://h", "/openapi.json"), ("https://tools.example", "/openapi.json"),
                             ("https://tools.example/gpsync", "/gpsync/openapi.json"),
                             ("http://localhost:8080/a/b/", "/a/b/openapi.json"), ("", "/openapi.json")):
            link = f'<{target}>; rel="service-desc"; type="application/json"'
            for status, (got, headers, _) in {200: self.cgi("openapi", public_base=base),
                                              401: self.cgi("api-list", token=TOKEN, public_base=base)}.items():
                self.assertEqual(got, status)
                self.assertLink(link, headers, (base, status))

    def test_a_browser_page_can_read_the_link_even_from_a_401(self):
        # Without these a web page can not read Link (nor the 401 at all), so it can not find the description.
        expose = "Access-Control-Expose-Headers: Link, Content-Disposition"
        for status, (got, headers, _) in {200: self.cgi("openapi"), 401: self.cgi("api-list", token=TOKEN),
                                          404: self.cgi("api-job", id="1-nope")}.items():
            self.assertEqual(got, status)
            self.assertIn("Access-Control-Allow-Origin: *", headers, status)
            self.assertIn(expose, headers, status)


class Fetch(Base):
    def setUp(self):
        self.data = tempfile.mkdtemp()   # a fresh job store, so no job is reused from another test

    def test_routes_share_a_handler(self):
        self.assertEqual({(m, p): c for m, p, c in ROUTES}[("GET", "/api/get")], {(m, p): c for m, p, c in ROUTES}[("POST", "/api/jobs")])

    def test_start_then_reuse(self):
        status, _, body = self.cgi("api-create", data=self.data, q="23063")
        first = json.loads(body)
        self.assertEqual(status, 202)
        self.assertNotIn("kind", first)
        for key in ("id", "song", "status", "status_url", "page_url", "created"):
            self.assertIn(key, first)
        status, _, body = self.cgi("api-create", data=self.data, q="23063")   # a repeated or prefetched GET
        self.assertEqual((status, json.loads(body)["id"]), (200, first["id"]))
        self.wait_done(first["id"], self.data)
        status, _, body = self.cgi("api-create", data=self.data, song="23063")
        self.assertEqual((status, json.loads(body)["id"]), (200, first["id"]), "the finished job is reused too")

    def test_search_text_needs_first(self):
        status, _, body = self.cgi("api-create", data=self.data, q="johann sebastian bach bourre in e minor")
        self.assertEqual(status, 409)
        self.assertEqual([m["song"] for m in json.loads(body)["matches"]], ["320", "321"])
        self.assertEqual(self.cgi("api-list", data=self.data)[0], 200)
        self.assertEqual(json.loads(self.cgi("api-list", data=self.data)[2]), [], "nothing was started")
        status, _, body = self.cgi("api-create", data=self.data, q="johann sebastian bach bourre in e minor", first="1")
        job = json.loads(body)
        self.assertEqual((status, job["song"], job["matched"]["artist"]), (202, "320", "Johann Sebastian Bach"))
        self.wait_done(job["id"], self.data)

    def test_bad_requests(self):
        self.assertEqual(self.cgi("api-create", data=self.data)[0], 400)

    def test_token(self):
        self.assertEqual(self.cgi("api-create", data=self.data, token=TOKEN, q="23063")[0], 401)
        status, _, body = self.cgi("api-create", data=self.data, token=TOKEN, token_param=TOKEN, q="23063")
        self.assertEqual(status, 202)
        self.wait_done(json.loads(body)["id"], self.data, token=TOKEN, token_param=TOKEN)


if __name__ == "__main__":
    unittest.main()
