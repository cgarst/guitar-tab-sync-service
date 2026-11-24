#!/usr/bin/env python3
"""Offline test of app/sources.py: python3 tests/test_sources.py"""
import os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import sources  # noqa: E402


class Sources(unittest.TestCase):
    def upstream(self, text):
        d = Path(tempfile.mkdtemp())
        (d / "sync.py").write_text(text)
        return d

    def test_host_is_read_from_the_upstream_release(self):
        d = self.upstream('def fetch_song_meta(song_id):\n    url = f"https://tabs.example.test/api/meta/{song_id}"\n')
        self.assertEqual(sources.upstream_base(d), "https://tabs.example.test")

    def test_unreadable_or_unrecognised_upstream_gives_none(self):
        self.assertIsNone(sources.upstream_base(Path(tempfile.mkdtemp())))
        self.assertIsNone(sources.upstream_base(self.upstream("print('nothing here')")))

    def test_env_overrides_and_missing_host_is_an_error(self):
        old = os.environ.get("SOURCE_URL")
        try:
            os.environ["SOURCE_URL"] = "http://mirror.example.test/"
            self.assertEqual(sources.Source("x", "tab source", sources._default_base).base_url, "http://mirror.example.test")
            os.environ["SOURCE_URL"] = ""
            with self.assertRaises(sources.SourceError):
                sources.Source("x", "tab source", lambda: None).base_url
        finally:
            os.environ.pop("SOURCE_URL", None) if old is None else os.environ.update(SOURCE_URL=old)

    def test_parse(self):
        p = sources.source().parse
        self.assertEqual(p(" 0023063 "), "23063")
        self.assertEqual(p("https://tabs.example.test/a/wsa/x-tab-s61178"), "61178")
        self.assertIsNone(p("johann sebastian bach"))


if __name__ == "__main__":
    unittest.main()
