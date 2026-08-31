import hashlib
import json
import unittest

from reliable_transcript import canonical_segment_digest, parse_provider_segments


class ProviderParsingTests(unittest.TestCase):
    def test_parses_timestamped_provider_payload(self):
        segments = parse_provider_segments(
            {
                "transcript": [
                    {"start": 0, "duration": 1.25, "text": " Hello   world "},
                    {"start": "1.25", "duration": "2.0", "text": "Next line"},
                ]
            }
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].text, "Hello world")
        self.assertEqual(segments[1].end, 3.25)

    def test_digest_is_stable(self):
        segments = parse_provider_segments(
            {"transcript": [{"start": 0, "duration": 1, "text": "Stable"}]}
        )
        first = canonical_segment_digest(segments)
        second = canonical_segment_digest(segments)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)


if __name__ == "__main__":
    unittest.main()
