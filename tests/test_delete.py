#!/usr/bin/env python3
"""Deleting a job from the list deletes its record, log and files."""
import json, os, subprocess, sys, tempfile, unittest, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


class Delete(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, True)

    def make(self, jid, status):
        d = self.data / "jobs" / jid
        (d / "out").mkdir(parents=True)
        (d / "out" / "a.mpak").write_bytes(b"x")
        (d / "log.txt").write_text("log")
        (d / "job.json").write_text(json.dumps({"id": jid, "song": jid.split("-")[0], "kind": "mpak", "status": status,
                                                "created": "2026-01-01T00:00:00Z", "file": "a.mpak"}))
        return d

    def call(self, cmd, **v):
        env = {**os.environ, "DATA_DIR": str(self.data), **{f"v_{k}": x for k, x in v.items()}}
        r = subprocess.run([sys.executable, str(ROOT / "tests" / "cgi_adapter.py"), cmd], capture_output=True, text=True, env=env)
        head, _, body = r.stdout.partition("\n\n")
        return head, body

    def test_page_delete_removes_everything_and_redirects(self):
        d = self.make("7-aaaaaa", "done")
        head, _ = self.call("delete", id="7-aaaaaa")
        self.assertIn("303", head)
        self.assertIn("Location: /", head)
        self.assertFalse(d.exists())

    def test_api_delete(self):
        d = self.make("7-aaaaaa", "failed")
        head, body = self.call("api-delete", id="7-aaaaaa")
        self.assertEqual(json.loads(body), {"deleted": "7-aaaaaa"})
        self.assertFalse(d.exists())
        head, body = self.call("api-delete", id="7-aaaaaa")
        self.assertIn("404", head)

    def test_retry_replaces_a_failed_job_but_not_a_done_one(self):
        failed, done = self.make("7-aaaaaa", "failed"), self.make("8-bbbbbb", "done")
        job = json.loads((failed / "job.json").read_text())
        (failed / "job.json").write_text(json.dumps({**job, "name": "Fake Artist - Song 7"}))
        head, _ = self.call("start", song="7", retry="7-aaaaaa")
        self.assertIn("303", head)
        new = head.split("Location: /job?id=")[1].split()[0]
        self.assertNotEqual(new, "7-aaaaaa")
        self.assertFalse(failed.exists())
        self.assertTrue((self.data / "jobs" / new).exists())
        self.assertEqual(json.loads((self.data / "jobs" / new / "job.json").read_text())["name"], "Fake Artist - Song 7")
        self.call("start", song="9", retry="8-bbbbbb")
        self.assertTrue(done.exists(), "only a failed job is replaced")

    def test_a_running_job_is_kept(self):
        d = self.make("7-aaaaaa", "running")
        head, body = self.call("api-delete", id="7-aaaaaa")
        self.assertIn("409", head)
        self.assertTrue(d.exists())
        head, _ = self.call("delete", id="7-aaaaaa")
        self.assertIn("409", head)
        self.assertTrue(d.exists())

    def test_delete_all_keeps_running_jobs(self):
        done, failed, running = self.make("7-aaaaaa", "done"), self.make("8-bbbbbb", "failed"), self.make("9-cccccc", "running")
        head, _ = self.call("delete-all")
        self.assertIn("303", head)
        self.assertEqual((done.exists(), failed.exists(), running.exists()), (False, False, True))

    def test_delete_all_button_only_with_something_to_clear(self):
        self.make("9-cccccc", "running")
        self.assertNotIn("/delete-all", self.call("index")[1])
        self.make("7-aaaaaa", "done")
        self.assertIn('action="/delete-all"', self.call("index")[1])

    def test_a_bad_id_deletes_nothing(self):
        keep = self.make("7-aaaaaa", "done")
        for bad in ("../jobs", "7-aaaaaa/../..", ""):
            self.call("api-delete", id=bad)
        self.assertTrue(keep.exists())

    def test_the_list_offers_delete_for_finished_jobs_only(self):
        self.make("7-aaaaaa", "done")
        self.make("8-bbbbbb", "running")
        _, body = self.call("index")
        self.assertEqual(body.count('action="/delete"'), 1)
        self.assertIn("<form class=del method=\"post\" action=\"/delete\"", body)   # hidden until Edit is on
        self.assertIn('id="edit"', body)
        self.assertIn("Edit", body)
        self.assertIn('name="id" value="7-aaaaaa"', body)


if __name__ == "__main__":
    unittest.main()
