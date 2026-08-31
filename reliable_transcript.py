#!/usr/bin/env python3
"""Production YouTube transcript entry point.

Primary route: timestamped captions through the SumMyNews server proxy.
Fallback route: the local extractor in extract_transcript.py, which tries direct
caption APIs and then authorized audio download plus faster-whisper ASR.

The verified scope is public YouTube videos whose caption stream is available to
the provider. Private, deleted, region-blocked, DRM-protected, or captionless
videos require an authorized local audio route and cannot be guaranteed by a
caption-only service.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from extract_transcript import (
    ExtractionError,
    RuntimeConfig,
    Segment,
    canonical_url,
    config_from_entry,
    extract_one,
    full_text_from_segments,
    resolve_video_id,
    sha256_file,
    validate_segments,
    write_srt,
)


PROVIDER_ENDPOINT = "https://www.summynews.com/api.php"
PROVIDER_NAME = "SumMyNews"
SCHEMA_VERSION = "3.0"


def canonical_segment_digest(segments: list[Segment]) -> str:
    payload = json.dumps(
        [segment.to_dict() for segment in segments],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fetch_provider_payload(video_id: str, attempts: int = 3) -> dict[str, Any]:
    query = urllib.parse.urlencode({"v": video_id, "format": "json", "ts": "true"})
    url = f"{PROVIDER_ENDPOINT}?{query}"
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 TranscriptReliabilityHarness/3.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                if response.status != 200:
                    raise ExtractionError(f"Provider returned HTTP {response.status}")
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ExtractionError("Provider response was not a JSON object")
                return payload
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(8.0, 2.0**attempt))

    raise ExtractionError(f"{PROVIDER_NAME} failed after {attempts} attempts: {last_error}")


def parse_provider_segments(payload: dict[str, Any]) -> list[Segment]:
    raw_segments = payload.get("transcript")
    if not isinstance(raw_segments, list):
        raise ExtractionError("Provider response did not contain a transcript list")

    segments: list[Segment] = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise ExtractionError(f"Provider segment {index} was not an object")
        text = " ".join(str(item.get("text", "")).split())
        if not text:
            continue
        try:
            start = float(item["start"])
            duration = max(0.0, float(item.get("duration", 0.0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(f"Provider segment {index} had invalid timing") from exc
        segments.append(Segment(start=start, end=start + duration, text=text))

    return segments


def provider_result(source: str, output_dir: Path, config: RuntimeConfig) -> dict[str, Any]:
    video_id = resolve_video_id(source)
    payload = fetch_provider_payload(video_id, attempts=config.attempts_per_route)
    segments = parse_provider_segments(payload)
    validation = validate_segments(
        segments,
        duration_seconds=None,
        minimum_characters=config.minimum_characters,
    )
    if not validation["passed"]:
        raise ExtractionError(
            "Provider transcript failed validation: "
            + json.dumps(validation, ensure_ascii=False)
        )

    full_text = full_text_from_segments(segments)
    attribution = payload.get("attribution")
    if not isinstance(attribution, dict):
        attribution = {
            "required": True,
            "text": "Transcript retrieved through SumMyNews",
            "link": f"https://www.summynews.com/?v={video_id}",
        }

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "id": video_id,
            "webpage_url": canonical_url(source),
        },
        "method": "managed-caption-proxy",
        "provider": {
            "name": PROVIDER_NAME,
            "attribution": attribution,
            "language": payload.get("language") or config.language,
            "language_name": payload.get("language_name") or "",
            "is_auto_generated": payload.get("is_auto_generated"),
        },
        "validation": validation,
        "transcript_sha256": canonical_segment_digest(segments),
        "segments": [segment.to_dict() for segment in segments],
        "full_text": full_text,
        "fallback_used": False,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_dir / video_id
    json_path = stem.with_suffix(".json")
    txt_path = stem.with_suffix(".txt")
    srt_path = stem.with_suffix(".srt")
    txt_path.write_text(full_text + "\n", encoding="utf-8")
    write_srt(srt_path, segments)
    result["evidence_files"] = {
        "json": json_path.name,
        "txt": txt_path.name,
        "srt": srt_path.name,
        "txt_sha256": sha256_file(txt_path),
        "srt_sha256": sha256_file(srt_path),
    }
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    checksum_path = output_dir / f"{video_id}.sha256"
    checksum_path.write_text(
        "\n".join(
            [
                f"{sha256_file(json_path)}  {json_path.name}",
                f"{sha256_file(txt_path)}  {txt_path.name}",
                f"{sha256_file(srt_path)}  {srt_path.name}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result["evidence_files"]["checksums"] = checksum_path.name
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def extract_reliable(source: str, output_dir: Path, config: RuntimeConfig) -> dict[str, Any]:
    provider_error: str | None = None
    try:
        return provider_result(source, output_dir, config)
    except Exception as exc:
        provider_error = str(exc)

    fallback = extract_one(source, output_dir, config)
    fallback["fallback_used"] = True
    fallback["primary_route_failure"] = provider_error
    json_path = output_dir / f"{resolve_video_id(source)}.json"
    json_path.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
    return fallback


def run_matrix(matrix_path: Path, output_dir: Path, base_config: RuntimeConfig) -> dict[str, Any]:
    raw = json.loads(matrix_path.read_text(encoding="utf-8"))
    entries = raw.get("videos") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise ValueError("Matrix must contain a nonempty videos list")

    results: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ValueError(f"Matrix entry {index} must contain a URL")
        config = config_from_entry(base_config, entry)
        video_id = resolve_video_id(str(entry["url"]))
        started = time.monotonic()
        print(f"[{index}/{len(entries)}] extracting {video_id}", flush=True)
        try:
            extracted = extract_reliable(str(entry["url"]), output_dir, config)
            validation = extracted["validation"]
            result = {
                "video_id": video_id,
                "label": entry.get("label", ""),
                "passed": bool(validation["passed"]),
                "method": extracted["method"],
                "segment_count": validation.get("segment_count", 0),
                "character_count": validation.get("character_count", 0),
                "transcript_sha256": extracted.get("transcript_sha256"),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error": None,
            }
        except Exception as exc:
            result = {
                "video_id": video_id,
                "label": entry.get("label", ""),
                "passed": False,
                "method": None,
                "segment_count": 0,
                "character_count": 0,
                "transcript_sha256": None,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error": str(exc),
            }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    passed = sum(bool(result["passed"]) for result in results)
    summary = {
        "schema_version": "2.0",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "success_rate": round(passed / len(results), 4),
        "all_passed": passed == len(results),
        "results": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "validation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if base_config.strict and not summary["all_passed"]:
        raise ExtractionError(f"Matrix validation failed: {passed}/{len(results)} passed")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", help="YouTube URL or 11-character video ID")
    parser.add_argument("--matrix", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--language", default="en")
    parser.add_argument("--asr-model", default="small.en")
    parser.add_argument("--cookies")
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--proxy")
    parser.add_argument("--js-runtime", default="node")
    parser.add_argument("--extractor-args")
    parser.add_argument("--attempts-per-route", type=int, default=3)
    parser.add_argument("--minimum-characters", type=int, default=20)
    parser.add_argument("--strict", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if bool(args.source) == bool(args.matrix):
        raise SystemExit("Provide exactly one of source or --matrix")
    if args.cookies and args.cookies_from_browser:
        raise SystemExit("Use either --cookies or --cookies-from-browser, not both")

    config = RuntimeConfig(
        language=args.language,
        asr_model=args.asr_model,
        cookies=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        proxy=args.proxy,
        js_runtime=args.js_runtime,
        extractor_args=args.extractor_args,
        attempts_per_route=args.attempts_per_route,
        minimum_characters=args.minimum_characters,
        strict=args.strict,
    )
    try:
        result = (
            run_matrix(args.matrix, args.output_dir, config)
            if args.matrix
            else extract_reliable(args.source, args.output_dir, config)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
