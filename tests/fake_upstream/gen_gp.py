"""Stand-in for upstream gen_gp.py: a minimal zip shaped like a GP7/8 file."""
import zipfile

GPIF = "<GPIF><MasterBars>" + "<MasterBar><Time>4/4</Time></MasterBar>" * 8 + "</MasterBars></GPIF>"


def fetch_all_tracks(song_id):
    return {"id": song_id}, [{"name": "Guitar"}]


def generate_gp(tracks, output_path, meta=None):
    with zipfile.ZipFile(output_path, "w") as z:
        z.writestr("Content/score.gpif", GPIF)
