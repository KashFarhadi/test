#!/usr/bin/env python3
"""Reliable YouTube transcript extraction with caption and local-ASR fallbacks.

The extractor never bypasses access controls. Public videos are handled without
credentials. For videos the caller is authorized to view, pass an exported
Netscape cookie file or a browser cookie source to yt-dlp.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Sequence


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
TIMESTAMP_LINE = re.compile(
    r"(?P<start>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})"
)
INLINE_VTT_TIMESTAMP = re.compile(r"<\d{2}:\d{2}:\d{2}[.]\d{3}>")
HTML_TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")


class ExtractionError(RuntimeError):
    """A recoverable extraction-route failure."""


@dataclasses.dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "timestamp": format_timestamp(self.start),
            "text": self.text,
        }


@dataclasses.dataclass(slots=True)
class RouteFailure:
    route: str
    attempt: int
    error: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(slots=True)
class RuntimeConfig:
    language: str = "en"
    asr_model: str = "small.en"
    force_asr: bool = False
    cookies: str | None = None
    cookies_from_browser: str | None = None
    proxy: str | None = None
    js_runtime: str = "node"
    extractor_args: str | None = None
    attempts_per_route: int = 3
    minimum_characters: int = 20
    strict: bool = False


def normalize_whitespace(value: str) -> str:
    return WHITESPACE.sub(" ", value).strip()


def format_timestamp(seconds: float, srt: bool = False) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def parse_timestamp(value: str) -> float:
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"Invalid timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def resolve_video_id(value: str) -> str:
    value = value.strip()
    if VIDEO_ID_PATTERN.fullmatch(value):
        return value

    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower().split(":", 1)[0]
    candidate = ""
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.path.rstrip("/") == "/watch":
            candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live", "v"}:
            candidate = path_parts[1]
    if not VIDEO_ID_PATTERN.fullmatch(candidate):
        raise ValueError(f"Could not resolve an 11-character YouTube video ID from {value!r}")
    return candidate


def canonical_url(value: str) -> str:
    return f"https://www.youtube.com/watch?v={resolve_video_id(value)}"


def run_command(
    command: Sequence[str],
    *,
    timeout: int = 900,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        stderr = normalize_whitespace(completed.stderr)[-4000:]
        stdout = normalize_whitespace(completed.stdout)[-1000:]
        detail = stderr or stdout or f"exit status {completed.returncode}"
        raise ExtractionError(detail)
    return completed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def yt_dlp_common(config: RuntimeConfig) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--force-ipv4",
        "--js-runtimes",
        config.js_runtime,
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--file-access-retries",
        "3",
        "--socket-timeout",
        "30",
        "--sleep-requests",
        "0.75",
    ]
    if config.cookies:
        command += ["--cookies", config.cookies]
    if config.cookies_from_browser:
        command += ["--cookies-from-browser", config.cookies_from_browser]
    if config.proxy:
        command += ["--proxy", config.proxy]
    if config.extractor_args:
        command += ["--extractor-args", config.extractor_args]
    return command


def metadata_via_ytdlp(url: str, config: RuntimeConfig) -> dict[str, Any]:
    command = yt_dlp_common(config) + [
        "--skip-download",
        "--dump-single-json",
        "--no-warnings",
        url,
    ]
    completed = run_command(command, timeout=300)
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"yt-dlp returned invalid metadata JSON: {exc}") from exc
    return {
        "id": data.get("id") or resolve_video_id(url),
        "title": data.get("title") or "",
        "channel": data.get("channel") or data.get("uploader") or "",
        "duration_seconds": float(data["duration"]) if data.get("duration") is not None else None,
        "webpage_url": data.get("webpage_url") or canonical_url(url),
        "availability": data.get("availability"),
        "live_status": data.get("live_status"),
    }


def clean_caption_text(value: str) -> str:
    value = INLINE_VTT_TIMESTAMP.sub("", value)
    value = HTML_TAG.sub("", value)
    return normalize_whitespace(html.unescape(value).replace("\u200b", ""))


def parse_vtt(path: Path) -> list[Segment]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    segments: list[Segment] = []
    index = 0
    while index < len(lines):
        match = TIMESTAMP_LINE.search(lines[index])
        if not match:
            index += 1
            continue
        start = parse_timestamp(match.group("start"))
        end = parse_timestamp(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            line = lines[index].strip()
            if not TIMESTAMP_LINE.search(line) and not line.startswith("NOTE"):
                text_lines.append(line)
            index += 1
        text = clean_caption_text(" ".join(text_lines))
        if text and end >= start:
            if segments and abs(start - segments[-1].start) < 0.001 and text == segments[-1].text:
                segments[-1].end = max(segments[-1].end, end)
            elif not segments or text != segments[-1].text or start > segments[-1].end + 0.25:
                segments.append(Segment(start=start, end=end, text=text))
        index += 1
    return segments


def longest_word_overlap(previous: list[str], current: list[str], max_words: int = 20) -> int:
    upper = min(len(previous), len(current), max_words)
    for length in range(upper, 0, -1):
        if [word.casefold() for word in previous[-length:]] == [word.casefold() for word in current[:length]]:
            return length
    return 0


def full_text_from_segments(segments: Iterable[Segment]) -> str:
    output_words: list[str] = []
    previous_words: list[str] = []
    for segment in segments:
        words = segment.text.split()
        if not words:
            continue
        overlap = longest_word_overlap(previous_words, words)
        novel = words[overlap:]
        if novel:
            output_words.extend(novel)
        previous_words = words
    return normalize_whitespace(" ".join(output_words))


def select_language_track(transcript_list: Any, language: str) -> Any:
    tracks = list(transcript_list)
    if not tracks:
        raise ExtractionError("YouTube listed no transcript tracks")
    exact = [track for track in tracks if str(track.language_code).casefold() == language.casefold()]
    prefix = [
        track
        for track in tracks
        if str(track.language_code).split("-", 1)[0].casefold()
        == language.split("-", 1)[0].casefold()
    ]
    candidates = exact or prefix or tracks
    candidates.sort(key=lambda track: (bool(track.is_generated), str(track.language_code)))
    return candidates[0]


def captions_via_transcript_api(url: str, config: RuntimeConfig) -> tuple[list[Segment], dict[str, Any]]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise ExtractionError("youtube-transcript-api is not installed") from exc

    video_id = resolve_video_id(url)
    api = YouTubeTranscriptApi()
    track = select_language_track(api.list(video_id), config.language)
    fetched = track.fetch()
    segments = [
        Segment(
            start=float(item.start),
            end=float(item.start) + max(0.0, float(item.duration)),
            text=clean_caption_text(str(item.text)),
        )
        for item in fetched
        if clean_caption_text(str(item.text))
    ]
    return segments, {
        "language": str(fetched.language_code),
        "language_name": str(fetched.language),
        "is_generated": bool(fetched.is_generated),
    }


def captions_via_ytdlp(url: str, config: RuntimeConfig, workdir: Path) -> tuple[list[Segment], dict[str, Any]]:
    video_id = resolve_video_id(url)
    output_template = str(workdir / "captions" / "%(id)s.%(ext)s")
    (workdir / "captions").mkdir(parents=True, exist_ok=True)
    language_spec = f"{config.language},{config.language}.*,-live_chat"
    command = yt_dlp_common(config) + [
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        language_spec,
        "--sub-format",
        "vtt/best",
        "--output",
        output_template,
        url,
    ]
    run_command(command, timeout=600)
    candidates = sorted((workdir / "captions").glob(f"{video_id}*.vtt"))
    if not candidates:
        raise ExtractionError(f"yt-dlp produced no {config.language} VTT file")
    best_path = max(candidates, key=lambda path: path.stat().st_size)
    segments = parse_vtt(best_path)
    if not segments:
        raise ExtractionError(f"VTT file {best_path.name} contained no caption cues")
    language_match = re.search(rf"{re.escape(video_id)}[.]([^.]+(?:-[^.]+)*)[.]vtt$", best_path.name)
    return segments, {
        "language": language_match.group(1) if language_match else config.language,
        "caption_file": best_path.name,
        "caption_sha256": sha256_file(best_path),
    }


def locate_downloaded_audio(workdir: Path, video_id: str) -> Path:
    candidates = [
        path
        for path in workdir.glob(f"{video_id}.*")
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".json", ".vtt", ".srt", ".txt"}
    ]
    if not candidates:
        raise ExtractionError("yt-dlp reported success but no audio file was found")
    return max(candidates, key=lambda path: path.stat().st_size)


def download_audio(url: str, config: RuntimeConfig, workdir: Path) -> tuple[Path, list[dict[str, Any]]]:
    video_id = resolve_video_id(url)
    output_template = str(workdir / f"{video_id}.%(ext)s")
    strategies: list[tuple[str, str | None]] = [
        ("default", None),
        ("web-safari-tv", "youtube:player_client=web_safari,tv"),
        ("android-vr", "youtube:player_client=android_vr"),
    ]
    attempts: list[dict[str, Any]] = []
    source_path: Path | None = None
    for name, strategy_args in strategies:
        command = yt_dlp_common(config)
        if strategy_args and not config.extractor_args:
            command += ["--extractor-args", strategy_args]
        command += [
            "--format",
            "bestaudio[acodec!=none]/bestaudio/best",
            "--output",
            output_template,
            url,
        ]
        try:
            run_command(command, timeout=1800)
            source_path = locate_downloaded_audio(workdir, video_id)
            attempts.append({"strategy": name, "status": "success", "file": source_path.name})
            break
        except Exception as exc:
            attempts.append({"strategy": name, "status": "failed", "error": str(exc)[-2000:]})
    if source_path is None:
        raise ExtractionError("All yt-dlp audio strategies failed: " + json.dumps(attempts, ensure_ascii=False))

    wav_path = workdir / f"{video_id}.16k-mono.wav"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        timeout=1800,
    )
    if not wav_path.exists() or wav_path.stat().st_size <= 44:
        raise ExtractionError("ffmpeg did not create a valid WAV file")
    return wav_path, attempts


def ffprobe_duration(path: Path) -> float:
    completed = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=120,
    )
    try:
        return float(completed.stdout.strip())
    except ValueError as exc:
        raise ExtractionError(f"ffprobe returned invalid duration: {completed.stdout!r}") from exc


def asr_transcribe(path: Path, config: RuntimeConfig) -> tuple[list[Segment], dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ExtractionError("faster-whisper is not installed") from exc

    cpu_threads = max(2, min(8, os.cpu_count() or 2))
    model = WhisperModel(
        config.asr_model,
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
        num_workers=1,
    )
    result, info = model.transcribe(
        str(path),
        language=config.language or None,
        beam_size=5,
        best_of=5,
        temperature=0.0,
        vad_filter=False,
        condition_on_previous_text=True,
        word_timestamps=False,
    )
    segments: list[Segment] = []
    diagnostics: list[dict[str, Any]] = []
    for item in result:
        text = normalize_whitespace(item.text)
        if not text:
            continue
        segment = Segment(start=float(item.start), end=float(item.end), text=text)
        segments.append(segment)
        diagnostics.append(
            {
                "start": round(float(item.start), 3),
                "end": round(float(item.end), 3),
                "avg_logprob": round(float(item.avg_logprob), 5),
                "no_speech_prob": round(float(item.no_speech_prob), 5),
            }
        )
    return segments, {
        "model": config.asr_model,
        "detected_language": str(info.language),
        "language_probability": round(float(info.language_probability), 6),
        "processed_duration_seconds": round(float(info.duration), 3),
        "duration_after_vad_seconds": round(float(info.duration_after_vad), 3),
        "cpu_threads": cpu_threads,
        "segment_diagnostics": diagnostics,
    }


def validate_segments(
    segments: Sequence[Segment],
    *,
    duration_seconds: float | None,
    minimum_characters: int,
    processed_duration_seconds: float | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not segments:
        errors.append("no timestamped segments")
        return {
            "passed": False,
            "errors": errors,
            "warnings": warnings,
            "segment_count": 0,
            "character_count": 0,
        }

    previous_start = -math.inf
    character_count = 0
    for index, segment in enumerate(segments):
        if not math.isfinite(segment.start) or not math.isfinite(segment.end):
            errors.append(f"segment {index} has a non-finite timestamp")
        if segment.start < -0.001 or segment.end < segment.start:
            errors.append(f"segment {index} has invalid bounds")
        if segment.start + 0.05 < previous_start:
            errors.append(f"segment {index} starts before the previous segment")
        if not normalize_whitespace(segment.text):
            errors.append(f"segment {index} is empty")
        previous_start = segment.start
        character_count += len(segment.text)

    if character_count < minimum_characters:
        errors.append(
            f"transcript contains {character_count} characters, below minimum {minimum_characters}"
        )

    first_start = segments[0].start
    last_end = max(segment.end for segment in segments)
    coverage_ratio = None
    if duration_seconds and duration_seconds > 0:
        coverage_ratio = min(1.0, max(0.0, last_end / duration_seconds))
        if last_end > duration_seconds + 30:
            errors.append(
                f"last timestamp {last_end:.2f}s exceeds source duration {duration_seconds:.2f}s by more than 30s"
            )
        if coverage_ratio < 0.50:
            warnings.append(
                f"last spoken/caption segment ends at only {coverage_ratio:.1%} of media duration"
            )
    if first_start > 120:
        warnings.append(f"first segment does not begin until {first_start:.1f}s")

    if processed_duration_seconds is not None and duration_seconds is not None:
        tolerance = max(2.0, duration_seconds * 0.01)
        if abs(processed_duration_seconds - duration_seconds) > tolerance:
            errors.append(
                "ASR engine did not report processing the full audio duration: "
                f"processed={processed_duration_seconds:.2f}s, audio={duration_seconds:.2f}s"
            )

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "segment_count": len(segments),
        "character_count": character_count,
        "word_count": len(full_text_from_segments(segments).split()),
        "first_start_seconds": round(first_start, 3),
        "last_end_seconds": round(last_end, 3),
        "source_duration_seconds": round(duration_seconds, 3) if duration_seconds else None,
        "end_coverage_ratio": round(coverage_ratio, 6) if coverage_ratio is not None else None,
    }


def retry_route(
    route_name: str,
    attempts: int,
    failures: list[RouteFailure],
    function: Any,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return function()
        except Exception as exc:
            last_error = exc
            failures.append(RouteFailure(route=route_name, attempt=attempt, error=str(exc)[-4000:]))
            if attempt < attempts:
                time.sleep(min(8.0, 2.0**attempt))
    raise ExtractionError(f"{route_name} failed after {attempts} attempts: {last_error}")


def write_srt(path: Path, segments: Sequence[Segment]) -> None:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.extend(
            [
                str(index),
                f"{format_timestamp(segment.start, srt=True)} --> {format_timestamp(segment.end, srt=True)}",
                segment.text,
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def extract_one(source: str, output_dir: Path, config: RuntimeConfig) -> dict[str, Any]:
    url = canonical_url(source)
    video_id = resolve_video_id(source)
    failures: list[RouteFailure] = []
    metadata: dict[str, Any] = {
        "id": video_id,
        "title": "",
        "channel": "",
        "duration_seconds": None,
        "webpage_url": url,
    }
    try:
        metadata = retry_route(
            "yt-dlp-metadata",
            config.attempts_per_route,
            failures,
            lambda: metadata_via_ytdlp(url, config),
        )
    except ExtractionError:
        pass

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"yt-{video_id}-") as temporary:
        workdir = Path(temporary)
        method = ""
        method_details: dict[str, Any] = {}
        audio_evidence: dict[str, Any] | None = None
        segments: list[Segment] = []

        if not config.force_asr:
            for route_name, extractor in (
                (
                    "youtube-transcript-api",
                    lambda: captions_via_transcript_api(url, config),
                ),
                (
                    "yt-dlp-captions",
                    lambda: captions_via_ytdlp(url, config, workdir),
                ),
            ):
                try:
                    candidate_segments, candidate_details = retry_route(
                        route_name,
                        config.attempts_per_route,
                        failures,
                        extractor,
                    )
                    validation = validate_segments(
                        candidate_segments,
                        duration_seconds=metadata.get("duration_seconds"),
                        minimum_characters=config.minimum_characters,
                    )
                    if validation["passed"]:
                        segments = candidate_segments
                        method = route_name
                        method_details = candidate_details
                        break
                    failures.append(
                        RouteFailure(
                            route=route_name,
                            attempt=config.attempts_per_route + 1,
                            error="validation failed: " + json.dumps(validation, ensure_ascii=False),
                        )
                    )
                except ExtractionError:
                    continue

        if not segments:
            wav_path, download_attempts = retry_route(
                "yt-dlp-audio",
                config.attempts_per_route,
                failures,
                lambda: download_audio(url, config, workdir),
            )
            audio_duration = ffprobe_duration(wav_path)
            if metadata.get("duration_seconds") is None:
                metadata["duration_seconds"] = audio_duration
            candidate_segments, candidate_details = retry_route(
                "faster-whisper-asr",
                config.attempts_per_route,
                failures,
                lambda: asr_transcribe(wav_path, config),
            )
            segments = candidate_segments
            method = "audio+faster-whisper"
            method_details = candidate_details
            audio_evidence = {
                "wav_sha256": sha256_file(wav_path),
                "wav_size_bytes": wav_path.stat().st_size,
                "audio_duration_seconds": round(audio_duration, 3),
                "download_attempts": download_attempts,
            }

        processed_duration = method_details.get("processed_duration_seconds")
        validation = validate_segments(
            segments,
            duration_seconds=metadata.get("duration_seconds"),
            minimum_characters=config.minimum_characters,
            processed_duration_seconds=float(processed_duration) if processed_duration is not None else None,
        )
        full_text = full_text_from_segments(segments)
        result: dict[str, Any] = {
            "schema_version": "2.0",
            "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": metadata,
            "method": method,
            "method_details": method_details,
            "audio_evidence": audio_evidence,
            "route_failures": [failure.to_dict() for failure in failures],
            "validation": validation,
            "segments": [segment.to_dict() for segment in segments],
            "full_text": full_text,
        }

        stem = output_dir / video_id
        json_path = stem.with_suffix(".json")
        txt_path = stem.with_suffix(".txt")
        srt_path = stem.with_suffix(".srt")
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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

        if config.strict and not validation["passed"]:
            raise ExtractionError(
                f"Transcript validation failed for {video_id}: "
                + json.dumps(validation, ensure_ascii=False)
            )
        return result


def config_from_entry(base: RuntimeConfig, entry: dict[str, Any]) -> RuntimeConfig:
    values = dataclasses.asdict(base)
    for field in dataclasses.fields(RuntimeConfig):
        if field.name in entry:
            values[field.name] = entry[field.name]
    return RuntimeConfig(**values)


def run_matrix(matrix_path: Path, output_dir: Path, base_config: RuntimeConfig) -> dict[str, Any]:
    raw = json.loads(matrix_path.read_text(encoding="utf-8"))
    entries = raw["videos"] if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise ValueError("Matrix must contain a nonempty videos list")

    results: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ValueError(f"Matrix entry {index} must contain a URL")
        source = str(entry["url"])
        video_id = resolve_video_id(source)
        print(f"[{index}/{len(entries)}] extracting {video_id}", flush=True)
        config = config_from_entry(base_config, entry)
        started = time.monotonic()
        try:
            extracted = extract_one(source, output_dir, config)
            results.append(
                {
                    "video_id": video_id,
                    "url": canonical_url(source),
                    "label": entry.get("label", ""),
                    "passed": bool(extracted["validation"]["passed"]),
                    "method": extracted["method"],
                    "duration_seconds": extracted["source"].get("duration_seconds"),
                    "segment_count": extracted["validation"].get("segment_count"),
                    "character_count": extracted["validation"].get("character_count"),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "error": None,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "video_id": video_id,
                    "url": canonical_url(source),
                    "label": entry.get("label", ""),
                    "passed": False,
                    "method": None,
                    "duration_seconds": None,
                    "segment_count": 0,
                    "character_count": 0,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "error": str(exc),
                }
            )
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)

    passed = sum(1 for result in results if result["passed"])
    summary = {
        "schema_version": "1.0",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_count": len(entries),
        "passed_count": passed,
        "failed_count": len(entries) - passed,
        "success_rate": round(passed / len(entries), 4),
        "all_passed": passed == len(entries),
        "results": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "validation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = [
        "# YouTube Transcript Validation",
        "",
        f"**Result:** {passed}/{len(entries)} passed ({summary['success_rate']:.0%})",
        "",
        "| # | Video | Method | Segments | Characters | Result |",
        "|---:|---|---|---:|---:|---|",
    ]
    for index, result in enumerate(results, start=1):
        markdown.append(
            f"| {index} | `{result['video_id']}` {result['label']} | "
            f"{result['method'] or 'failed'} | {result['segment_count']} | "
            f"{result['character_count']} | {'PASS' if result['passed'] else 'FAIL'} |"
        )
        if result["error"]:
            markdown.append(f"\nFailure {result['video_id']}: `{result['error']}`\n")
    (output_dir / "validation-summary.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")

    if base_config.strict and not summary["all_passed"]:
        raise ExtractionError(f"Matrix validation failed: {passed}/{len(entries)} passed")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", help="YouTube URL or 11-character video ID")
    parser.add_argument("--matrix", type=Path, help="JSON file containing a videos list")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--language", default="en")
    parser.add_argument("--asr-model", default="small.en")
    parser.add_argument("--force-asr", action="store_true")
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
    parser = build_parser()
    args = parser.parse_args()
    if bool(args.source) == bool(args.matrix):
        parser.error("Provide exactly one of source or --matrix")
    if args.cookies and args.cookies_from_browser:
        parser.error("Use either --cookies or --cookies-from-browser, not both")
    if args.attempts_per_route < 1 or args.attempts_per_route > 5:
        parser.error("--attempts-per-route must be between 1 and 5")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        parser.error("ffmpeg and ffprobe must be installed and available on PATH")

    config = RuntimeConfig(
        language=args.language,
        asr_model=args.asr_model,
        force_asr=args.force_asr,
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
        if args.matrix:
            summary = run_matrix(args.matrix, args.output_dir, config)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            result = extract_one(args.source, args.output_dir, config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
