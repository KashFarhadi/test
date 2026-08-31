#!/usr/bin/env python3
"""Probe SumMyNews timestamped captions across the live ten-video matrix."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from extract_transcript import Segment, resolve_video_id, validate_segments


def fetch_json(video_id: str, attempts: int = 3) -> dict:
    url = "https://www.summynews.com/api.php?" + urllib.parse.urlencode(
        {"v": video_id, "format": "json", "ts": "true"}
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 TranscriptReliabilityHarness/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}: {payload[:500]!r}")
                return json.loads(payload)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"SumMyNews failed after {attempts} attempts: {last_error}")


def main() -> int:
    matrix = json.loads(Path("test_matrix.json").read_text(encoding="utf-8"))
    output_dir = Path("provider-artifacts")
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    for index, entry in enumerate(matrix["videos"], start=1):
        video_id = resolve_video_id(entry["url"])
        started = time.monotonic()
        print(f"[{index}/{len(matrix['videos'])}] {video_id}", flush=True)
        try:
            data = fetch_json(video_id)
            raw_segments = data.get("transcript") or []
            segments = [
                Segment(
                    start=float(item["start"]),
                    end=float(item["start"]) + max(0.0, float(item.get("duration", 0.0))),
                    text=str(item.get("text", "")).strip(),
                )
                for item in raw_segments
                if str(item.get("text", "")).strip()
            ]
            validation = validate_segments(
                segments,
                duration_seconds=None,
                minimum_characters=int(entry.get("minimum_characters", 20)),
            )
            evidence = {
                "provider": "SumMyNews",
                "attribution": data.get("attribution")
                or f"https://www.summynews.com/?v={video_id}",
                "video_id": video_id,
                "language": data.get("language"),
                "language_name": data.get("language_name"),
                "is_auto_generated": data.get("is_auto_generated"),
                "validation": validation,
                "segments": [segment.to_dict() for segment in segments],
            }
            (output_dir / f"{video_id}.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = {
                "video_id": video_id,
                "passed": bool(validation["passed"]),
                "segment_count": validation.get("segment_count", 0),
                "character_count": validation.get("character_count", 0),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error": None,
            }
        except Exception as exc:
            result = {
                "video_id": video_id,
                "passed": False,
                "segment_count": 0,
                "character_count": 0,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error": str(exc),
            }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    passed = sum(result["passed"] for result in results)
    summary = {
        "provider": "SumMyNews",
        "requested_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "all_passed": passed == len(results),
        "results": results,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
