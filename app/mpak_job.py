#!/usr/bin/env python3
"""Runs one MPAK job: `mpak_job.py --song <id>` in the job's output directory. Started by gpsync.py with the
the downloader venv's Python (it needs yt-dlp and requests).

Unlike sync.py, which writes a single *_synced.gp with the audio embedded, this keeps the pieces apart:
the plain tab, the untrimmed YouTube audio and the source's own bar times. It runs the downloader's `sync.py --raw`
(needs a release that has the flag) and imports its other functions rather than re-implementing them.

It makes every output that applies: the Guitar Pro file, a pack of the full band and the source's backing video (two
recordings, each with its own sync), and a pack with the six stems. A missing StemDeck or backing video skips that pack.
With `--resume` (the "Retry stems" button) it runs steps 3 and 4 again from the files a failed separation left in work/.
Steps (printed as [n/4]; parsed by gpsync.py):
    1 tab and audio (song metadata, video points, tab, audio)   2 backing audio   3 stems (StemDeck)   4 pack
The shared sync lock (inherited as GPSYNC_LOCK_FD) is released after step 2: StemDeck queues its own work, and the
minutes it takes must not block other jobs' downloads.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

APP = Path(__file__).resolve().parent
UPSTREAM = Path(os.environ.get("UPSTREAM_DIR", "/opt/upstream"))
sys.path.insert(0, str(APP))
sys.path.append(str(UPSTREAM))

import mpak  # noqa: E402
import sources  # noqa: E402
from stemdeck import ALL_STEMS, StemDeck, StemDeckError, configured_url, mix_format_for  # noqa: E402

STEPS = 4


def step(n, text):
    print(f"\n[{n}/{STEPS}] {text}", flush=True)


def release_lock():
    fd = os.environ.get("GPSYNC_LOCK_FD")
    if fd and fd.isdigit():
        fcntl.flock(int(fd), fcntl.LOCK_UN)


def score_origin():
    """The score's `origin` (SPEC section 6.4): the source's host name, a name for people. None when the source is unknown."""
    try:
        return urlparse(sources.source().base_url).hostname
    except sources.SourceError:
        return None


DOWNLOAD_ATTEMPTS = 3
RETRY_PAUSE = float(os.environ.get("GPSYNC_RETRY_PAUSE", "8"))  # seconds, times the attempt number


def download_audio(sync, video_id, path):
    """The downloader's download, tried up to DOWNLOAD_ATTEMPTS times: YouTube now and then answers a media request with HTTP 403 and
    the same request works a moment later. Raises the last error."""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            sync.download_youtube_audio(video_id, path, trim_start=0.0)
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("no file was produced")
            return
        except Exception as e:
            path.unlink(missing_ok=True)
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            print(f"  Download failed ({e}); trying again ({attempt + 1}/{DOWNLOAD_ATTEMPTS})...", flush=True)
            time.sleep(RETRY_PAUSE * attempt)


def run_upstream_raw(song_id, work):
    """Run `sync.py --song <id> --raw` in `work`, retried like download_audio, with its output shown live. It names its files
    after the song, so they are found by the sync JSON it writes. Returns (tab, raw audio, synced gp) as Paths and the parsed sync
    JSON (artist, title, revision, bar times). It carries on without audio when a download fails, so the file is checked."""
    cmd = [sys.executable, str(UPSTREAM / "sync.py"), "--song", str(song_id), "--raw"]
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        for f in (*work.glob("*.gp"), *work.glob("*_raw.mp3"), *work.glob("*_sync.json"), *work.glob(".tmp_audio.mp3")):
            f.unlink()
        p = subprocess.Popen(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        seen = []
        for line in p.stdout:
            seen.append(line)
            # The downloader's own "[n/5]" headings would be read as this job's progress, so they are shown with parentheses.
            print("  | " + re.sub(r"\[(\d)/(\d)\]", r"(\1/\2)", line.rstrip("\n")), flush=True)
        p.wait()
        # The downloader carries on without audio when YouTube refuses, so the reason is in its output, not in its exit code.
        why = next((m.group(1) for l in reversed(seen) if (m := re.search(r"Audio download failed: (?:ERROR: )?(.*)", l))), None)
        found = sorted(work.glob("*_sync.json"))
        if p.returncode == 0 and len(found) == 1:
            base = found[0].name[:-len("_sync.json")]
            tab, raw, synced = (work / f"{base}{suffix}" for suffix in (".gp", "_raw.mp3", "_synced.gp"))
            if tab.is_file() and raw.is_file() and raw.stat().st_size > 0:
                return tab, raw, synced, json.loads(found[0].read_text())
        if attempt == DOWNLOAD_ATTEMPTS:
            if why and "not a bot" in why and not os.environ.get("YOUTUBE_PROXY_URL"):
                why += " (YouTube blocks datacenter addresses; set YOUTUBE_PROXY_URL to use a home proxy)"
            raise RuntimeError(f"Audio download failed: {why}" if why else "Audio download failed: no audio was produced"
                               if p.returncode == 0 else f"The tab and audio downloader exited with {p.returncode}")
        print(f"  The tab and audio downloader failed; trying again ({attempt + 1}/{DOWNLOAD_ATTEMPTS})...", flush=True)
        time.sleep(RETRY_PAUSE * attempt)


def choose_backing(candidates, meta):
    """The backing video the source's player opens: a backing entry is the mix without its `tracks`, and the player opens the one
    that leaves out the song's default track (`defaultTrack`). Falls back to the one for the popular guitar track, then to the
    first. `candidates` are entries with `tracks` and `points`, in the source's order; only those with bar times count."""
    candidates = [c for c in candidates if c.get("points")]
    for key in ("defaultTrack", "popularTrackGuitar"):
        track = meta.get(key)
        if track is None:
            continue
        for c in candidates:
            if isinstance(c.get("tracks"), list) and track in c["tracks"]:
                return c
    return candidates[0] if candidates else None


def backing_label(entry):
    """"Backing (without Guitar)": a backing video is the mix without the instruments the source lists as `without`. Plain "Backing"
    when it lists none (an older release does not)."""
    names = [n.title() for n in entry.get("without") or []]
    return f"Backing (without {', '.join(names)})" if names else "Backing"


def check_points(gp_path, points):
    """Warn when the bar times cannot match the tab (the reader would then ignore them)."""
    try:
        with zipfile.ZipFile(gp_path) as z:
            gpif = z.read("Content/score.gpif").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return
    bars = len(re.findall(r"<MasterBar>", gpif))
    if "<Repeat" not in gpif and len(points) > bars:
        print(f"  WARNING: {len(points)} bar times but the tab has {bars} bars; a reader will ignore the sync data.")


def separate(stemdeck, mix_path, work, deadline):
    """Run the mix through StemDeck and fetch all six stems: [(name, Path)]. Cancels the StemDeck job and re-raises on any failure."""
    ext = mix_format_for(mix_path.name)
    job_id = None
    try:
        job_id = stemdeck.submit(mix_path, ALL_STEMS)
        print(f"  StemDeck job {job_id}", flush=True)
        last = [None]

        def progress(status, fraction, stage):
            # StemDeck's stage text is often a progress line of its own ("Separating 56%"). Its overall figure (0-100) lags
            # that and sits near 0 in the stages after separating ("Mixing tracks... 1%"), where it would make a bar go
            # back from 100%, so the stage is shown as it says it and no figure is added
            text = stage or status
            line = f"  {text}"
            if line != last[0]:
                last[0] = line
                print(line, flush=True)

        remaining = max(1, deadline - time.time()) if deadline else None
        stemdeck.wait(job_id, on_progress=progress, timeout=remaining)
        stems = []
        for name in ALL_STEMS:
            dest = work / f"{name}.{ext}"
            print(f"  Fetching {name}", flush=True)
            stemdeck.download_stem(job_id, name, ext, dest)
            stems.append((name, dest))
    except Exception:  # StemDeckError, or anything else: do not leave the separation running
        if job_id:
            stemdeck.cancel(job_id)
            stemdeck.delete(job_id)
        raise
    stemdeck.delete(job_id)
    return stems


def build_pack(out, *, title, artist, gp_path, mix_path, stems, video_id, sync_bytes, backing=None):
    manifest = mpak.build_manifest(title=title, artist=artist, score_path=gp_path, mix_path=mix_path, stems=stems,
                                   video_id=video_id, sync_bytes=sync_bytes, origin=score_origin(), backing=backing)
    mpak.write_mpak(out, manifest, sync_bytes, gp_path, mix_path, stems, backing=backing)


RESUME = "resume.json"   # work/resume.json: what "Retry stems" needs when the separation failed


def save_resume(work, **state):
    """Keep the downloaded audio and tab so the separation and packing can be run again without downloading anything."""
    (work / RESUME).write_text(json.dumps(state))


def resume_stems(deadline):
    """`--resume`: steps 3 and 4 again for a job whose separation failed, from the files kept in work/. Adds the stems pack to
    the outputs already made. A failure is a warning, as in a first run: the job keeps its other outputs and can be retried again."""
    work = Path("work")
    try:
        st = json.loads((work / RESUME).read_text())
        gp_path, mix_path = work / st["gp"], work / st["mix"]
        if not (gp_path.is_file() and mix_path.is_file()):
            raise OSError("missing file")
    except (OSError, ValueError, KeyError):
        sys.exit("ERROR: The downloaded audio is no longer there, so the stems cannot be retried. Start the song again.")
    try:
        stemdeck = check_stemdeck(configured_url())
    except StemDeckError as e:
        print(f"WARNING: stems skipped: {e}", flush=True)
        return
    step(3, "Separating stems in StemDeck...")
    try:
        stems = separate(stemdeck, mix_path, work, deadline)
    except Exception as e:
        print(f"WARNING: stems skipped: {e}", flush=True)
        return
    step(4, "Packing the stems .mpak...")
    sync_bytes = mpak.json_bytes(mpak.build_sync(st["points"], st["video_id"], st["revision_id"]))
    out = Path(f"{st['base']} (stems).mpak")
    build_pack(out, title=st["title"], artist=st["artist"], gp_path=gp_path, mix_path=mix_path, stems=stems,
               video_id=st["video_id"], sync_bytes=sync_bytes)
    shutil.rmtree(work, ignore_errors=True)
    print(f"Saved: {out}")
    print("\nDone!")


def check_stemdeck(url):
    """A ready StemDeck client, or raises StemDeckError saying why not."""
    if not url:
        raise StemDeckError("StemDeck is not configured or is turned off.")
    stemdeck = StemDeck(url)
    info = stemdeck.health()
    print(f"StemDeck {info['version']} ({info['device'] or 'device unknown'}) is ready", flush=True)
    for fmt in ("mp3", "flac"):
        if isinstance(info["formats"], dict) and info["formats"].get(fmt) is False:
            raise StemDeckError(f"StemDeck's ffmpeg cannot encode {fmt}.")
    return stemdeck


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song")
    ap.add_argument("--resume", action="store_true", help="run the separation and packing again from the kept files")
    args = ap.parse_args()
    deadline = float(os.environ["GPSYNC_DEADLINE"]) if os.environ.get("GPSYNC_DEADLINE") else None
    if args.resume:
        return resume_stems(deadline)
    if not args.song:
        ap.error("--song is required")
    song_id = int(args.song)

    # Stems are used whenever StemDeck is there; without it (or when it fails) that output is skipped.
    want_stems, stemdeck = True, None
    try:
        stemdeck = check_stemdeck(configured_url())
    except StemDeckError as e:
        print(f"WARNING: stems skipped: {e}", flush=True)
        want_stems = False

    import sync

    work = Path("work")
    work.mkdir(exist_ok=True)
    # The downloader does the song metadata, video points, tab, untrimmed audio and synced file in one run (sync.py --raw).
    step(1, "Fetching the tab and audio (song metadata, video points, tab, audio)...")
    try:
        gp_path, mix_path, synced_by_upstream, data = run_upstream_raw(song_id, work)
    except Exception as e:
        sys.exit(f"ERROR: Audio download failed: {e}")
    artist, title, revision_id, points, video_id = data["artist"], data["title"], data["revisionId"], data["points"], data["videoId"]
    base = gp_path.stem
    print(f"  Song: {artist} - {title}\n  Latest revision: {revision_id}")
    if not points:
        sys.exit("ERROR: The source has no sync points for this song.")
    check_points(gp_path, points)
    backing_entry = choose_backing(data.get("categories", {}).get("backing", []), data)

    want_band = True
    if backing_entry:
        print(f"  Backing video: https://youtu.be/{backing_entry['videoId']}, points={len(backing_entry['points'])}")
    else:
        print("  WARNING: no backing video with sync points, so there is no full band/backing pack.")
        want_band = False

    backing_path = None
    if want_band:  # still under the sync lock: both downloads are YouTube requests
        step(2, "Downloading the backing video...")
        backing_path = work / f"{base} (backing).mp3"
        try:
            download_audio(sync, backing_entry["videoId"], backing_path)
        except Exception as e:
            print(f"  WARNING: backing audio download failed ({e}), so there is no full band/backing pack.")
            backing_path = None
    else:
        step(2, "Downloading the backing video... skipped")
    release_lock()

    stems = []
    if want_stems:
        step(3, "Separating stems in StemDeck...")
        try:
            stems = separate(stemdeck, mix_path, work, deadline)
        except Exception as e:
            print(f"  WARNING: stems skipped: {e}", flush=True)
    else:
        step(3, "Separating stems in StemDeck... skipped")

    step(4, "Packing the .mpak files and the Guitar Pro file...")
    sync_bytes = mpak.json_bytes(mpak.build_sync(points, video_id, revision_id))
    outputs = []

    def pack(out, stems_in, backing):
        build_pack(out, title=title, artist=artist, gp_path=gp_path, mix_path=mix_path, stems=stems_in,
                   video_id=video_id, sync_bytes=sync_bytes, backing=backing)
        outputs.append(out)

    if stems:
        pack(Path(f"{base} (stems).mpak"), stems, None)
    if backing_path:
        backing = (backing_path, backing_entry["videoId"],
                   mpak.json_bytes(mpak.build_sync(backing_entry["points"], backing_entry["videoId"], revision_id, "backing")),
                   backing_label(backing_entry))
        pack(Path(f"{base} (full band + backing).mpak"), [], backing)
    gp_out = Path(f"{base}_synced.gp")
    if synced_by_upstream.is_file():
        shutil.move(str(synced_by_upstream), gp_out)
        print(f"Saved: {gp_out}")
    if gp_out.is_file():
        outputs.append(gp_out)
    if configured_url() and not stems:
        # The stems are missing but StemDeck is there (or was meant to be): keep what a retry needs.
        save_resume(work, gp=gp_path.name, mix=mix_path.name, base=base, artist=artist, title=title,
                    revision_id=revision_id, points=points, video_id=video_id)
    else:
        shutil.rmtree(work, ignore_errors=True)
    if not outputs:
        sys.exit("ERROR: No output file could be made.")
    for out in outputs:
        print(f"Saved: {out}")
    print("\nDone!")


if __name__ == "__main__":
    main()
