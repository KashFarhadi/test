# YouTube Transcript Reliability Harness

Version 3.0, validated August 18, 2026.

A layered transcript extractor for public YouTube videos, with explicit evidence and failure boundaries.

## Verified extraction order

1. **Managed timestamped-caption proxy:** SumMyNews. This avoids the data-center IP blocks that caused direct YouTube caption and media requests to fail in GitHub Actions.
2. **Direct caption fallbacks:** `youtube-transcript-api`, then current `yt-dlp` subtitle extraction.
3. **Authorized local audio fallback:** current `yt-dlp`, FFmpeg, and `faster-whisper` ASR. Use browser cookies or a Netscape cookie file only for content the caller is authorized to view.

A route counts as successful only when it returns nonempty timestamped segments, monotonic timestamps, valid timing bounds, sufficient text, and machine-readable evidence.

## Verified result

- Ten different public videos: **10/10 passed**.
- Exact target `ILtQtKuH84Q`: **10/10 consecutive fetches passed**.
- All ten target fetches returned the same 518 segments, 18,453 caption characters, 17:44.16 ending timestamp, and SHA-256 transcript digest.
- The direct cloud-runner route was also tested. YouTube's bot challenge blocked 9/10 videos, which is why it is not the primary cloud method.

## Single video

```bash
python reliable_transcript.py "https://youtu.be/VIDEO_ID" \
  --output-dir artifacts \
  --strict
```

## Ten-video validation

```bash
python reliable_transcript.py \
  --matrix test_matrix.json \
  --output-dir artifacts \
  --strict
```

## Local fallback dependencies

The managed caption route uses only the Python standard library. Install the full dependency set when caption retrieval may fail or the video has no captions:

```bash
python -m pip install -r requirements.txt
```

Install FFmpeg and use Node 24 for current `yt-dlp` YouTube extraction. Where YouTube requires proof-of-origin tokens, configure a maintained yt-dlp PO-token provider rather than hard-coding transient tokens.

For an age-restricted, private, or members-only video that you are authorized to view, add either `--cookies /path/to/cookies.txt` or `--cookies-from-browser chrome`. The tool does not bypass access controls.

## Output contract

Every successful extraction writes:

- timestamped JSON evidence
- normalized TXT transcript
- SRT subtitles
- SHA-256 checksums
- capture method, provider attribution, language, caption type, validation metrics, and fallback failures

## Honest reliability boundary

The 10/10 result applies to the tested class: accessible public videos with a retrievable caption stream. No method can truthfully guarantee transcripts for deleted videos, unavailable private videos, DRM-protected content, regional blocks without lawful access, or captionless videos whose audio cannot be accessed. Captionless accessible videos are handled by the authorized local audio-plus-ASR fallback.
