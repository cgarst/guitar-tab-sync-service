#!/usr/bin/env python3
"""Fetch the upstream release (or branch) and install its dependencies.

Runs at container start. If GitHub is unreachable but a previous install exists,
the service starts with that copy instead of failing.
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from urllib.parse import quote
from pathlib import Path

REPO = os.environ.get("GPYTSYNC_REPO", "teaqu/guitar-pro-youtube-sync")
REF = os.environ.get("GPYTSYNC_VERSION", "latest")
BRANCH = os.environ.get("GPYTSYNC_BRANCH", "").strip()  # when set, track this branch instead of a release
UPSTREAM = Path(os.environ.get("UPSTREAM_DIR", "/opt/upstream"))
PYTHON = os.environ.get("UPSTREAM_PYTHON", "/opt/venv/bin/python")
MARKER = UPSTREAM / ".release"


def get(url: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"User-Agent": "gpsync-service", "Accept": accept})
    return urllib.request.urlopen(req, timeout=60)


def resolve() -> tuple[str, str]:
    if BRANCH:
        # The marker holds branch@sha, so a new commit on the branch is picked up at the next start.
        with get(f"https://api.github.com/repos/{REPO}/commits/{quote(BRANCH, safe='')}") as r:
            sha = json.load(r)["sha"]
        return f"{BRANCH}@{sha[:12]}", f"https://api.github.com/repos/{REPO}/tarball/{sha}"
    path = "releases/latest" if REF == "latest" else f"releases/tags/{REF}"
    with get(f"https://api.github.com/repos/{REPO}/{path}") as r:
        rel = json.load(r)
    return rel["tag_name"], rel["tarball_url"]


def install_release(tag: str, tarball_url: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "release.tar.gz"
        with get(tarball_url, "*/*") as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        with tarfile.open(archive) as tar:
            tar.extractall(Path(tmp) / "src", filter="data")
        (top,) = (Path(tmp) / "src").iterdir()
        if not (top / "sync.py").exists():
            raise RuntimeError(f"release {tag} has no sync.py")
        # Keep the directory itself (it may be a mount point); replace its contents.
        for child in UPSTREAM.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        for child in top.iterdir():
            shutil.move(str(child), UPSTREAM / child.name)
    MARKER.write_text(tag)


def main() -> None:
    have = MARKER.read_text().strip() if MARKER.exists() else None
    try:
        tag, url = resolve()
        if tag == have:
            print(f"[update] {REPO} {tag} already installed")
        else:
            print(f"[update] installing {REPO} {tag} (was {have or 'nothing'})")
            install_release(tag, url)
    except Exception as e:
        if not (UPSTREAM / "sync.py").exists():
            sys.exit(f"[update] cannot fetch {REPO} and nothing is installed: {e}")
        print(f"[update] WARNING: could not check for a new release ({e}); using {have}")

    # Always refresh dependencies: yt-dlp breaks often and the requirement is a floor.
    req = UPSTREAM / "requirements.txt"
    pip = [PYTHON, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-U", "-r", str(req)]
    if subprocess.call(pip) != 0:
        print("[update] WARNING: pip install failed; checking existing environment")
    if subprocess.call([PYTHON, "-c", "import yt_dlp, requests"]) != 0:
        sys.exit("[update] dependencies are not importable")
    print(f"[update] ready: {MARKER.read_text().strip()}")


if __name__ == "__main__":
    main()
