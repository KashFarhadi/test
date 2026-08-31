#!/usr/bin/env python3
"""Fetch the exact target transcript ten times and require stable evidence."""

from __future__ import annotations

import hashlib
import json
import time

from extract_transcript import Segment, validate_segments
from probe_summynews import fetch_json

VIDEO_ID = "ILtQtKuH84Q"
ATTEMPTS = 10


def canonical_digest(data: dict) -> tuple[str, list[Segment]]:
    segments = [
        Segment(
            start=float(item["start"]),
            end=float(item["start"]) + max(0.0, float(item.get("duration", 0.0))),
            text=str(item.get("text", "")).strip(),
        )
        for item in data.get("transcript", [])
        if str(item.get("text", "")).strip()
    ]
    canonical = json.dumps(
        [segment.to_dict() for segment in segments],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), segments


def main() -> int:
    results: list[dict] = []
    digests: set[str] = set()
    for index in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        try:
            data = fetch_json(VIDEO_ID, attempts=3)
            digest, segments = canonical_digest(data)
            validation = validate_segments(
                segments,
                duration_seconds=1064.16,
                minimum_characters=18000,
            )
            passed = bool(validation["passed"])
            if passed:
                digests.add(digest)
            result = {
                "attempt": index,
                "passed": passed,
                "digest": digest,
                "segment_count": len(segments),
                "character_count": validation.get("character_count"),
                "last_end_seconds": validation.get("last_end_seconds"),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error": None,
            }
        except Exception as exc:
            result = {
                "attempt": index,
                "passed": False,
                "digest": None,
                "segment_count": 0,
                "character_count": 0,
                "last_end_seconds": None,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error": str(exc),
            }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    passed_count = sum(bool(result["passed"]) for result in results)
    stable = len(digests) == 1
    summary = {
        "video_id": VIDEO_ID,
        "requested_attempts": ATTEMPTS,
        "passed_attempts": passed_count,
        "all_passed": passed_count == ATTEMPTS,
        "stable_transcript": stable,
        "unique_digests": sorted(digests),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    with open("provider-artifacts/repeat-target-summary.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return 0 if summary["all_passed"] and stable else 1


if __name__ == "__main__":
    raise SystemExit(main())
