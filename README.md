# YouTube Transcript Reliability Harness

A validated, layered transcript extractor for public YouTube videos.

## Extraction order

1. Native or auto-generated captions through current `yt-dlp`.
2. Independent caption retrieval through `youtube-transcript-api`.
3. Audio download through `yt-dlp`, followed by local `faster-whisper` ASR.

A route only counts as successful when it returns nonempty timestamped segments, monotonic timestamps, plausible duration bounds, and the configured minimum end-coverage ratio.

## Single video

```bash
python -m pip install -r requirements.txt
python extract_transcript.py "https://youtu.be/VIDEO_ID" \
  --output-dir artifacts \
  --asr-model small \
  --strict
```

For an age-restricted, private, or members-only video that you are authorized to view, export a Netscape-format cookie file and add `--cookies /path/to/cookies.txt`. The tool does not bypass access controls.

## Ten-video validation

```bash
python extract_transcript.py \
  --matrix test_matrix.json \
  --output-dir artifacts \
  --asr-model tiny \
  --strict
```

The GitHub Actions workflow runs the same matrix and uploads `validation-summary.json` plus each transcript's JSON, TXT, and SRT evidence.

## Output contract

Each JSON evidence file records source metadata, extraction method, retries, failures from earlier routes, file hashes, validation metrics, timestamped segments, and normalized full text.
