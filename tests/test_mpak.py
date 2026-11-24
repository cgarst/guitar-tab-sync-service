#!/usr/bin/env python3
"""Offline tests for MPAK output: python3 tests/test_mpak.py
Uses tests/fake_upstream and tests/fake_stemdeck.py. The pack is also validated with the reference tool from the public
MusicPackSpec repository, which is cloned once into the system temp directory (needs git, node 22+, npm and network;
the validation is skipped when any is missing)."""
import urllib.request
import fcntl, hashlib, json, os, shutil, socket, subprocess, sys, tempfile, time, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
FAKE_UP = ROOT / "tests" / "fake_upstream_raw"
SPEC_REPO = "https://github.com/cgarst/MusicPackSpec.git"
SPEC = Path(tempfile.gettempdir()) / "MusicPackSpec-reference"


def spec_cli():
    """Return the path of the reference validator from the public repo, cloning it on first use; None if unavailable."""
    cli = SPEC / "reference" / "js" / "bin" / "mpak.js"
    js = SPEC / "reference" / "js"
    try:
        if not cli.is_file():
            shutil.rmtree(SPEC, ignore_errors=True)
            subprocess.run(["git", "clone", "--depth", "1", SPEC_REPO, str(SPEC)], check=True, capture_output=True, timeout=120)
        if not (js / "node_modules").is_dir():
            subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=js, check=True, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    return cli
STEMS = ["vocals", "drums", "bass", "guitar", "piano", "other"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.sd = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_stemdeck.py"), str(cls.port)])
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.sd.terminate()
        cls.sd.wait()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/reset", data=b"", method="POST")).close()

    def env(self, **extra):
        return {**os.environ, "UPSTREAM_DIR": str(FAKE_UP), "STEMDECK_URL": f"http://127.0.0.1:{self.port}",
                "PYTHONUNBUFFERED": "1", **extra}

    def job(self, song, **extra):
        out = self.tmp / "out"
        out.mkdir(exist_ok=True)
        p = subprocess.run([sys.executable, str(APP / "mpak_job.py"), "--song", song], cwd=out,
                           capture_output=True, text=True, env=self.env(**extra), timeout=60)
        return p, out


class MpakJob(Base):
    """One job makes every output that applies: the Guitar Pro file, a full band + backing pack and a stems pack."""

    def names(self, out):
        return sorted(x.name for x in out.iterdir())

    def test_everything_that_applies(self):
        p, out = self.job("23063", SOURCE_URL="https://tabs.example.test")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.names(out), ["Fake Artist - Song 23063 (full band + backing).mpak",
                                           "Fake Artist - Song 23063 (stems).mpak", "Fake Artist - Song 23063_synced.gp"])
        self.assertFalse((out / "work").exists(), "temp files are cleaned up")
        self.assertIn("[4/4]", p.stdout)
        stems_pack = out / "Fake Artist - Song 23063 (stems).mpak"
        z = zipfile.ZipFile(stems_pack)
        self.assertEqual(z.namelist()[0], "manifest.json")
        m = json.loads(z.read("manifest.json"))
        self.assertEqual(m["song"], {"title": "Song 23063", "artist": "Fake Artist"})
        score = m["scores"][0]
        self.assertEqual(hashlib.sha256(z.read(score["path"])).hexdigest(), score["sha256"])
        self.assertEqual(score["size"], len(z.read(score["path"])))
        self.assertEqual(score["origin"], "tabs.example.test", "origin is the source's host name (SPEC 6.4)")
        self.assertEqual(len(m["recordings"]), 1, "the stems are the mix's stem layer, not recordings of their own")
        mix = m["recordings"][0]
        stems = mix["stems"]
        self.assertEqual((mix["role"], mix["stemsComplete"]), ("original", True))
        self.assertEqual([s["type"] for s in mix["sources"]], ["file", "youtube"])
        self.assertEqual(mix["sources"][1]["videoId"], "AAAAAAAAAAA")
        self.assertEqual([s["id"] for s in stems], STEMS)
        for s in stems:
            self.assertNotIn("role", s)
            self.assertNotIn("stemOf", s)
            src = s["sources"][0]
            self.assertEqual(src["mime"], "audio/mpeg")
            self.assertEqual(hashlib.sha256(z.read(src["path"])).hexdigest(), src["sha256"])
        self.assertEqual({s["id"]: s["stem"]["instrument"] for s in stems}["piano"], "keys")
        sync = json.loads(z.read("sync/mix.json"))
        self.assertEqual((sync["anchorType"], sync["revisionId"], sync["videoId"]), ("bar", 1234, "AAAAAAAAAAA"))
        self.assertEqual(sync["points"][0], 0.5)
        self.assertEqual(len(sync["points"]), 8)
        entry = m["entries"]["sync/mix.json"]
        self.assertEqual(entry["sha256"], hashlib.sha256(z.read("sync/mix.json")).hexdigest())
        self.validate_against_spec(stems_pack)

    def test_full_band_and_backing_pack_has_a_sync_file_for_each(self):
        p, out = self.job("23063")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pack = out / "Fake Artist - Song 23063 (full band + backing).mpak"
        z = zipfile.ZipFile(pack)
        m = json.loads(z.read("manifest.json"))
        mix, backing = m["recordings"]
        self.assertEqual((mix["id"], mix["role"]), ("mix", "original"))
        self.assertNotIn("stems", mix)
        self.assertEqual((backing["id"], backing["role"]), ("backing", "backing"))
        self.assertEqual(backing["sources"][1]["videoId"], "BBBBBBBBBBB")
        self.assertEqual(mix["sync"], {"main": "sync/mix.json"})
        self.assertEqual(backing["sync"], {"main": "sync/backing.json"})
        for name, first in (("sync/mix.json", 0.5), ("sync/backing.json", 1.0)):
            doc = json.loads(z.read(name))
            self.assertEqual(doc["points"][0], first)
            self.assertEqual(doc["recording"], name.split("/")[1].split(".")[0])
            self.assertEqual(m["entries"][name]["sha256"], hashlib.sha256(z.read(name)).hexdigest())
        src = backing["sources"][0]
        self.assertEqual(hashlib.sha256(z.read(src["path"])).hexdigest(), src["sha256"])
        self.validate_against_spec(pack)

    def validate_against_spec(self, pack):
        node = shutil.which("node")
        cli = spec_cli() if node else None
        if not cli:
            self.skipTest("MusicPackSpec reference tool not available (pack content was still checked)")
        r = subprocess.run([node, str(cli), "validate", str(pack)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("warning", r.stderr)

    def test_no_stemdeck_address_skips_the_stems(self):
        p, out = self.job("23063", STEMDECK_URL="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("stems skipped", p.stdout)
        self.assertEqual(self.names(out), ["Fake Artist - Song 23063 (full band + backing).mpak", "Fake Artist - Song 23063_synced.gp"])

    def test_stemdeck_turned_off_skips_the_stems(self):
        p, out = self.job("23063", STEMDECK_ENABLED="0")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("stems skipped", p.stdout)
        self.assertFalse(list(out.glob("*(stems).mpak")))

    def test_unreachable_stemdeck_skips_the_stems(self):
        p, out = self.job("23063", STEMDECK_URL=f"http://127.0.0.1:{free_port()}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("not answering", p.stdout)
        self.assertEqual(self.names(out), ["Fake Artist - Song 23063 (full band + backing).mpak", "Fake Artist - Song 23063_synced.gp", "work"],
                         "the audio is kept so the stems can be retried")

    def test_backing_is_the_one_without_the_default_track(self):
        p, out = self.job("999")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pack = out / "Fake Artist - Song 999 (full band + backing).mpak"
        backing = json.loads(zipfile.ZipFile(pack).read("manifest.json"))["recordings"][1]
        self.assertEqual(backing["sources"][1]["videoId"], "EEEEEEEEEEE")

    def test_no_backing_video_skips_that_pack(self):
        p, out = self.job("556")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("no backing video", p.stdout)
        self.assertEqual(self.names(out), ["Fake Artist - Song 556 (stems).mpak", "Fake Artist - Song 556_synced.gp"])

    def test_a_failed_separation_keeps_the_other_outputs(self):
        p, out = self.job("888")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("separation blew up (out of memory)", p.stdout, "StemDeck's error_detail names the cause")
        self.assertEqual(self.names(out), ["Fake Artist - Song 888 (full band + backing).mpak", "Fake Artist - Song 888_synced.gp", "work"])
        self.assertTrue((out / "work" / "resume.json").is_file(), "the audio is kept for a retry")

    def test_resume_separates_and_packs_again_from_the_kept_audio(self):
        p, out = self.job("888")
        self.assertIn("stems skipped", p.stdout)
        r = subprocess.run([sys.executable, str(APP / "mpak_job.py"), "--resume"], cwd=out, capture_output=True, text=True,
                           env=self.env(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[3/4]", r.stdout)
        self.assertIn("[4/4]", r.stdout)
        self.assertEqual(self.names(out), ["Fake Artist - Song 888 (full band + backing).mpak", "Fake Artist - Song 888 (stems).mpak",
                                           "Fake Artist - Song 888_synced.gp"], "the stems pack is added and the temp files are gone")
        m = json.loads(zipfile.ZipFile(out / "Fake Artist - Song 888 (stems).mpak").read("manifest.json"))
        self.assertEqual([s["id"] for s in m["recordings"][0]["stems"]], STEMS)

    def test_resume_without_the_kept_audio_says_so(self):
        out = self.tmp / "out"
        out.mkdir()
        r = subprocess.run([sys.executable, str(APP / "mpak_job.py"), "--resume"], cwd=out, capture_output=True, text=True,
                           env=self.env(), timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no longer there", r.stdout + r.stderr)

    def test_audio_failure_fails_the_job(self):
        p, out = self.job("777", GPSYNC_RETRY_PAUSE="0")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Audio download failed", p.stdout + p.stderr)
        self.assertEqual(list(out.glob("*.mpak")), [])

    def test_a_transient_download_failure_is_retried(self):
        p, out = self.job("778", GPSYNC_RETRY_PAUSE="0")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("trying again (2/3)", p.stdout)
        self.assertEqual(len(list(out.glob("*.mpak"))), 2)


class MpakJobRaw(Base):
    """Upstream's --raw output (audio, sync JSON, synced file) is what the packs are made from."""

    def raw_job(self, song, **extra):
        return self.job(song, GPSYNC_RETRY_PAUSE="0", **extra)

    def test_uses_upstreams_raw_output(self):
        p, out = self.raw_job("23063")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(x.name for x in out.iterdir()),
                         ["Fake Artist - Song 23063 (full band + backing).mpak", "Fake Artist - Song 23063 (stems).mpak",
                          "Fake Artist - Song 23063_synced.gp"])
        self.assertEqual((out / "Fake Artist - Song 23063_synced.gp").read_bytes(), b"PK\x03\x04synced-by-upstream")
        self.assertIn("[4/4]", p.stdout)
        self.assertNotIn("[1/5]", p.stdout, "upstream's own headings must not read as this job's progress")
        pack = out / "Fake Artist - Song 23063 (full band + backing).mpak"
        z = zipfile.ZipFile(pack)
        mix, backing = json.loads(z.read("manifest.json"))["recordings"]
        self.assertEqual(mix["sources"][1]["videoId"], "AAAAAAAAAAA")
        self.assertEqual(backing["sources"][1]["videoId"], "BBBBBBBBBBB")
        self.assertEqual(json.loads(z.read("sync/mix.json"))["points"][0], 0.5)
        self.assertEqual(json.loads(z.read("sync/backing.json"))["points"][0], 1.0)
        self.assertEqual(z.read(mix["sources"][0]["path"]), b"ID3fake-mp3-AAAAAAAAAAA", "the audio is the untrimmed raw file")

    def test_backing_is_the_one_without_the_default_track(self):
        p, out = self.raw_job("999")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pack = out / "Fake Artist - Song 999 (full band + backing).mpak"
        backing = json.loads(zipfile.ZipFile(pack).read("manifest.json"))["recordings"][1]
        self.assertEqual(backing["sources"][1]["videoId"], "EEEEEEEEEEE")
        self.assertEqual(backing["label"], "Backing (without Guitar)")

    def test_no_backing_video_skips_that_pack(self):
        p, out = self.raw_job("556")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(list(out.glob("*.mpak"))), 1)

    def test_no_audio_from_upstream_fails_the_job(self):
        p, out = self.raw_job("777")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Audio download failed", p.stdout + p.stderr)


class GpsyncRunner(Base):
    """gpsync.py's detached runner: result handling, and the sync lock being released mid-job."""

    def run_job(self, song, **extra):
        data = self.tmp / "data"
        job_id = f"{song}-abc123"
        (data / "jobs" / job_id / "out").mkdir(parents=True)
        job = {"id": job_id, "song": song, "status": "queued", "created": "2026-01-01T00:00:00Z"}
        (data / "jobs" / job_id / "job.json").write_text(json.dumps(job))
        env = self.env(DATA_DIR=str(data), UPSTREAM_PYTHON=sys.executable, MPAK_JOB_TIMEOUT="120", **extra)
        proc = subprocess.Popen([sys.executable, str(APP / "gpsync.py"), "run", job_id], env=env)
        return data, job_id, proc

    def read(self, data, job_id):
        return json.loads((data / "jobs" / job_id / "job.json").read_text())

    def view(self, data, job_id, command="api-job", **extra):
        env = {**os.environ, "DATA_DIR": str(data), "PUBLIC_BASE_URL": "http://h", "v_id": job_id,
               "STEMDECK_URL": f"http://127.0.0.1:{self.port}", **extra}
        out = subprocess.run([sys.executable, str(ROOT / "tests" / "cgi_adapter.py"), command], capture_output=True, text=True, env=env).stdout
        return out.split("\n\n", 1)[-1]

    BEST_FIRST = ["Fake Artist - Song 23063 (stems).mpak", "Fake Artist - Song 23063 (full band + backing).mpak",
                  "Fake Artist - Song 23063_synced.gp"]

    def test_done_and_lock_released_during_stem_wait(self):
        data, job_id, proc = self.run_job("23063")
        log = data / "jobs" / job_id / "log.txt"
        released = False
        for _ in range(200):
            if log.exists() and "[3/4]" in log.read_text():
                with open(data / "sync.lock", "w") as f:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        released = True
                    except BlockingIOError:
                        pass
                break
            time.sleep(0.05)
        proc.wait(timeout=60)
        self.assertTrue(released, "sync lock was still held while waiting for StemDeck")
        job = self.read(data, job_id)
        self.assertEqual((job["status"], job["audio"]), ("done", True))
        self.assertEqual(job["name"], "Fake Artist - Song 23063")
        self.assertEqual(job["files"], self.BEST_FIRST, "every output, best first")
        self.assertEqual(job["file"], self.BEST_FIRST[0])

    def test_job_view_lists_the_outputs_good_better_best(self):
        data, job_id, proc = self.run_job("23063")
        proc.wait(timeout=60)
        v = json.loads(self.view(data, job_id))
        self.assertNotIn("kind", v)
        self.assertEqual([f["name"] for f in v["files"]], self.BEST_FIRST)
        self.assertNotIn("name=", v["files"][0]["file_url"])
        self.assertIn("name=Fake%20Artist%20-%20Song%2023063_synced.gp", v["files"][2]["file_url"])
        self.assertEqual([(o["tier"], o["available"]) for o in v["outputs"]], [("good", True), ("better", True), ("best", True)])

    def test_job_shows_unavailable_stems_with_the_reason(self):
        data, job_id, proc = self.run_job("23063", STEMDECK_URL="")
        proc.wait(timeout=60)
        v = json.loads(self.view(data, job_id))
        self.assertEqual([(o["tier"], o["available"]) for o in v["outputs"]], [("good", True), ("better", True), ("best", False)])
        self.assertIn("not configured or is turned off", v["outputs"][2]["reason"])
        page = self.view(data, job_id, "job", QUERY_STRING=f"id={job_id}")
        self.assertIn('<h3>Good<span class="kind">GP</span></h3>', page)
        self.assertNotIn('aria-label="Download MPAK with Stems"', page, "an output that was not made has no download button")
        self.assertLess(page.index("MPAK with Full Band + Backing"), page.index("MPAK with Stems"), "listed good, better, best")
        self.assertIn("Not made: StemDeck is not configured or is turned off", page)

    def test_a_failed_job_page_offers_retry_and_a_good_one_does_not(self):
        data, job_id, proc = self.run_job("777", GPSYNC_RETRY_PAUSE="0")
        proc.wait(timeout=60)
        page = self.view(data, job_id, "job", QUERY_STRING=f"id={job_id}")
        self.assertIn('action="/get"', page)
        self.assertIn('name="song" value="777"', page)
        self.assertIn(f'name="retry" value="{job_id}"', page)
        self.assertIn(">Retry<", page)
        data, job_id, proc = self.run_job("23063")
        proc.wait(timeout=60)
        self.assertNotIn(">Retry<", self.view(data, job_id, "job", QUERY_STRING=f"id={job_id}"))

    def test_retry_stems_on_a_done_job_whose_separation_failed(self):
        data, job_id, proc = self.run_job("888")
        proc.wait(timeout=60)
        env = {**os.environ, "DATA_DIR": str(data), "PUBLIC_BASE_URL": "http://h"}
        v = json.loads(self.view(data, job_id))
        self.assertEqual((v["status"], v["can_retry_stems"], [(o["tier"], o["available"]) for o in v["outputs"]]),
                         ("done", True, [("good", True), ("better", True), ("best", False)]))
        self.assertIn("out of memory", v["outputs"][2]["reason"])
        page = self.view(data, job_id, "job", QUERY_STRING=f"id={job_id}")
        self.assertIn('action="/retry-stems"', page)
        self.assertIn(f'name="id" value="{job_id}"', page)
        # Retry: the detached runner finishes the job again with the stems pack added.
        run = subprocess.run([sys.executable, str(ROOT / "tests" / "cgi_adapter.py"), "api-retry-stems"], capture_output=True, text=True,
                             env={**env, "v_id": job_id, "UPSTREAM_PYTHON": sys.executable, "STEMDECK_URL": f"http://127.0.0.1:{self.port}"})
        self.assertIn("202", run.stdout.split("\n\n")[0], run.stdout + run.stderr)
        for _ in range(300):
            job = self.read(data, job_id)
            if job["status"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["files"], ["Fake Artist - Song 888 (stems).mpak", "Fake Artist - Song 888 (full band + backing).mpak",
                                        "Fake Artist - Song 888_synced.gp"])
        v = json.loads(self.view(data, job_id))
        self.assertEqual([(o["tier"], o["available"]) for o in v["outputs"]], [("good", True), ("better", True), ("best", True)])
        self.assertFalse(v["can_retry_stems"])
        self.assertNotIn('action="/retry-stems"', self.view(data, job_id, "job", QUERY_STRING=f"id={job_id}"))

    def test_no_retry_when_stems_are_off_or_the_job_is_not_done(self):
        data, job_id, proc = self.run_job("23063")
        proc.wait(timeout=60)
        self.assertFalse(json.loads(self.view(data, job_id))["can_retry_stems"], "a job with stems has nothing to retry")
        out = subprocess.run([sys.executable, str(ROOT / "tests" / "cgi_adapter.py"), "api-retry-stems"], capture_output=True, text=True,
                             env={**os.environ, "DATA_DIR": str(data), "v_id": job_id})
        self.assertIn("409", out.stdout.split("\n\n")[0])

    def test_failed_job_reports_last_log_line(self):
        data, job_id, proc = self.run_job("777", GPSYNC_RETRY_PAUSE="0")
        proc.wait(timeout=60)
        job = self.read(data, job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Audio download failed", job["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
