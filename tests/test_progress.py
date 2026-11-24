"""Offline test of the `progress` a running job reports, read from the end of its log."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import gpsync  # noqa: E402

HEAD = "[1/6] Fetching song metadata...\n  Song: x\n\n"
DOWNLOAD = "[4/6] Downloading YouTube audio (untrimmed).\n[download]   0.0% of 3MiB\r[download]  61.6% of 3.25MiB at 20MiB/s ETA 00:00"
SEPARATE = "\n\n[5/6] Separating stems in StemDeck...\n  StemDeck job abc\n  Downloading the separation model 69%\n  Separating 56%"


class Progress(unittest.TestCase):
    def test_nothing_before_the_first_step(self):
        self.assertIsNone(gpsync.progress_of(""))
        self.assertIsNone(gpsync.progress_of("starting"))

    def test_a_step_with_no_output_yet(self):
        p = gpsync.progress_of("[1/6] Fetching song metadata...\n")
        self.assertEqual((p["step"], p["steps"], p["stage"], p["percent"]), (1, 6, "Fetching song metadata", None))

    def test_a_download_line_gives_the_percentage_and_names_the_step(self):
        p = gpsync.progress_of(HEAD + DOWNLOAD)
        self.assertEqual((p["step"], p["percent"]), (4, 61.6))
        self.assertTrue(p["stage"].startswith("Downloading YouTube audio"))

    def test_stemdeck_stage_text_is_split_into_stage_and_percent(self):
        p = gpsync.progress_of(HEAD + DOWNLOAD + SEPARATE)
        self.assertEqual((p["step"], p["stage"], p["percent"]), (5, "Separating", 56.0))
        self.assertEqual(gpsync.progress_of(HEAD + DOWNLOAD + SEPARATE.replace("Separating 56%", "Downloading the separation model 69%"))["stage"],
                         "Downloading the separation model")

    def test_a_line_without_a_percentage(self):
        p = gpsync.progress_of(HEAD + DOWNLOAD + SEPARATE + "\n  Fetching vocals")
        self.assertEqual((p["stage"], p["percent"]), ("Fetching vocals", None))


if __name__ == "__main__":
    unittest.main()
