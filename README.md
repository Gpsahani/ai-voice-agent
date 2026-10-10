# Audio transcription prototype

Small HTTP service that accepts a WAV or MP3 upload, normalizes it with FFmpeg, and returns a transcript with segment timestamps from faster-whisper.

Install and start steps are in [docs/INSTALLATION.md](docs/INSTALLATION.md). Request examples and the error table are in [docs/USAGE.md](docs/USAGE.md). The behavior reference is [docs/TECHNICAL_PLAN.md](docs/TECHNICAL_PLAN.md).

## Features

- `POST /v1/transcriptions` accepts a multipart field named `file`.
- WAV and MP3 are the supported formats. The container is identified by ffprobe, not by the client `Content-Type`.
- ffprobe reads metadata only (audio stream, format name, duration). It does not prove the whole file decodes. Supported audio is then normalized with FFmpeg to 16 kHz mono WAV before transcription.
- Segment `start` and `end` come from the model. For a recording longer than `CHUNK_SECONDS`, each chunk is transcribed separately and local times are shifted by that chunk's offset (`global = chunk_offset + local`). Overlap at the start of later chunks is dropped. The service never invents times by slicing the plain transcript into equal parts.
- Uploads are capped by `MAX_UPLOAD_BYTES` (default 25 MiB) and `MAX_DURATION_SECONDS` (default 600). Audio between one chunk and that duration cap is chunked inside the request. Longer audio is rejected.
- Temporary upload, normalized, and chunk files are deleted after success and failure.
- `GET /health` reports that the process is up. `GET /ready` is 200 only after the model loads. A failed model load is logged, the process stays up, and `/ready` returns 503 `ENGINE_UNAVAILABLE`.

## Non-goals

Authentication is omitted. There is no API key, user, or rate limit. Do not expose this process to the public internet as-is.

This prototype also has no database, object storage, durable queue, job table, or background worker. In-process background tasks are not a substitute for that path.

## Limits

| Control | Default | Effect |
| --- | --- | --- |
| `MAX_UPLOAD_BYTES` | 26214400 | Reject larger bodies. `Content-Length` is checked first when the header is present; bytes are still counted while streaming. |
| `MAX_DURATION_SECONDS` | 600 | Reject longer media before transcription. |
| `CHUNK_SECONDS` | 120 | Files longer than this are split. |
| `CHUNK_OVERLAP_SECONDS` | 2 | Overlap between chunks. Must be `>= 0` and `< CHUNK_SECONDS`. |
| `FFMPEG_TIMEOUT_SECONDS` | 60 | Timeout for normalization and chunk extraction. |
| `FFPROBE_TIMEOUT_SECONDS` | 30 | Timeout for metadata inspection. |

There is no `TRANSCRIBE_TIMEOUT_SECONDS`. A timer around the request cannot safely stop faster-whisper once inference is running. The synchronous prototype bounds duration and runs one inference at a time instead.

The HTTP `Content-Length` includes multipart framing, so a file very close to the byte cap can be rejected before the byte counter runs. Set a reverse-proxy body limit as well; the route counter is not the only upload control.

## Timestamps

1. Short files (duration `<= CHUNK_SECONDS`): one `transcribe()` call on the normalized WAV. Each segment keeps the model's `start`, `end`, and `text`.
2. Longer files: FFmpeg extracts each window with `-ss <offset> -t CHUNK_SECONDS`. The offset of chunk `i` is `i * (CHUNK_SECONDS - CHUNK_OVERLAP_SECONDS)`.
3. Local times become global times by adding the chunk offset.
4. For every chunk after the first, segments whose global end is at or before `chunk_offset + CHUNK_OVERLAP_SECONDS` are dropped. A repeated word later than that window is kept.
5. The joined `text` is those segment strings separated by a single space. If any chunk fails, the request fails and no partial transcript is returned.
6. Before the response is sent, times must be finite, `start >= 0`, `end >= start`, and starts must be chronological. When duration is known, `end` must be within `duration + 0.5` seconds.

## Tests

From the repository root, with the virtualenv active:

```powershell
pytest
```

Default `pytest` does not import or download a model. The real-model smoke test is marked `integration` and is skipped unless `RUN_INTEGRATION=1`.

Probe tests that need the `ffprobe` and `ffmpeg` binaries skip with a reason when those tools are not on `PATH`. Schema, upload, chunk-math, and fake-engine API tests still run.

Fake transcripts are available only when a test overrides the FastAPI transcriber dependency. No environment variable selects `FakeTranscriber`, and startup never falls back to it if model load fails.

### Observed pytest result

Command, from `c:\Work\voice_agent`, after `RUN_INTEGRATION` was unset:

```text
.\.venv\Scripts\python.exe -m pytest -ra
```

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Work\voice_agent
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.15.1
collected 63 items

tests\test_api.py ...........s.                                          [ 20%]
tests\test_audio.py ...............sss                                   [ 49%]
tests\test_chunking.py .......                                           [ 60%]
tests\test_health.py .......                                             [ 71%]
tests\test_integration.py s                                              [ 73%]
tests\test_validation.py .................                               [100%]

=========================== short test summary info ===========================
SKIPPED [1] tests\test_api.py:222: ffprobe and ffmpeg are not on PATH
SKIPPED [1] tests\test_audio.py:344: ffprobe and ffmpeg are not on PATH
SKIPPED [1] tests\test_audio.py:356: ffprobe and ffmpeg are not on PATH
SKIPPED [1] tests\test_audio.py:378: ffprobe and ffmpeg are not on PATH
SKIPPED [1] tests\test_integration.py:13: Set RUN_INTEGRATION=1 to run the real faster-whisper smoke test
======================== 58 passed, 5 skipped in 0.58s ========================
```

`py -3.11` was not installed, so this venv is Python 3.12.10. The four media skips are the real ffprobe/ffmpeg cases. The integration test did not download a model.

## Production path

This is the next design for long recordings or concurrent load. It is not implemented.

1. Store the upload in object storage.
2. Insert a durable job record and enqueue a message. Return `202` and a job id instead of holding the HTTP connection open.
3. Workers pull jobs, transcribe with the same chunk offset rules, and write the result.
4. `GET /v1/transcriptions/jobs/{job_id}` returns status and, when complete, the same body as the synchronous endpoint.
5. Retry transient failures with exponential backoff and a maximum attempt count. Keep permanent failures failed.
6. Make submission idempotent. Add metrics for queue depth, latency, failures, audio duration, and worker use.

## Docker

The `Dockerfile` uses `python:3.11-slim`, installs `ffmpeg`, and starts `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

`docker build` was run and did not succeed. The Docker CLI is installed; the daemon is not running:

```text
docker build -t transcription-service .
ERROR: error during connect: Head "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/_ping": open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

Do not treat the image as built. Start Docker Desktop and run the command in [docs/INSTALLATION.md](docs/INSTALLATION.md) before relying on the container.
