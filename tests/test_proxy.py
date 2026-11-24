"""YOUTUBE_PROXY_URL: the job's environment sends YouTube through the proxy and keeps the tab source and StemDeck direct."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
os.environ["SOURCE_URL"] = "https://tabs.example.test"
import gpsync  # noqa: E402


class ProxyEnv(unittest.TestCase):
    def test_unset_gives_nothing(self):
        self.assertEqual(gpsync.proxy_env({}), {})

    def test_proxy_with_direct_hosts(self):
        e = gpsync.proxy_env({"YOUTUBE_PROXY_URL": "http://u:p@home:18888",
                              "STEMDECK_URL": "http://stemdeck:8000"})
        self.assertEqual(e["HTTPS_PROXY"], "http://u:p@home:18888")
        self.assertEqual(e["https_proxy"], "http://u:p@home:18888")
        for h in ("localhost", "stemdeck", "tabs.example.test"):
            self.assertIn(h, e["NO_PROXY"].split(","))


if __name__ == "__main__":
    unittest.main()
