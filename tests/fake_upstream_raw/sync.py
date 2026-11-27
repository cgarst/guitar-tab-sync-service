"""Stand-in for an upstream whose sync.py has the --raw flag. Everything else is fake_upstream's sync.py.

As a script, `--raw` writes <name>.gp, <name>_raw.mp3, <name>_sync.json and <name>_synced.gp in the current directory.
"""
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import quote

_path = Path(__file__).resolve().parent.parent / "fake_upstream" / "sync.py"
_spec = importlib.util.spec_from_file_location("fake_upstream_sync", _path)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
globals().update({k: v for k, v in vars(_base).items() if not k.startswith("__")})


def main():
    argv = sys.argv[1:]
    assert "--raw" in argv, "this fake only models --raw"  # the flag text also marks this upstream as supporting it
    song = argv[argv.index("--song") + 1]
    if song == "777":
        print("  WARNING: Audio download failed: HTTP Error 403\n  Continuing without audio...")
        return  # carries on without audio, like the real one
    meta = fetch_song_meta(song)
    stem = f"{meta['artist']} - {meta['title']}"
    entries = fetch_video_points(song, meta["revisionId"])
    print("\n[1/5] Fetching song metadata...")
    import gen_gp
    gen_gp.generate_gp([], Path(f"{stem}.gp"))
    raw_stem = f"{stem}_{quote(entries[0]['videoId'], safe='')}"
    download_youtube_audio(entries[0]["videoId"], Path(f"{raw_stem}_raw.mp3"))
    categories = {}
    for e in entries[1:]:
        categories.setdefault(e["feature"], []).append(
            {"videoId": e["videoId"], "tracks": e.get("tracks"), **({"without": e["without"]} if "without" in e else {}),
             "points": e["points"]})
    Path(f"{raw_stem}_sync.json").write_text(json.dumps(
        {"videoId": entries[0]["videoId"], "revisionId": meta["revisionId"], "artist": meta["artist"],
         "title": meta["title"], "defaultTrack": meta.get("defaultTrack"),
         "popularTrackGuitar": meta.get("popularTrackGuitar"), "points": entries[0]["points"],
         "categories": categories}))
    Path(f"{stem}_synced.gp").write_bytes(b"PK\x03\x04synced-by-upstream")
    print("\n[5/5] Syncing GP file...\nDone!")


if __name__ == "__main__":
    main()
