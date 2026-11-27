"""Stand-in for upstream sync.py so the service can be tested offline.

As a script it mimics `python sync.py --song N` (writes *_synced.gp). As a module it offers the functions that
app/mpak_job.py imports from the real upstream.
"""
import argparse, sys, time
from pathlib import Path

POINTS = [0.5 + 2.0 * i for i in range(8)]


def fetch_song_meta(song_id):
    if str(song_id) == "666":
        raise RuntimeError("could not fetch song")
    meta = {"artist": "Fake Artist", "title": f"Song {song_id}", "revisionId": 1234}
    if str(song_id) == "999":  # several backing videos; the default track (3) is left out of the third
        meta.update(defaultTrack=3, popularTrackGuitar=2)
    return meta


BACKING_POINTS = [1.0 + 2.5 * i for i in range(8)]


def fetch_video_points(song_id, revision_id):
    entries = [{"videoId": "AAAAAAAAAAA", "points": POINTS, "feature": None, "status": "done"}]
    if str(song_id) != "556":  # 556 has no backing video
        entries.append({"videoId": "BBBBBBBBBBB", "points": BACKING_POINTS, "feature": "backing", "status": "done",
                        "tracks": "All"})
    if str(song_id) == "999":
        for vid, tracks in (("CCCCCCCCCCC", [6]), ("DDDDDDDDDDD", [10]), ("EEEEEEEEEEE", [2, 3])):
            entries.append({"videoId": vid, "points": BACKING_POINTS, "feature": "backing", "status": "done", "tracks": tracks,
                            "without": ["drums"] if tracks == [10] else ["bass"] if tracks == [6] else ["guitar"]})
    return entries


def get_video_options(entries, tracks_meta=None):
    cats = {}
    for e in entries:
        if e.get("feature") not in (None, "alternative") and e.get("status") == "done":
            cats.setdefault(e["feature"], []).append({"entry": e, "label": "All instruments"})
    return {"full_mix": entries[0], "categories": cats}


def select_video_entry(entries, video_index=None):
    return entries[video_index or 0]


def download_youtube_audio(video_id, output_path, trim_start=0.0, cookies_browser=None):
    if "Song 777" in str(output_path) and str(output_path).endswith("_raw.mp3"):
        raise RuntimeError("HTTP Error 403")
    if "Song 778" in str(output_path) and str(output_path).endswith("_raw.mp3") and not Path("flaky-failed").exists():
        Path("flaky-failed").write_text("1")  # fails once, then works (YouTube's intermittent 403)
        raise RuntimeError("HTTP Error 403: Forbidden")
    Path(output_path).write_bytes(b"ID3fake-mp3-" + video_id.encode())
    return Path(output_path)


def _ffmpeg_bin():
    return "true"  # the trim is a no-op here; the fake mp3 is read as is


def sync_gp_file(gp_file, points, output_path, mp3_path=None):
    Path(output_path).write_bytes(b"PK\x03\x04synced")
    return []


def main():
    p = argparse.ArgumentParser(); p.add_argument("--song", required=True); a = p.parse_args()
    if a.song == "666":
        print("  ERROR: could not fetch song"); sys.exit(1)
    print("\n[1/5] Fetching song metadata..."); print(f"  Song: Fake Artist - Song {a.song}")
    time.sleep(1); print("\n[4/5] Downloading YouTube audio...")
    print("  WARNING: Audio download failed: HTTP Error 403"); time.sleep(1)
    open(f"Fake Artist - Song {a.song}_synced.gp", "wb").write(b"PK\x03\x04fake")
    print("\nDone!")


if __name__ == "__main__":
    main()
