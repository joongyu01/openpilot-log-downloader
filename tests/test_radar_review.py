import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
"""Offline job-state and partial-download checks; never accesses a real device."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import radar_review as review


class Remote(io.BytesIO):
    headers = {"Content-Length": "100"}


class RadarReviewTests(unittest.TestCase):
    def setUp(self):
        review._jobs.clear()

    def tearDown(self):
        review._jobs.clear()

    def test_rejects_path_traversal_and_bad_options(self):
        self.assertEqual(review.response("127.0.0.1", "../private--1")[0], 400)
        self.assertEqual(review.response("127.0.0.1", "route--1", "invalid")[0], 400)

    @patch.object(review, "engine_root", return_value=Path("engine"))
    @patch.object(review.threading, "Thread")
    def test_failed_job_is_retryable_but_poll_does_not_restart(self, thread, _engine):
        review.response("127.0.0.1", "route--1")
        job = next(iter(review._jobs.values()))
        job.update(status="error", error="test error")
        self.assertEqual(review.response("127.0.0.1", "route--1")[0], 422)
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(review.response("127.0.0.1", "route--1", retry=True)[0], 202)
        self.assertEqual(thread.call_count, 2)

    @patch.object(review, "engine_root", return_value=Path("engine"))
    @patch.object(review.threading, "Thread")
    def test_ready_result_is_reused_and_pending_queue_is_bounded(self, thread, _engine):
        for i in range(4):
            self.assertEqual(review.response("127.0.0.1", f"route--{i}")[0], 202)
        self.assertEqual(review.response("127.0.0.1", "route--4")[0], 503)
        next(iter(review._jobs.values())).update(status="ready", payload={"frames": [1]})
        self.assertEqual(review.response("127.0.0.1", "route--0", retry=True), (200, {"frames": [1]}))
        self.assertEqual(thread.call_count, 4)

    def test_incomplete_download_never_reaches_exporter(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {}
            with patch.object(review, "CACHE", Path(directory)), \
                    patch.object(review.urllib.request, "urlopen", return_value=Remote(b"short")), \
                    patch.object(review.subprocess, "run") as run:
                review._prepare(job, Path("engine"), "127.0.0.1", "route--1", "auto", "recorded")
                self.assertEqual(job["status"], "error")
                self.assertIn("모두 받지 못했습니다", job["error"])
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
