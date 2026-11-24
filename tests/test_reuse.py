#!/usr/bin/env python3
"""Offline test: a finished job is reused only when the current code made it. python3 tests/test_reuse.py"""
import json, os, subprocess, sys, tempfile, types, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, UPSTREAM_DIR=str(ROOT / "tests" / "fake_upstream"))
sys.path.insert(0, str(ROOT / "app"))
import gpsync  # noqa: E402


class Reuse(unittest.TestCase):
    def setUp(self):
        # gpsync reads DATA_DIR when first imported; another test module may have imported it before this one set the variable
        self._paths = (gpsync.DATA, gpsync.JOBS, gpsync.UPSTREAM)
        gpsync.DATA, gpsync.JOBS, gpsync.UPSTREAM = Path(DATA), Path(DATA) / "jobs", ROOT / "tests" / "fake_upstream"
        self.started = []
        self._popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: (self.started.append(a), types.SimpleNamespace(wait=lambda: 0))[1]   # do not run a real job

    def tearDown(self):
        subprocess.Popen = self._popen
        gpsync.DATA, gpsync.JOBS, gpsync.UPSTREAM = self._paths

    def done_job(self, jid, code):
        d = Path(DATA) / "jobs" / jid
        (d / "out").mkdir(parents=True)
        (d / "out" / "a.mpak").write_bytes(b"x")
        job = {"id": jid, "song": "7", "status": "done", "created": "2026-01-01T00:00:00Z",
               "file": "a.mpak"}
        if code:
            job["code"] = code
        (d / "job.json").write_text(json.dumps(job))

    def test_same_code_reuses_the_job(self):
        self.done_job("7-aaaaaa", gpsync.code_version())
        job, created = gpsync.create_job("7")
        self.assertEqual((job["id"], created), ("7-aaaaaa", False))
        self.assertEqual(self.started, [])

    def test_older_code_or_an_unstamped_job_runs_again(self):
        self.done_job("8-aaaaaa", "0000000000")
        self.done_job("9-aaaaaa", None)
        for song, old in (("8", "8-aaaaaa"), ("9", "9-aaaaaa")):
            d = Path(DATA) / "jobs" / old / "job.json"
            j = json.loads(d.read_text()); j["song"] = song; d.write_text(json.dumps(j))
            job, created = gpsync.create_job(song)
            self.assertTrue(created, song)
            self.assertNotEqual(job["id"], old)
            self.assertEqual(job["code"], gpsync.code_version())

    def test_the_version_changes_with_the_code(self):
        self.assertEqual(gpsync.code_version(), gpsync.code_version())
        self.assertEqual(len(gpsync.code_version()), 10)


if __name__ == "__main__":
    unittest.main()
