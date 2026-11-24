#!/usr/bin/env python3
"""Request handlers and job runner for the Guitar Pro sync service.

server.py serves HTTP and calls `dispatch(command, params, headers)` for each request. The commands (COMMANDS):
    index | start | job | delete | retry-stems | delete-all | api-delete | api-retry-stems | api-create | api-list | api-job | api-file | api-search | health
    | openapi | opensearch
From a shell this file also runs the housekeeping and background commands:
    recover          fail jobs left unfinished by a previous container
    sweep            delete expired files and jobs (FILE_TTL_MINUTES, JOB_TTL_MINUTES)
    run <job-id>     the detached runner that executes mpak_job.py for one job

Stdlib only. Job state lives in $DATA_DIR/jobs/<id>/{job.json,log.txt,out/}.
"""
import calendar
import fcntl
import shutil
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urlparse

import sources
import stemdeck as stemdeck_module

DATA = Path(os.environ.get("DATA_DIR", "/data"))
JOBS = DATA / "jobs"
UPSTREAM = Path(os.environ.get("UPSTREAM_DIR", "/opt/upstream"))
PYTHON = os.environ.get("UPSTREAM_PYTHON", "/opt/venv/bin/python")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Where a proxy mounts the service ("/gpsync" for a public address ending in /gpsync), without scheme or host.
PUBLIC_PREFIX = urlparse(PUBLIC_BASE).path.rstrip("/")
OPEN_TEMPLATE = os.environ.get("OPEN_LINK_TEMPLATE", "")
# The name of the app the link opens; the button reads "Open in <name>" ("Open" when there is no name).
OPEN_APP_NAME = os.environ.get("OPEN_LINK_LABEL", "").strip()
OPEN_LABEL = f"Open in {OPEN_APP_NAME}" if OPEN_APP_NAME else "Open"
# MPAK jobs wait for StemDeck to separate the song (minutes on a CPU), so they get their own limit.
MPAK_TIMEOUT = int(os.environ.get("MPAK_JOB_TIMEOUT", "3600"))
STEMDECK_URL = stemdeck_module.configured_url()
STEMS_OFF = os.environ.get("STEMDECK_ENABLED", "1").strip() == "0"
# Optional access token. Empty = open, as before. When set, every request must present it (see authorized()).
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
COOKIE = "gpsync_token"

ID_RE = re.compile(r"^\d{1,9}-[0-9a-f]{6}$")
STEPS = 4   # mpak_job.py prints [n/4]


# --------------------------------------------------------------------------- #
# Request context and response helpers
# --------------------------------------------------------------------------- #
# server.py runs each request on its own thread. `dispatch` fills this in; the handlers read the request with param() and
# header() and answer with emit(), which stores the response here for the server to send.
_ctx = threading.local()


def download_label(name):
    """A download button's text: the file type, not the file name."""
    ext = name.rsplit(".", 1)[-1].upper() if "." in name else ""
    if ext == "MPAK" and "(stems)" in name:
        return "MPAK with Stems"
    if ext == "MPAK" and "(full band + backing)" in name:
        return "MPAK with Full Band + Backing"
    return ext or name


def emit(body, status=200, ctype="application/json", headers=()):
    """Set the response: `body` is a dict/list (JSON), text, bytes, or a Path (a file, streamed by the server).
    `headers` are "Name: value" lines."""
    if isinstance(body, (dict, list)):
        body = json.dumps(body, indent=2)
    if isinstance(body, str):
        body = body.encode()
    length = body.stat().st_size if isinstance(body, Path) else len(body)
    # RFC 8631: every response says where the API description is, so a client that gets a 401 can still find it. The
    # target is a path (never a host) that includes the proxy prefix, so it stays on the origin the client used.
    lines = [f"Content-Type: {ctype}", f"Content-Length: {length}",
             # Both relations on one Link line.
             f'Link: <{PUBLIC_PREFIX}/openapi.json>; rel="service-desc"; type="application/json", '
             f'<{PUBLIC_PREFIX}/opensearch.xml>; rel="search"; type="application/opensearchdescription+xml"',
             *headers, *getattr(_ctx, "set_cookie", ())]
    _ctx.response = (status, lines, body)


# A browser page (the app's web build) can read a response header only when it is listed here: Link finds the API
# description, Content-Disposition names a downloaded file.
CORS = ["Access-Control-Allow-Origin: *", "Access-Control-Expose-Headers: Link, Content-Disposition"]


def api_headers():
    return [*CORS, "Cache-Control: no-store"]


def api_error(status, message):
    emit({"error": message}, status, headers=api_headers())


def public_url(path):
    base = PUBLIC_BASE
    if not base:
        proto = header("X-Forwarded-Proto") or "http"
        base = f"{proto}://{header('Host') or 'localhost'}"
    return base + path


def file_sig(job_id):
    """Signature that lets one job's file be fetched without the token (the link in `file_url` / `open_url`, which another
    app opens with no way to send a header). It covers that file only and changes when the token does."""
    return hmac.new(AUTH_TOKEN.encode(), f"file:{job_id}".encode(), hashlib.sha256).hexdigest()[:32]


def presented_tokens():
    """Every token the request carries: `token=<token>` in the URL (or form), and the cookie a page sets after a valid
    `?token=`. A token in a header (Authorization) is not accepted."""
    found = [param("token")]
    for part in header("Cookie").split(";"):
        name, _, val = part.strip().partition("=")
        if name == COOKIE:
            found.append(unquote(val))
    return [t for t in found if t]


def authorized(command):
    if not AUTH_TOKEN:
        return True
    if any(hmac.compare_digest(t.encode(), AUTH_TOKEN.encode()) for t in presented_tokens()):
        return True
    if command == "api-file":
        job_id = param("id")
        return bool(ID_RE.match(job_id)) and hmac.compare_digest(param("sig").encode(), file_sig(job_id).encode())
    return False


def deny(command):
    """401. The token goes in the URL (`token=<token>`); a page needs it once, then keeps a cookie."""
    if command.startswith("api-"):
        return emit({"error": "A token is required: add token=<token> to the request."}, 401,
                    headers=["Cache-Control: no-store", *CORS])
    emit("Open the page with ?token=<token>.", 401, "text/plain; charset=utf-8", ["Cache-Control: no-store"])


def param(name):
    """A query-string or form value of the current request, stripped; empty when absent."""
    return getattr(_ctx, "params", {}).get(name, "").strip()


def header(name):
    """A header of the current request (any case); empty when absent."""
    return getattr(_ctx, "headers", {}).get(name.lower(), "")


# --------------------------------------------------------------------------- #
# Job store
# --------------------------------------------------------------------------- #
def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def seconds_between(start, end=None):
    """Whole seconds between two job timestamps (end defaults to now)."""
    if not start:
        return None
    parse = lambda t: calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%SZ"))
    return max(0, parse(end or now()) - parse(start))


def fmt_duration(sec):
    if sec is None:
        return ""
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"


def normalize_song(raw):
    """A song ID or song page URL -> numeric ID string, else None."""
    return sources.source().parse(raw)


def search_songs(query, size=10):
    """The source's own search: [{song, artist, title, url}], best match first."""
    return sources.source().search(query, size)


class NeedsConfirmation(Exception):
    """Search text matched songs, and the caller has not said which one (or that the first will do)."""

    def __init__(self, query, matches):
        super().__init__(query)
        self.query, self.matches = query, matches


def resolve_song(raw, first=False):
    """What the person typed -> (song id, matched or None, error). An ID or URL is used as is. Search text is looked up on
    the source, and no job starts from it until the caller has seen the artist and title: NeedsConfirmation carries the
    matches, and `first` takes the first one without asking."""
    raw = raw.strip()
    song = normalize_song(raw)
    if song:
        return song, None, None
    if not raw:
        return None, None, "Provide a song ID, URL or search text (e.g. 23063 or johann sebastian bach bourre in e minor)."
    try:
        hits = search_songs(raw, 5)
    except (urllib.error.URLError, OSError, ValueError, sources.SourceError) as e:
        return None, None, f"Search failed: {getattr(e, 'reason', e)}"
    if not hits:
        return None, None, f"No song matches \"{raw}\"."
    if not first:
        raise NeedsConfirmation(raw, hits)
    return hits[0]["song"], {"query": raw, "artist": hits[0]["artist"], "title": hits[0]["title"]}, None


def delete_job(job_id):
    """Remove a finished or failed job with its log and files. Returns an error message, or None when it is gone."""
    job = read_job(job_id)
    if not job:
        return "No such job"
    if job["status"] in ("queued", "running"):
        return "The job is still running; wait for it to finish."
    shutil.rmtree(JOBS / job["id"], ignore_errors=True)
    return None


def read_job(job_id):
    if not ID_RE.match(job_id or ""):
        return None
    try:
        return json.loads((JOBS / job_id / "job.json").read_text())
    except (OSError, ValueError):
        return None


def write_job(job):
    d = JOBS / job["id"]
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job))
    os.replace(tmp, d / "job.json")


def all_jobs():
    jobs = []
    if JOBS.is_dir():
        for d in JOBS.iterdir():
            j = read_job(d.name)
            if j:
                jobs.append(j)
    return sorted(jobs, key=lambda j: j["created"], reverse=True)


def code_version():
    """Identifies the code that makes a job's files: this service's own source plus the installed upstream release. A finished
    job is reused only when it was made by the same version, so after an update the same song runs again."""
    h = hashlib.sha1()
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        h.update(f.name.encode() + f.read_bytes())
    try:
        h.update((UPSTREAM / ".release").read_bytes())
    except OSError:
        pass
    return h.hexdigest()[:10]


_create_lock = threading.Lock()


def create_job(song, force=False, matched=None, name=None):
    """Return (job, created). Reuses a queued/running job, or a done one made by the current code, for the same song.
    Serialized, so two requests for the same song at once start one job."""
    with _create_lock:
        return _create_job(song, force, matched, name)


def _create_job(song, force, matched, name=None):
    version = code_version()
    if not force:
        for j in all_jobs():
            if j["song"] == song and j["status"] in ("queued", "running", "done"):
                if j["status"] == "done" and (j.get("code") != version
                                              or not (JOBS / j["id"] / "out" / j.get("file", "")).is_file()):
                    continue
                return j, False
    job_id = f"{song}-{secrets.token_hex(3)}"
    (JOBS / job_id / "out").mkdir(parents=True)
    job = {"id": job_id, "song": song, "status": "queued", "created": now(), "code": version}
    if name:
        job["name"] = name  # a retry keeps the failed job's "artist - title" until the new log has its own
    if matched:
        job["matched"] = matched  # what search text was resolved to, so the caller can check it
    write_job(job)
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "run", job_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    threading.Thread(target=proc.wait, daemon=True).start()   # the server lives on: reap the runner when it exits
    return job, True


def read_log(job_id):
    try:
        return (JOBS / job_id / "log.txt").read_text(errors="replace")
    except OSError:
        return ""


def parse_log(text):
    steps = re.findall(r"\[(\d)/\d\]", text)
    m = re.search(r"Song: (.+)", text)
    return {
        "step": int(steps[-1]) if steps else 0,
        "name": m.group(1).strip() if m else None,
    }


def _plain(s):
    """A step title or stage without its parenthetical notes, e.g. "(untrimmed)" or "(no audio)"."""
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


def progress_of(text):
    """What a running job is doing now: {step, steps, name, stage, percent}, read from the end of its log. `name` is the step's
    title; `stage` is the latest thing it printed (StemDeck's "Separating 56%", a download's "[download] 61.6% ..."), with its
    percentage taken out into `percent` (null when the line has none)."""
    heads = list(re.finditer(r"\[(\d)/(\d)\] ([^\n]*)", text))
    if not heads:
        return None
    last = heads[-1]
    tail = [x.strip() for x in re.split(r"[\r\n]+", text[last.end():]) if x.strip()]
    line = tail[-1] if tail else ""
    pct = re.findall(r"(\d+(?:\.\d+)?)%", line)
    name = _plain(last.group(3)).rstrip(".")
    if line.startswith("[download]") or not line:
        stage = name  # a tool's own progress line: the step's title says what it is
    else:
        stage = _plain(re.sub(r"\s*\d+(?:\.\d+)?%\s*", " ", line)) or name
    return {"step": int(last.group(1)), "steps": int(last.group(2)), "name": name, "stage": stage,
            "percent": float(pct[0]) if pct else None}


TIERS = (  # an `all` job's outputs, worst to best: (tier, title, what it is, file-name marker)
    ("good", "Good", "Guitar Pro file with the full band audio embedded", "_synced.gp"),
    ("better", "Better", "Music Pack with the full band and backing as two recordings", "(full band + backing).mpak"),
    ("best", "Best", "Music Pack with the stems separated by StemDeck", "(stems).mpak"),
)


def skip_reasons(text):
    """Why an `all` job left an output out, from its log: {tier: reason}."""
    why = {}
    m = re.findall(r"stems skipped: ([^\n]*)", text)
    if m:
        why["best"] = m[-1].strip()   # the latest: a retry adds a newer one
    m = re.search(r"WARNING: (no backing video[^\n]*|backing audio download failed[^\n]*)", text)
    if m:
        why["better"] = m.group(1).strip().rstrip(".")
    m = re.search(r"WARNING: could not make the Guitar Pro file: ([^\n]*)", text)
    if m:
        why["good"] = m.group(1).strip()
    return why


def source_url(song):
    """The source's page for a song, or None when the source's address is unknown."""
    try:
        return sources.source().song_url(song)
    except sources.SourceError:
        return None


def view(job):
    """Public representation of a job (API and pages)."""
    info = parse_log(read_log(job["id"]))
    v = {
        "id": job["id"], "song": job["song"], "status": job["status"],
        "name": job.get("name") or info["name"],
        "step": info["step"], "steps": STEPS,
        "created": job["created"], "started": job.get("started"), "finished": job.get("finished"),
        "error": job.get("error"),
        # Run time (elapsed so far while running) and time spent waiting in the queue.
        "duration_seconds": seconds_between(job.get("started"), job.get("finished")),
        "queued_seconds": seconds_between(job["created"], job.get("started")),
        "status_url": public_url(f"/api/job?id={job['id']}"),
        "page_url": public_url(f"/job?id={job['id']}"),
    }
    src = source_url(job["song"])
    if src:
        v["source_url"] = src
    if job.get("matched"):
        v["matched"] = job["matched"]
    if job["status"] == "running":
        v["progress"] = progress_of(read_log(job["id"]))
    if job["status"] == "done" and job.get("purged"):
        v["purged"] = job["purged"]   # the files were deleted (FILE_TTL_MINUTES); start the song again to get them
    elif job["status"] == "done":
        def url_of(name, first):
            u = public_url(f"/api/job/file?id={job['id']}")
            if not first:
                u += f"&name={quote(name, safe='')}"
            return u + (f"&sig={file_sig(job['id'])}" if AUTH_TOKEN else "")
        names = job.get("files") or [job.get("file")]
        file_url = url_of(names[0], True)
        v.update(file=job.get("file"), file_url=file_url, audio=job.get("audio", True))
        # Every file of the job; the first is the main one (`file`, `file_url`).
        v["files"] = [{"name": n, "file_url": url_of(n, i == 0)} for i, n in enumerate(names)]
        # The three outputs in good/better/best order, each available or not, with the reason when it is not.
        why = skip_reasons(read_log(job["id"]))
        by_name = {f["name"]: f for f in v["files"]}
        v["outputs"] = []
        for tier, title, what, marker in TIERS:
            f = next((f for n, f in by_name.items() if n.endswith(marker)), None)
            o = {"tier": tier, "title": title, "description": what, "available": bool(f)}
            if f:
                o.update(name=f["name"], file_url=f["file_url"])
            else:
                o["reason"] = why.get(tier) or "not made"
            v["outputs"].append(o)
        v["can_retry_stems"] = can_retry_stems(job)
        if OPEN_TEMPLATE:
            v["open_url"] = (OPEN_TEMPLATE.replace("{url_raw}", file_url)
                             .replace("{url}", quote(file_url, safe="")))
    return v


# --------------------------------------------------------------------------- #
# Runner (detached; one sync at a time)
# --------------------------------------------------------------------------- #
def proxy_env(environ=None):
    """Environment variables that send a job's YouTube traffic through YOUTUBE_PROXY_URL (a residential proxy, since YouTube
    blocks datacenter addresses). yt-dlp and requests read HTTP(S)_PROXY. The tab source and StemDeck are listed in NO_PROXY so
    they are reached directly. Empty when no proxy is set."""
    environ = os.environ if environ is None else environ
    proxy = environ.get("YOUTUBE_PROXY_URL", "").strip()
    if not proxy:
        return {}
    direct = ["localhost", "127.0.0.1", "::1"]
    try:
        direct.append(urlparse(sources.SOURCES["default"].base_url).hostname or "")
    except sources.SourceError:
        pass
    direct.append(urlparse(environ.get("STEMDECK_URL", "") or "http://stemdeck:8000").hostname or "")
    no_proxy = ",".join(dict.fromkeys(h for h in direct if h))
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": no_proxy, "no_proxy": no_proxy}


def run(job_id):
    job = read_job(job_id)
    if not job:
        return
    d = JOBS / job_id
    # The job (yt-dlp, ffmpeg, upstream code) has no use for the access token.
    env = {**{k: v for k, v in os.environ.items() if k != "AUTH_TOKEN"}, "PYTHONUNBUFFERED": "1", **proxy_env()}
    with open(DATA / "sync.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # queue behind any running sync
        job.update(status="running", started=now())
        write_job(job)
        error = None
        # The child lets go of the lock (it shares this descriptor) once the downloads are done, so the
        # minutes spent in StemDeck do not hold up other jobs. It stops itself a little before the timeout
        # so it can cancel its StemDeck job.
        cmd = [PYTHON, str(Path(__file__).resolve().with_name("mpak_job.py")), "--song", job["song"]]
        env.update(GPSYNC_LOCK_FD=str(lock.fileno()), GPSYNC_DEADLINE=str(time.time() + MPAK_TIMEOUT - 30))
        limit, pass_fds = MPAK_TIMEOUT, (lock.fileno(),)
        with open(d / "log.txt", "ab", buffering=0) as log:
            try:
                rc = subprocess.run(
                    cmd, cwd=d / "out", stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    env=env, timeout=limit, pass_fds=pass_fds,
                ).returncode
            except subprocess.TimeoutExpired:
                rc, error = None, f"Timed out after {limit}s"
    text = read_log(job_id)
    info = parse_log(text)
    files = output_files(d / "out")
    job["finished"] = now()
    job["name"] = info["name"] or job.get("name")
    if rc == 0 and files:
        # A job fails outright when the audio is missing, so a finished one always has it.
        job.update(status="done", file=files[0], audio=True, files=files)
    else:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        job.update(status="failed", error=error or (lines[-1] if lines else f"exit code {rc}"))
    write_job(job)


def output_files(out_dir):
    """Every output that applies, best first (the first is the main file)."""
    packs = sorted(out_dir.glob("*.mpak"), key=lambda p: p.name)
    rank = lambda p: (0 if "(stems)" in p.name else 1, p.name)
    return [p.name for p in sorted(packs, key=rank)] + [p.name for p in sorted(out_dir.glob("*_synced.gp"))]


def run_stems(job_id):
    """The detached runner for "Retry stems": the separation and packing of a done job again, from the audio it kept. It takes no
    sync lock (nothing is downloaded). The job is done again whatever happens: its other outputs are still there, and a failed
    separation is a warning in the log, as in the first run."""
    job = read_job(job_id)
    if not job:
        return
    d = JOBS / job_id
    env = {**{k: v for k, v in os.environ.items() if k != "AUTH_TOKEN"}, "PYTHONUNBUFFERED": "1",
           "GPSYNC_DEADLINE": str(time.time() + MPAK_TIMEOUT - 30)}
    with open(d / "log.txt", "ab", buffering=0) as log:
        log.write(b"\n--- Retrying the stems ---\n")
        try:
            subprocess.run([PYTHON, str(Path(__file__).resolve().with_name("mpak_job.py")), "--resume"], cwd=d / "out",
                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=MPAK_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.write(f"WARNING: stems skipped: Timed out after {MPAK_TIMEOUT}s\n".encode())
    files = output_files(d / "out")
    job.update(status="done", finished=now(), files=files, file=files[0] if files else job.get("file"), error=None)
    write_job(job)


def can_retry_stems(job):
    """A done job whose separation failed but whose audio is still kept, with StemDeck turned on."""
    if job["status"] != "done" or job.get("purged") or not STEMDECK_URL:
        return False
    out = JOBS / job["id"] / "out"
    return (out / "work" / "resume.json").is_file() and not any("(stems).mpak" in n for n in job.get("files") or [])


def retry_stems(job_id):
    """Start the stems again for a job. Returns an error message, or None when it started."""
    with _create_lock:   # two clicks start one retry
        job = read_job(job_id)
        if not job:
            return "No such job"
        if not can_retry_stems(job):
            return "This job's stems cannot be retried" if job["status"] == "done" else f"Job is {job['status']}"
        job.update(status="running", started=now(), finished=None)
        write_job(job)
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "stems", job_id],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    return None


def recover():
    for j in all_jobs():
        if j["status"] in ("queued", "running"):
            j.update(status="failed", error="Interrupted by a service restart", finished=now())
            write_job(j)


def sweep():
    """Housekeeping, run at start and every few minutes. Output files (the .gp and .mpak, the bulk of the storage) of a done job are
    deleted FILE_TTL_MINUTES after it finished; the job record and log stay and say `purged`. Whole job folders go after JOB_TTL_MINUTES.
    A purged job is not reused: asking for the song again runs it again. 0 disables either limit."""
    file_ttl = float(os.environ.get("FILE_TTL_MINUTES", "0") or 0) * 60
    job_ttl = float(os.environ.get("JOB_TTL_MINUTES", "0") or 0) * 60
    for j in all_jobs():
        if j["status"] in ("queued", "running"):
            continue
        age = seconds_between(j.get("finished") or j["created"])
        if job_ttl and age > job_ttl:
            shutil.rmtree(JOBS / j["id"], ignore_errors=True)
        elif file_ttl and age > file_ttl and not j.get("purged"):   # failed jobs can leave temp files too
            out = JOBS / j["id"] / "out"
            for f in out.iterdir() if out.is_dir() else ():
                f.unlink() if f.is_file() or f.is_symlink() else shutil.rmtree(f, ignore_errors=True)
            if j["status"] == "done":
                j["purged"] = now()
                write_job(j)


# --------------------------------------------------------------------------- #
# API handlers
# --------------------------------------------------------------------------- #
def start_from_params():
    song, matched, err = resolve_song(param("song") or param("q"), param("first") in ("1", "true"))
    if err:
        return None, None, err
    old = read_job(param("retry"))   # set by the Retry button: the failed job this one replaces
    name = old.get("name") if old and old["status"] == "failed" and old["song"] == song else None
    job, created = create_job(song, force=param("force") in ("1", "true"), matched=matched, name=name)
    return job, created, None


def api_create():
    try:
        job, created, err = start_from_params()
    except NeedsConfirmation as n:
        return emit({"error": "Search text matched these songs; nothing was started. Send the chosen song's ID as song=<id>, "
                              "or repeat the request with first=1 to take the first result.",
                     "query": n.query, "matches": n.matches}, 409, headers=api_headers())
    if err:
        return api_error(400, err)
    emit(view(job), 202 if created else 200, headers=api_headers())


def api_search():
    q = param("q")
    if not q:
        return api_error(400, "Provide q=<search text>.")
    try:
        size = min(max(int(param("size") or 10), 1), 50)
        emit(search_songs(q, size), headers=api_headers())
    except ValueError:
        api_error(400, "size must be a number.")
    except (urllib.error.URLError, OSError, sources.SourceError) as e:
        api_error(502, f"Search failed: {getattr(e, 'reason', e)}")


def api_list():
    emit([view(j) for j in all_jobs()[:50]], headers=api_headers())


def api_job():
    job = read_job(param("id"))
    if not job:
        return api_error(404, "No such job")
    v = view(job)
    if param("log") in ("1", "true"):
        v["log"] = read_log(job["id"])
    emit(v, headers=api_headers())


def api_delete():
    err = delete_job(param("id"))
    if err:
        return api_error(404 if err == "No such job" else 409, err)
    emit({"deleted": param("id")}, headers=api_headers())


def api_retry_stems():
    err = retry_stems(param("id"))
    if err:
        return api_error(404 if err == "No such job" else 409, err)
    emit(view(read_job(param("id"))), 202, headers=api_headers())


def api_file():
    job = read_job(param("id"))
    if not job:
        return api_error(404, "No such job")
    if job["status"] != "done":
        return api_error(409, f"Job is {job['status']}; no file available")
    if job.get("purged"):
        return api_error(410, "The files of this job were deleted; start the song again")
    name = param("name") or job.get("file", "")
    if name not in (job.get("files") or [job.get("file")]):   # only files the job produced
        return api_error(404, "No such file in this job")
    path = JOBS / job["id"] / "out" / name
    if job["status"] != "done" or not path.is_file():
        return api_error(409, f"Job is {job['status']}; no file available")
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", path.name)
    emit(path, 200, "application/octet-stream", api_headers() + [
        f"Content-Disposition: attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(path.name)}"])


def openapi():
    """The API description (app/openapi.json). Open without a token, and it holds no secret and no address."""
    emit((Path(__file__).resolve().parent / "openapi.json").read_bytes(), headers=api_headers())


def opensearch():
    """The OpenSearch description (RFC-less de facto standard: browsers read it to add a search engine). Two result types
    for the same search text: the web page for a person, and the API address that starts a job for a program. Open without
    a token; it names the service's own address and nothing else."""
    esc_attr = lambda s: html.escape(s, quote=True)
    page_url = public_url("/get?q={searchTerms}")
    api_url = public_url("/api/get?q={searchTerms}&first=1")
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n'
            '  <ShortName>Guitar Pro sync</ShortName>\n'
            '  <Description>Search a tab source and make a synced Guitar Pro file or a Music Pack.</Description>\n'
            '  <InputEncoding>UTF-8</InputEncoding>\n'
            f'  <Url type="text/html" method="get" template="{esc_attr(page_url)}"/>\n'
            f'  <Url type="application/json" method="get" template="{esc_attr(api_url)}"/>\n'
            '</OpenSearchDescription>\n')
    emit(body, headers=api_headers(), ctype="application/opensearchdescription+xml")


def health():
    if not authorized("health"):  # liveness stays open for the container healthcheck, but says nothing else
        return emit({"ok": True}, headers=api_headers())
    release = (UPSTREAM / ".release").read_text().strip() if (UPSTREAM / ".release").exists() else None
    emit({"ok": True, "release": release, "stems": bool(STEMDECK_URL)}, headers=api_headers())


# --------------------------------------------------------------------------- #
# Web pages
# --------------------------------------------------------------------------- #
GH_LINK = ('<a class="gh" href="https://github.com/cgarst/guitar-tab-sync-service" target="_blank" rel="noopener" '
           'aria-label="GitHub repository" title="GitHub"><svg viewBox="0 0 16 16" width="18" height="18" fill="currentColor" aria-hidden="true">'
           '<path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg></a>')

CSS = """
.gh{display:block;width:fit-content;margin:32px auto 0;color:var(--mute);opacity:.6}.gh:hover{opacity:1;color:var(--fg)}
:root{--bg:#f4f4f4;--surface:#fff;--fg:#2b2b2b;--mute:#5c5c5c;--line:#dcdcdc;--hover:#ebebeb;
--accent:#ad4b14;--accent-hover:#94400f;--on-accent:#fff;--focus:#ad4b14;--link:#ad4b14;
--ok:#2f8a55;--err:#b04a3f;--run:#3b78c4;--wait:#707070;
--r:6px;--r-lg:10px;
--duration-quick:150ms;--duration-fast:250ms;--ease-smooth-out:cubic-bezier(.22,1,.36,1);--scale-small:.98}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f0f0f;--surface:#2a2a2a;--fg:#d2d2d2;--mute:#999;--line:#363636;
--hover:#2d2d2d;--accent:#bb5a1a;--accent-hover:#cc6a28;--on-accent:#fff;--focus:#e0894f;--link:#e0894f;
--ok:#5fbf85;--err:#e08a80;--run:#7aa7e0;--wait:#8a8a8a}}
:root[data-theme=dark]{--bg:#0f0f0f;--surface:#2a2a2a;--fg:#d2d2d2;--mute:#999;--line:#363636;
--hover:#2d2d2d;--accent:#bb5a1a;--accent-hover:#cc6a28;--on-accent:#fff;--focus:#e0894f;--link:#e0894f;
--ok:#5fbf85;--err:#e08a80;--run:#7aa7e0;--wait:#8a8a8a}
.themebar{display:flex;justify-content:flex-end;margin:-16px 0 8px}
.theme{display:inline-flex;border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:var(--surface)}
.theme button{display:grid;place-items:center;width:32px;height:28px;padding:0;border:0;background:none;color:var(--mute);cursor:pointer}
.theme button+button{border-left:1px solid var(--line)}
.theme button:hover{background:var(--hover);color:var(--fg)}
.theme button[aria-pressed=true]{background:var(--hover);color:var(--fg)}
.theme svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:680px;margin:0 auto;padding:40px 20px 64px}
h1{font-size:1.6rem;margin:0 0 6px}
h2{font-size:.8125rem;font-weight:600;margin:32px 0 8px;color:var(--mute)}
.loghead{display:flex;align-items:flex-end;justify-content:space-between;gap:8px;margin:32px 0 8px}.loghead h2{margin:0}
button.copy{min-height:32px;padding:4px 12px;font-size:.8125rem;background:var(--surface);color:var(--fg);border-color:var(--line)}
button.copy:hover{background:var(--hover);border-color:var(--mute)}
h3{font-size:1rem;margin:0;font-weight:600}
.sub,.meta{color:var(--mute);font-size:.875rem;font-variant-numeric:tabular-nums}
.sub{margin:0}.meta{margin:2px 0 0;overflow-wrap:anywhere}
a{color:var(--link);text-underline-offset:2px}
.back{margin:0 0 20px;font-size:.875rem}.back a{color:var(--mute)}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
form{display:flex;gap:8px;margin:20px 0}
input,select{flex:1;min-width:0;min-height:44px;padding:10px 12px;font:inherit;color:inherit;background:var(--surface);
border:1px solid var(--line);border-radius:var(--r);transition:border-color var(--duration-quick) var(--ease-smooth-out)}
input:hover{border-color:var(--mute)}input:focus-visible{outline:2px solid var(--focus);outline-offset:-1px;border-color:var(--focus)}
button,.btn{min-height:44px;padding:10px 18px;font:inherit;font-weight:600;line-height:1.2;color:var(--on-accent);
background:var(--accent);border:1px solid var(--accent);border-radius:var(--r);text-decoration:none;cursor:pointer;
display:inline-flex;align-items:center;justify-content:center;white-space:nowrap;
transition:background-color var(--duration-quick) var(--ease-smooth-out),border-color var(--duration-quick) var(--ease-smooth-out),transform var(--duration-quick) var(--ease-smooth-out)}
button:hover,.btn:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
button:active,.btn:active{transform:scale(var(--scale-small))}
.btn.alt{background:var(--surface);color:var(--fg);border-color:var(--line)}
.btn.alt:hover{background:var(--hover);border-color:var(--mute)}
.btn.danger{background:transparent;color:var(--err);border-color:transparent}
.btn.danger:hover{background:var(--hover);border-color:transparent}
.btn.small{min-height:32px;padding:4px 12px;font-size:.8125rem}
.toggle{position:absolute;opacity:0;pointer-events:none}.edit{cursor:pointer}
.recent-head{display:flex;justify-content:space-between;align-items:center;margin:32px 0 8px}.recent-head h2{margin:0}
.del{display:none}#edit:checked~.del{display:flex}#edit:checked~ul .del{display:flex}
.edit .off,#edit:checked~.recent-head .on{display:none}#edit:checked~.recent-head .off{display:inline}
#edit:focus-visible~.recent-head .edit{outline:2px solid var(--focus);outline-offset:2px}
.row-end{display:flex;align-items:center;gap:12px;flex-shrink:0;white-space:nowrap}
.group,.files{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden}
ul{list-style:none;padding:0;margin:0}.group{margin-top:20px}.files{margin-top:0}
li{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);
transition:background-color var(--duration-quick) var(--ease-smooth-out)}
li:last-child{border-bottom:0}li:hover{background:var(--hover)}
li a{color:var(--fg);text-decoration:none;font-weight:500}li a:hover{text-decoration:underline}
li>span:first-child{min-width:0;overflow-wrap:anywhere}
.file{display:flex;justify-content:space-between;align-items:center;gap:8px 16px;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--line)}
.file:last-child{border-bottom:0}
.file .info{flex:1 1 240px;min-width:0}
.file .kind{font-weight:400;font-size:.875rem;color:var(--mute);margin-left:6px}
.file .btn{min-width:112px}
.file .na{color:var(--mute);font-size:.875rem}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:.875rem;font-weight:600;text-transform:capitalize;color:var(--fg)}
.pill::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--dot)}
.s-done{--dot:var(--ok)}.s-failed{--dot:var(--err)}.s-running{--dot:var(--run)}.s-queued{--dot:var(--wait)}
.meta-row{display:flex;flex-wrap:wrap;align-items:center;gap:4px 14px;margin:0;color:var(--mute);font-size:.875rem;font-variant-numeric:tabular-nums}
progress{display:block;width:100%;height:6px;margin:16px 0;appearance:none;border:0;border-radius:3px;overflow:hidden;background:var(--line);color:var(--run)}
progress::-webkit-progress-bar{background:var(--line)}progress::-webkit-progress-value{background:var(--run)}progress::-moz-progress-bar{background:var(--run)}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0 0}
.actions.end{margin-top:12px}.actions.end .btn.danger{margin-left:-12px}
.warn{color:var(--mute)}.err{color:var(--err);font-weight:500}
pre{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:12px;
overflow:auto;font:.8125rem/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;max-height:340px;white-space:pre-wrap}
@media(max-width:520px){main{padding:24px 16px 48px}main>form{flex-wrap:wrap}main>form input{flex:1 1 100%}main>form button{width:100%}.file .btn{width:100%}.file .na{width:100%}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}button:active,.btn:active{transform:none}}
"""


def esc(s):
    return html.escape(str(s), quote=True)


THEME_HEAD = """<script>try{var t=localStorage.getItem('gpsync-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>"""
THEME_JS = """<script>(function(){var g=document.getElementById('theme'),r=document.documentElement;
function cur(){var t=r.dataset.theme;return t==='light'||t==='dark'?t:'auto'}
function show(){[].forEach.call(g.children,function(b){b.setAttribute('aria-pressed',b.dataset.t===cur())})}
g.onclick=function(e){var b=e.target.closest('button');if(!b)return;var n=b.dataset.t;
if(n==='auto')delete r.dataset.theme;else r.dataset.theme=n;
try{n==='auto'?localStorage.removeItem('gpsync-theme'):localStorage.setItem('gpsync-theme',n)}catch(x){}show()};show()})()</script>"""
THEME_BAR = (
    '<div class="themebar"><div class="theme" id="theme" role="group" aria-label="Colour theme">'
    '<button type="button" data-t="light" aria-label="Light" title="Light"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/></svg></button>'
    '<button type="button" data-t="auto" aria-label="Match system" title="Match system"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/>'
    '<path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></svg></button>'
    '<button type="button" data-t="dark" aria-label="Dark" title="Dark"><svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg></button>'
    '</div></div>')


def page(title, body, refresh=0, status=200):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    emit(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>{meta}
{THEME_HEAD}<style>{CSS}</style></head><body><main>{THEME_BAR}{body}<footer>{GH_LINK}</footer></main>{THEME_JS}</body></html>""",
         status, "text/html; charset=utf-8", ["Cache-Control: no-store"])


def pill(status):
    return f'<span class="pill s-{esc(status)}">{esc(status)}</span>'


def stemdeck_version():
    """StemDeck's version for the page footer, or None when it is not configured or does not answer."""
    if not STEMDECK_URL:
        return None
    try:
        from stemdeck import StemDeck
        return StemDeck(STEMDECK_URL).health(timeout=2)["version"] or None
    except Exception:
        return None


def index():
    release = (UPSTREAM / ".release").read_text().strip() if (UPSTREAM / ".release").exists() else "unknown"
    rows = "".join(
        f'<li><span><a href="/job?id={esc(j["id"])}">{esc(view(j)["name"] or "Song " + j["song"])}</a>'
        f'</span>'
        f'<span class="row-end">{pill(j["status"])} <span class="meta">{esc(fmt_duration(view(j)["duration_seconds"]))}</span>'
        f'{"" if j["status"] in ("queued", "running") else delete_button(j["id"], hidden=True)}</span></li>'
        for j in all_jobs()[:10])
    delete_all = ('<form class="del" method="post" action="/delete-all" style="margin:0 0 8px" '
                 'onsubmit="return confirm(\'Remove every finished song from the list and delete its files?\')">'
                 '<button class="btn alt small">Delete all</button></form>'
                 if any(j["status"] not in ("queued", "running") for j in all_jobs()) else "")
    # Delete buttons stay hidden until the Edit toggle (a checkbox, so it needs no script) is on.
    can_edit = any(j["status"] not in ("queued", "running") for j in all_jobs())
    recent = ('<input type="checkbox" id="edit" class="toggle"><div class="recent-head"><h2>Recent</h2>'
              + ('<label for="edit" class="btn alt small edit"><span class="on">Edit</span><span class="off">Done</span></label>'
                 if can_edit else "")
              + "</div>" + delete_all + ('<ul class="group">' + rows + "</ul>" if rows else '<p class="sub">Nothing yet.</p>'))
    sd = stemdeck_version()
    versions = f"guitar-pro-youtube-sync {esc(release)}" + (f" &middot; StemDeck {esc(sd)}" if sd else "")
    # The form makes everything that applies. Say so when stems are not possible, rather than hiding it.
    stems_note = ("" if sd else '<p class="warn">Stems are unavailable: StemDeck is '
                  + ("not answering" if STEMDECK_URL else "turned off" if STEMS_OFF else "not configured") + ".</p>")
    page("Tab Sync Service", f"""
<h1>Tab Sync Service</h1><p class="sub">Tab, full band and backing audio, and stems.</p>
<p class="meta">{versions}</p>
{stems_note}<form method="post" action="/get">
<input name="song" placeholder="Song ID, URL or search (artist + title)" required autofocus autocomplete="off">
<button>Download</button></form>
{recent}""")


def results_page(query):
    """Search text typed into the form: list what the source found, each with a button that starts the job."""
    try:
        hits = search_songs(query, 10)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return page("Tab Sync Service", f'<p class="err">Search failed: {esc(getattr(e, "reason", e))}</p><p><a href="/">Back</a></p>', status=502)
    rows = "".join(
        f'<li><span>{esc(h["artist"])} &ndash; <a href="{esc(h["url"])}" target="_blank" rel="noopener">{esc(h["title"])}</a> <span class="meta">#{h["song"]}</span></span>'
        f'<form method="post" action="/get" style="margin:0"><input type="hidden" name="song" value="{h["song"]}">'
        f'<button class="btn alt">Download</button></form></li>' for h in hits)
    body = f'<p class="back"><a href="/">&larr; Back</a></p><h1>Results for &ldquo;{esc(query)}&rdquo;</h1>'
    page("Tab Sync Service", body + (f'<ul class="group">{rows}</ul>' if rows else '<p class="sub">Nothing found.</p>'))


def start():
    """The form's POST, or a link (`/get?q=<song id, URL or search text>`). The web page makes everything that applies by default."""
    raw = param("song") or param("q")
    if raw and not normalize_song(raw):
        return results_page(raw)
    job, _, err = start_from_params()
    if err:
        return page("Tab Sync Service", f'<p class="err">{esc(err)}</p><p><a href="/">Back</a></p>', status=400)
    old = read_job(param("retry"))   # the Retry button: the new job takes the failed one's place
    if old and old["status"] == "failed" and old["id"] != job["id"]:
        delete_job(old["id"])
    emit("", 303, "text/plain", [f"Location: /job?id={job['id']}"])


def retry_stems_page():
    """The Retry button of the Best output: separate and pack again on the same job."""
    err = retry_stems(param("id"))
    if err and err != "No such job":
        return page("Tab Sync Service", f'<p class="err">{esc(err)}</p><p><a href="/">Back</a></p>', status=409)
    emit("", 303, "text/plain", [f"Location: /job?id={param('id')}"])


def delete_page():
    """Remove a job from the list and delete its files (the Delete button)."""
    err = delete_job(param("id"))
    if err and err != "No such job":
        return page("Tab Sync Service", f'<p class="err">{esc(err)}</p><p><a href="/">Back</a></p>', status=409)
    emit("", 303, "text/plain", ["Location: /"])


def delete_all_page():
    """Delete every finished or failed job (running and queued ones stay) and delete their files."""
    for j in all_jobs():
        if j["status"] not in ("queued", "running"):
            delete_job(j["id"])
    emit("", 303, "text/plain", ["Location: /"])


def delete_button(job_id, hidden=False):
    """`hidden`: shown only while the list is in edit mode (the Edit toggle)."""
    return (f'<form{" class=del" if hidden else ""} method="post" action="/delete" style="margin:0" onsubmit="return confirm(&quot;Delete this song and its files?&quot;)"><input type="hidden" name="id" value="{esc(job_id)}">'
            '<button class="btn danger small" title="Remove this song from the list and delete its files">Delete</button></form>')


def retry_button(job):
    """A failed job's Retry: the form's POST for the same song, which starts a new job (a failed one is never reused) and removes
    this one."""
    return (f'<form method="post" action="/get" style="margin:0"><input type="hidden" name="song" value="{esc(job["song"])}">'
            f'<input type="hidden" name="retry" value="{esc(job["id"])}">'
            '<button class="btn small" title="Start this song again as a new job">Retry</button></form>')


def job_page():
    job = read_job(param("id"))
    if not job:
        return page("Not found", '<p class="err">No such job.</p><p><a href="/">Back</a></p>', status=404)
    v = view(job)
    active = v["status"] in ("queued", "running")
    parts = ['<p class="back"><a href="/">&larr; All jobs</a></p>', f'<h1>{esc(v["name"] or "Song " + v["song"])}</h1>',
             f'<p class="meta-row">{pill(v["status"])}']
    if v["status"] == "running":
        parts.append(f'<span>step {v["step"]} of {v["steps"]}</span>')
        pr = v.get("progress")
        if pr:
            parts.append(f'<span>{esc(pr["stage"])}' + (f' {pr["percent"]:g}%' if pr["percent"] is not None else "") + "</span>")
    if v["duration_seconds"] is not None:
        label = "running for" if v["status"] == "running" else "took"
        parts.append(f'<span>{label} {esc(fmt_duration(v["duration_seconds"]))}</span>')
    elif v["status"] == "queued":
        parts.append(f'<span>waiting {esc(fmt_duration(seconds_between(v["created"])))}</span>')
    parts.append("</p>")
    if v.get("source_url"):
        parts.append(f'<p class="meta"><a href="{esc(v["source_url"])}" target="_blank" rel="noopener">{esc(v["source_url"])}</a></p>')
    if active:
        parts.append(f'<progress max="{v["steps"]}" value="{v["step"]}"></progress>')
    if v["status"] == "done" and v.get("purged"):
        parts.append('<p class="warn">The files were deleted to free space. Start the song again to get them.</p>')
    elif v["status"] == "done":
        # Good, better, best. One filled button per screen: Open in when it is set, otherwise the best file.
        outputs = list(v["outputs"])
        if v.get("open_url"):
            parts.append(f'<div class="actions"><a class="btn" href="{esc(v["open_url"])}">{esc(OPEN_LABEL)}</a></div>')
        primary = None if v.get("open_url") else next((o["tier"] for o in reversed(outputs) if o["available"]), None)
        rows = []
        for o in outputs:
            kind = {"best": "MPAK with Stems", "better": "MPAK with Full Band + Backing", "good": "GP"}[o["tier"]]
            info = f'<div class="info"><h3>{esc(o["title"])}<span class="kind">{esc(kind)}</span></h3><p class="meta">{esc(o["description"])}</p>'
            if o["available"]:
                cls = "btn" if o["tier"] == primary else "btn alt"
                action = f'<a class="{cls}" href="{esc(o["file_url"])}" aria-label="Download {esc(kind)}">Download</a>'
            else:
                info += f'<p class="meta">Not made: {esc(o["reason"])}</p>'
                action = ""
                if o["tier"] == "best" and v.get("can_retry_stems"):
                    action = (f'<form method="post" action="/retry-stems" style="margin:0"><input type="hidden" name="id" value="{esc(job["id"])}">'
                              '<button class="btn alt" title="Separate the stems and pack them again">Retry</button></form>')
            rows.append(f'<div class="file t-{o["tier"]}">{info}</div>{action}</div>')
        parts.append('<h2>Files</h2><div class="files">' + "".join(rows) + "</div>")
    if v["status"] == "failed":
        parts.append(f'<p class="err">{esc(v["error"])}</p>')
    log = read_log(job["id"]).strip()
    parts.append('<div class="loghead"><h2>Log</h2><button type="button" class="copy" id="copylog">Copy</button></div>')
    parts.append(f'<pre id="log">{esc(log) or "(no output yet)"}</pre>')
    parts.append(f'<p class="meta">Job {esc(v["id"])} &middot; created {esc(v["created"])} &middot; '
                 f'<a href="{esc(v["status_url"])}">JSON</a></p>')
    if not active:
        retry = retry_button(job) if v["status"] == "failed" else ""
        parts.append(f'<div class="actions end">{retry}{delete_button(job["id"])}</div>')
    # While the job runs the page fetches itself and swaps the changed parts in place (no reload, so no flash). The log starts at
    # the newest line and follows new output, unless the reader scrolled up: then it keeps their place.
    body = f'<div id="live" data-active="{1 if active else 0}">{"".join(parts)}</div>'
    body += """<script>(function(){var live=document.getElementById('live');if(!live)return;
var k='gpsync-log-'+location.search,saved=null;try{saved=JSON.parse(sessionStorage.getItem(k))}catch(e){}
function log(){return document.getElementById('log')}
function place(l,s){if(l)l.scrollTop=(s&&!s.bottom)?s.top:l.scrollHeight}
function state(l){return{top:l.scrollTop,bottom:l.scrollTop+l.clientHeight>=l.scrollHeight-4}}
place(log(),saved);
live.addEventListener('click',function(e){var b=e.target.closest('#copylog');if(!b||!log())return;var t=log().textContent;
function ok(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1500)}
function old(){var a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();
try{document.execCommand('copy')&&ok()}catch(x){}document.body.removeChild(a)}
if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(t).then(ok,old);else old()});
live.addEventListener('scroll',function(e){if(e.target.id==='log')try{sessionStorage.setItem(k,JSON.stringify(state(e.target)))}catch(x){}},true);
function poll(){if(live.dataset.active!=='1')return;
fetch(location.href,{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.ok?r.text():null}).then(function(t){
if(t){var n=new DOMParser().parseFromString(t,'text/html').getElementById('live');
if(n){var l=log(),s=l?state(l):null,done=n.dataset.active!=='1';
var a=live.children,b=n.children;
if(a.length!==b.length){live.innerHTML=n.innerHTML;place(log(),s)}
else for(var i=0;i<b.length;i++)if(a[i].outerHTML!==b[i].outerHTML){var isLog=a[i].id==='log';a[i].replaceWith(b[i].cloneNode(true));if(isLog)place(log(),s)}
live.dataset.active=n.dataset.active;
if(done)return}}
setTimeout(poll,3000)}).catch(function(){setTimeout(poll,3000)})}
setTimeout(poll,3000)})();</script>"""
    page(v["name"] or f"Song {v['song']}", body)


COMMANDS = {
    "index": index, "start": start, "job": job_page, "delete": delete_page, "retry-stems": retry_stems_page, "delete-all": delete_all_page, "api-delete": api_delete, "api-retry-stems": api_retry_stems,
    "api-create": api_create, "api-list": api_list, "api-job": api_job, "api-file": api_file, "api-search": api_search,
    "health": health, "openapi": openapi, "opensearch": opensearch,
}
OPEN_COMMANDS = ("health", "openapi", "opensearch")   # need no token (health says less without one)


def dispatch(command, params, headers):
    """Run one request: `command` is a key of COMMANDS, `params` the query and form values, `headers` the request headers.
    Returns (status, ["Name: value", ...], body) where body is bytes or a Path."""
    _ctx.params = params
    _ctx.headers = {k.lower(): v for k, v in headers.items()}
    _ctx.set_cookie = []
    _ctx.response = None
    if command in OPEN_COMMANDS or authorized(command):
        # A page opened with a valid ?token= keeps it in a cookie, so later links and forms need no parameter.
        if AUTH_TOKEN and not command.startswith("api-") and param("token") and hmac.compare_digest(
                param("token").encode(), AUTH_TOKEN.encode()):
            _ctx.set_cookie.append(f"Set-Cookie: {COOKIE}={quote(AUTH_TOKEN, safe='')}; Path=/; HttpOnly; SameSite=Lax")
        COMMANDS[command]()
    else:
        deny(command)
    return _ctx.response


if __name__ == "__main__":
    JOBS.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] in ("run", "stems"):
        {"run": run, "stems": run_stems}[sys.argv[1]](sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] in ("recover", "sweep"):
        {"recover": recover, "sweep": sweep}[sys.argv[1]]()
    else:
        sys.exit(f"usage: {sys.argv[0]} recover | sweep | run <job-id> | stems <job-id>   (the server is server.py)")
