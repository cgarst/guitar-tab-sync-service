"""Builds a Music Pack (.mpak, https://github.com/cgarst/MusicPackSpec) for one song: the tab as the
score, the YouTube audio as the full mix, each separated stem as a recording of its own, and the source's bar times
as the sync file. Stdlib only.
"""
import hashlib
import json
import time
import uuid
import zipfile
from pathlib import Path

GENERATOR = {"name": "guitar-tab-sync-service"}
# SPEC section 4, "Pack identity": the namespace of an id derived from the primary score.
PACK_ID_NAMESPACE = uuid.UUID("03924935-9520-4f8c-91cd-ebd5f6cf57c5")
MIME = {"mp3": "audio/mpeg", "flac": "audio/flac"}
STEM_LABEL = {"vocals": "Vocals", "drums": "Drums", "bass": "Bass", "guitar": "Guitar",
              "piano": "Keys", "other": "Other"}
STEM_INSTRUMENT = {"piano": "keys"}  # the spec's suggested name; the rest are the same
_CHUNK = 1 << 20
_DATE = (1980, 1, 1, 0, 0, 0)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def build_sync(points, video_id, revision_id, recording="mix"):
    """Sync file for a recording: the source's bare bar times in seconds (SPEC section 9.1, "Bar times")."""
    doc = {"version": 1, "recording": recording, "score": "main", "anchorType": "bar",
           "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "points": list(points)}
    if revision_id is not None:
        doc["revisionId"] = revision_id
    if video_id:
        doc["videoId"] = video_id
    return doc


def build_manifest(*, title, artist, score_path, mix_path, stems, video_id, sync_bytes, mix_duration_ms=None, origin=None,
                   backing=None):
    """`stems` is a list of (name, Path) in pack order. Every path is a real file, so sizes and hashes are true.
    `backing`, when given, is (Path, video_id, sync_bytes, label): a second recording (role `backing`) with a sync file of its own."""
    score_sha = sha256_file(score_path)
    mix_source = {"type": "file", "path": f"audio/{mix_path.name}", "mime": MIME.get(mix_path.suffix.lstrip("."), "audio/mpeg"),
                  "size": mix_path.stat().st_size, "sha256": sha256_file(mix_path)}
    if mix_duration_ms:
        mix_source["durationMs"] = mix_duration_ms
    sources = [mix_source]
    if video_id:
        sources.append({"type": "youtube", "videoId": video_id})
    # The stems are the mix's stem layer (SPEC section 8): nested in it, so they share its sync and reach its YouTube
    # source through it, and a reader that supports stems plays them together by default.
    stem_layer = [{
        "id": name, "label": STEM_LABEL.get(name, name.title()),
        "stem": {"instrument": STEM_INSTRUMENT.get(name, name)},
        "sources": [{"type": "file", "path": f"audio/{path.name}", "mime": MIME.get(path.suffix.lstrip("."), "audio/mpeg"),
                     "size": path.stat().st_size, "sha256": sha256_file(path)}],
    } for name, path in stems]
    mix = {"id": "mix", "role": "original", "label": "Full mix", "sync": {"main": "sync/mix.json"}, "sources": sources}
    if stem_layer:
        mix["stems"] = stem_layer
        mix["stemsComplete"] = len(stems) == 6
    recordings = [mix]
    entries = {"sync/mix.json": {"size": len(sync_bytes), "sha256": sha256_bytes(sync_bytes)}}
    if backing:
        b_path, b_video, b_sync, b_label = backing
        b_sources = [{"type": "file", "path": f"audio/{b_path.name}", "mime": MIME.get(b_path.suffix.lstrip("."), "audio/mpeg"),
                      "size": b_path.stat().st_size, "sha256": sha256_file(b_path)}]
        if b_video:
            b_sources.append({"type": "youtube", "videoId": b_video})
        recordings.append({"id": "backing", "role": "backing", "label": b_label, "sync": {"main": "sync/backing.json"},
                           "sources": b_sources})
        entries["sync/backing.json"] = {"size": len(b_sync), "sha256": sha256_bytes(b_sync)}
    score = {"id": "main", "path": f"score/{score_path.name}", "format": "gp", "name": score_path.name,
             "sha256": score_sha, "size": score_path.stat().st_size}
    if origin:  # SPEC section 6.4: where the score came from, a label for people; left out when unknown
        score["origin"] = origin
    song = {k: v for k, v in (("title", title), ("artist", artist)) if v}
    return {
        "format": "mpak", "version": 1, "id": str(uuid.uuid5(PACK_ID_NAMESPACE, score_sha)),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "generator": GENERATOR,
        "song": song,
        "scores": [score],
        "recordings": recordings,
        "entries": entries,
    }


def json_bytes(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_mpak(out_path, manifest, sync_bytes, score_path, mix_path, stems, backing=None):
    """Manifest first, audio Stored (it is already compressed), the rest Deflated. ZIP64 only when a file needs it."""
    files = [(f"score/{score_path.name}", score_path, False), (f"audio/{mix_path.name}", mix_path, True)]
    files += [(f"audio/{p.name}", p, True) for _, p in stems]
    if backing:
        files.append((f"audio/{backing[0].name}", backing[0], True))
    tmp = Path(str(out_path) + ".part")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        def add_bytes(name, data):
            info = zipfile.ZipInfo(name, _DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
        add_bytes("manifest.json", json_bytes(manifest))
        add_bytes("sync/mix.json", sync_bytes)
        if backing:
            add_bytes("sync/backing.json", backing[2])
        for name, path, store in files:
            info = zipfile.ZipInfo(name, _DATE)
            info.compress_type = zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            size = path.stat().st_size
            with open(path, "rb") as src, zf.open(info, "w", force_zip64=size >= (1 << 31)) as dst:
                while chunk := src.read(_CHUNK):
                    dst.write(chunk)
    tmp.replace(out_path)
