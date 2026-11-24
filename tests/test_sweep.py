#!/usr/bin/env python3
"""Offline test of the storage sweep (FILE_TTL_MINUTES / JOB_TTL_MINUTES)."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Sweep(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())

    def job(self, jid, status, finished, files=("a.mpak", "a_synced.gp")):
        d = self.data / "jobs" / jid
        (d / "out").mkdir(parents=True)
        for f in files:
            (d / "out" / f).write_bytes(b"x")
        (d / "job.json").write_text(json.dumps({"id": jid, "song": "1", "kind": "mpak", "status": status,
                                                "created": finished, "finished": finished, "file": "a.mpak"}))
        return d

    def sweep(self, file_min="1440", job_min="10080"):
        env = {**os.environ, "DATA_DIR": str(self.data), "FILE_TTL_MINUTES": file_min, "JOB_TTL_MINUTES": job_min}
        subprocess.run([sys.executable, str(ROOT / "app/gpsync.py"), "sweep"], env=env, check=True)

    def read(self, jid):
        return json.loads((self.data / "jobs" / jid / "job.json").read_text())

    def test_files_go_first_then_the_record(self):
        import time
        ago = lambda h: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - h * 3600))
        fresh, old, ancient = self.job("1-aaaaaa", "done", ago(1)), self.job("1-bbbbbb", "done", ago(30)), self.job("1-cccccc", "done", ago(24 * 8))
        running = self.job("1-dddddd", "running", ago(24 * 30))
        self.sweep()
        self.assertEqual(len(list((fresh / "out").iterdir())), 2)
        self.assertEqual(list((old / "out").iterdir()), [])
        self.assertIn("purged", self.read("1-bbbbbb"))
        self.assertFalse(ancient.exists(), "the record goes after JOB_TTL_MINUTES")
        self.assertEqual(len(list((running / "out").iterdir())), 2, "running jobs are never touched")

    def test_zero_keeps_everything(self):
        d = self.job("1-bbbbbb", "done", "2020-01-01T00:00:00Z")
        self.sweep("0", "0")
        self.assertEqual(len(list((d / "out").iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
