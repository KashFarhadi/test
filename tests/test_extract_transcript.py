import tempfile
import unittest
from pathlib import Path

from extract_transcript import (
    Segment,
    canonical_url,
    full_text_from_segments,
    parse_vtt,
    resolve_video_id,
    validate_segments,
)


class ResolveVideoIdTests(unittest.TestCase):
    def test_common_url_forms(self):
        expected = "dQw4w9WgXcQ"
        values = [
            expected,
            f"https://youtu.be/{expected}?si=abc",
            f"https://www.youtube.com/watch?v={expected}&t=2",
            f"https://youtube.com/shorts/{expected}",
            f"https://www.youtube-nocookie.com/embed/{expected}",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(resolve_video_id(value), expected)
                self.assertEqual(canonical_url(value), f"https://www.youtube.com/watch?v={expected}")


class VttTests(unittest.TestCase):
    def test_vtt_parser_and_rolling_text_deduplication(self):
        content = """WEBVTT

00:00:00.000 --> 00:00:01.000
Hello

00:00:01.000 --> 00:00:02.000
Hello world

00:00:02.000 --> 00:00:03.000
world again
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.vtt"
            path.write_text(content, encoding="utf-8")
            segments = parse_vtt(path)
        self.assertEqual(len(segments), 3)
        self.assertEqual(full_text_from_segments(segments), "Hello world again")


class ValidationTests(unittest.TestCase):
    def test_valid_segments_pass(self):
        segments = [
            Segment(0.0, 1.0, "Hello world"),
            Segment(1.0, 2.0, "This is a test transcript"),
        ]
        result = validate_segments(
            segments,
            duration_seconds=2.0,
            minimum_characters=10,
            processed_duration_seconds=2.0,
        )
        self.assertTrue(result["passed"])

    def test_non_monotonic_segments_fail(self):
        segments = [Segment(2.0, 3.0, "Later"), Segment(1.0, 2.0, "Earlier")]
        result = validate_segments(
            segments,
            duration_seconds=3.0,
            minimum_characters=5,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("previous" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
