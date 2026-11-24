"""Minimal StemDeck client (stdlib only), mirroring MajesticTab's src/stemdeck/stemdeckClient.js.

Calls used, all under <base>/api:
    GET    health                          must answer {"name": "StemDeck", ...}
    POST   jobs (multipart file+stems)     -> {"job_id"}
    GET    jobs/<id>                       -> {status, progress, stage, error}
    GET    jobs/<id>/mixdown.<flac|mp3>    the chosen stems summed into one file; one stem gives that stem
    POST   jobs/<id>/cancel
    DELETE jobs/<id>
Plain http only (StemDeck's https listener uses a self-signed certificate).
"""
import json
import os
import secrets
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

# The full htdemucs_6s set, in the order the pack lists them.
ALL_STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other")

FINISHED = {"done", "error", "cancelled", "stopped"}
ACCEPTED_EXTS = {"mp3", "wav", "flac", "mp4", "m4a", "ogg", "opus"}
LOSSLESS_EXTS = {"wav", "flac"}
POLL_SECONDS = 1.0
MAX_POLL_FAILURES = 5


class StemDeckError(Exception):
    pass


def configured_url():
    """The StemDeck address the app should use, or "" when stems are turned off (STEMDECK_ENABLED=0, or no address)."""
    if os.environ.get("STEMDECK_ENABLED", "1").strip() == "0":
        return ""
    return os.environ.get("STEMDECK_URL", "").strip()


def mix_format_for(filename):
    """Lossless source -> flac, lossy -> mp3 (so the result is about the size of what was sent)."""
    ext = Path(filename).suffix.lower().lstrip(".")
    return "flac" if ext in LOSSLESS_EXTS else "mp3"


class StemDeck:
    def __init__(self, base_url, poll_seconds=POLL_SECONDS):
        base = base_url.strip().rstrip("/")
        if base.lower().startswith("https://"):
            raise StemDeckError("STEMDECK_URL must be http:// (StemDeck's https listener is self-signed)")
        if "://" not in base:
            base = "http://" + base
        self.base = base + "/api"
        self.poll_seconds = poll_seconds

    # -- plumbing ---------------------------------------------------------- #
    def _open(self, path, data=None, headers=None, method=None, timeout=60):
        # StemDeck refuses plain http from another device ("needs an https:// address") unless a proxy says it
        # terminated TLS. Our hop is a private network between containers, so say so, as a reverse proxy would.
        headers = {"X-Forwarded-Proto": "https", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise StemDeckError(self._reason(e, f"StemDeck answered HTTP {e.code}"), ) from e
        except (urllib.error.URLError, OSError) as e:
            raise StemDeckError(f"StemDeck is not answering at {self.base}: {getattr(e, 'reason', e)}") from e

    @staticmethod
    def _reason(err, fallback):
        """The reason StemDeck gave for refusing a request, in its own words."""
        try:
            detail = json.load(err).get("detail")
            if isinstance(detail, str) and detail:
                return detail
        except (ValueError, OSError, AttributeError):
            pass
        return fallback

    # -- API --------------------------------------------------------------- #
    def health(self, timeout=10):
        try:
            with self._open("/health", timeout=timeout) as r:
                body = json.load(r)
        except StemDeckError as e:
            if "HTTP 403" in str(e):
                raise StemDeckError("StemDeck is not available on the network (turn it on in StemDeck's settings)") from e
            raise
        except ValueError as e:
            raise StemDeckError("Something other than StemDeck is answering") from e
        if not isinstance(body, dict) or body.get("name") != "StemDeck":
            raise StemDeckError("Something other than StemDeck is answering")
        return {"version": body.get("version", ""), "device": body.get("demucs_device", ""),
                "formats": body.get("export_formats")}

    def submit(self, audio_path, stems=ALL_STEMS):
        audio_path = Path(audio_path)
        if audio_path.suffix.lower().lstrip(".") not in ACCEPTED_EXTS:
            raise StemDeckError(f"StemDeck does not accept {audio_path.suffix} files")
        boundary = "gpsync" + secrets.token_hex(12)
        head = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"stems\"\r\n\r\n{json.dumps(list(stems))}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{quote(audio_path.name)}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        body = head + audio_path.read_bytes() + tail
        with self._open("/jobs", data=body, method="POST", timeout=300, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"}) as r:
            job_id = json.load(r).get("job_id")
        if not job_id:
            raise StemDeckError("StemDeck did not start the job")
        return job_id

    def state(self, job_id):
        try:
            with self._open(f"/jobs/{quote(job_id)}", timeout=30) as r:
                return json.load(r)
        except StemDeckError as e:
            if "HTTP 404" in str(e):
                raise StemDeckError("StemDeck no longer has this job") from e
            raise

    def wait(self, job_id, on_progress=None, timeout=None):
        """Poll until the job is done. Raises on error/cancel/timeout, tolerating brief outages."""
        deadline = time.monotonic() + timeout if timeout else None
        failures = 0
        while True:
            state = None
            try:
                state = self.state(job_id)
                failures = 0
            except StemDeckError as e:
                failures += 1
                if "no longer has" in str(e) or failures >= MAX_POLL_FAILURES:
                    raise
            if state:
                if on_progress:
                    on_progress(state.get("status", ""), state.get("progress") or 0, state.get("stage") or "")
                status = state.get("status")
                if status == "done":
                    return state
                if status in FINISHED:
                    raise StemDeckError(state.get("error") or (
                        "StemDeck could not separate this file" if status == "error" else f"Job {status} in StemDeck"))
            if deadline and time.monotonic() > deadline:
                raise StemDeckError("Timed out waiting for StemDeck")
            time.sleep(self.poll_seconds)

    def download_stem(self, job_id, stem, ext, dest):
        """One stem as a file: the mixdown of just that stem."""
        if ext not in ("mp3", "flac"):
            raise StemDeckError(f"unsupported stem format {ext}")
        path = f"/jobs/{quote(job_id)}/mixdown.{ext}?stems={quote(stem)}&gains=1"
        tmp = Path(str(dest) + ".part")
        with self._open(path, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        if tmp.stat().st_size == 0:
            tmp.unlink()
            raise StemDeckError(f"StemDeck returned an empty {stem} stem")
        os.replace(tmp, dest)

    def cancel(self, job_id):
        """Never raises: it is called on the way out."""
        try:
            self._open(f"/jobs/{quote(job_id)}/cancel", data=b"", method="POST", timeout=10).close()
        except Exception:
            pass

    def delete(self, job_id):
        try:
            self._open(f"/jobs/{quote(job_id)}", method="DELETE", timeout=30).close()
        except Exception:
            pass
