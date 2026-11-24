"""Where songs come from. A source knows how to read a song ID out of what a person typed, how to search for songs, and what
a song's page is called. There is one source today; a new one is another `Source` in `SOURCES`.

The first source's host is not written down here. It is read from the tab and audio downloader release, which is the code that fetches
the tab and the bar times from it, so the two always agree. `SOURCE_URL` overrides it; only the tests use that.
"""
import json
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import quote

UPSTREAM = Path(os.environ.get("UPSTREAM_DIR", "/opt/upstream"))


def upstream_base(upstream_dir=None):
    """The `https://host` the downloader's sync.py fetches song metadata from, or None when it cannot be found."""
    try:
        text = (Path(upstream_dir or UPSTREAM) / "sync.py").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"https://[A-Za-z0-9.-]+(?=/api/meta/)", text)
    return m.group(0) if m else None


class SourceError(Exception):
    """The source is not usable (not configured) or did not answer."""


class Source:
    def __init__(self, id, label, base_url):
        self.id, self.label = id, label
        self._base = base_url   # a string, or a function that returns one (looked up when first needed)

    @property
    def base_url(self):
        if callable(self._base):
            self._base = self._base()
        if not self._base:
            raise SourceError(f"The {self.label} address could not be found in the tab and audio downloader.")
        return self._base.rstrip("/")

    def parse(self, raw):
        """A song ID, or a URL of a song page, to the numeric ID as a string; None for anything else."""
        raw = raw.strip()
        if re.fullmatch(r"\d{1,9}", raw):
            return str(int(raw))
        m = re.search(r"-s(\d{1,9})(?:\D|$)", raw)
        return m.group(1) if m else None

    def song_url(self, song_id):
        return f"{self.base_url}/a/wsa/s{song_id}"

    def search(self, query, size=10):
        """Songs matching `query`: [{song, artist, title, url}], best match first. Raises URLError/OSError/ValueError on failure."""
        url = f"{self.base_url}/api/songs?size={int(size)}&pattern={quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (guitar-tab-sync-service)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            found = json.load(r)
        return [{"song": str(h["songId"]), "artist": h.get("artist", ""), "title": h.get("title", ""),
                 "url": self.song_url(h["songId"])}
                for h in found if isinstance(h, dict) and isinstance(h.get("songId"), int)]


def _default_base():
    return os.environ.get("SOURCE_URL", "").strip() or upstream_base()


SOURCES = {"default": Source("default", "tab source", _default_base)}


def source(source_id="default"):
    return SOURCES[source_id]
