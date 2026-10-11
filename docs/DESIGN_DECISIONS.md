# Audio Transcription API — design decisions

This document records the design of this transcription service and what the repository implements. Install steps are in [INSTALLATION.md](INSTALLATION.md). Request examples and the error table are in [USAGE.md](USAGE.md). The behavior reference is [TECHNICAL_PLAN.md](TECHNICAL_PLAN.md). Approaches left out of the code are in [ALTERNATIVES.md](ALTERNATIVES.md). The later production design is in [ENTERPRISE_SCALE.md](ENTERPRISE_SCALE.md).

## 1. Overview

The service accepts a WAV or MP3 upload, validates and normalizes it, transcribes speech with faster-whisper, and returns segment-level timestamps. The work finishes inside `POST /v1/transcriptions`. The audio and the transcript are not stored after the response.

The goal is a small, testable pipeline. A distributed job platform is described for later and is not implemented here.

## 2. Requirements covered

| Requirement | What the code does |
| --- | --- |
| Accept audio uploads such as WAV and MP3 | `POST /v1/transcriptions` reads multipart field `file`. ffprobe must report an audio stream whose format name includes `wav` or `mp3`. |
| Convert speech to text | faster-whisper transcribes the normalized WAV. Segment texts are joined with a single space. |
| Return timestamps for each segment | Each segment has `start`, `end`, and `text` in seconds from the start of the original file. |
| Validate and normalize supported audio | ffprobe checks metadata. FFmpeg converts supported audio to 16 kHz mono WAV. Empty, oversized, unsupported, undecodable, and over-duration files return a stable error body. |
| Process long audio safely | Files longer than 120 seconds and at most 10 minutes are chunked inside the same request. Files over 10 minutes are rejected. A failed chunk fails the whole request. |

## 3. Architecture

```text
Client
  |
  | POST /v1/transcriptions (multipart/form-data, field name: file)
  v
FastAPI endpoint
  |
  +--> Stream the upload, count bytes, reject empty and oversized files
  +--> ffprobe metadata, then FFmpeg normalize to 16 kHz mono WAV
  +--> Transcriber interface
  |       +--> FasterWhisperTranscriber in production
  |       +--> FakeTranscriber only when a test overrides the dependency
  +--> One-shot transcribe, or sequential chunks with global timestamps
  +--> Validate segment times before the response
  |
  v
JSON response
finally: delete the upload, the normalized WAV, and any chunk files
```

The route handles HTTP. `app/services/audio.py` handles streaming, probe, and normalization. `app/services/chunking.py` handles offsets and overlap. `app/services/transcriber.py` is the engine protocol, and `app/services/faster_whisper.py` is the production adapter. The model can be replaced in tests without changing the API schema.

`GET /health` reports that the process is up. `GET /ready` is 200 only after `load()` succeeds. A failed load is logged, the process stays up, and both `/ready` and a later upload return `503` `ENGINE_UNAVAILABLE`.

## 4. Technology decisions

| Component | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.11 (3.12 runs the same code) | Matches the service target and the faster-whisper wheels. The Dockerfile uses `python:3.11-slim`. |
| API | FastAPI and Uvicorn | Multipart uploads, typed response models, and OpenAPI at `/docs`. |
| ASR engine | faster-whisper | Local transcription with segment timestamps. Imported only inside adapter `load()`. |
| Audio tools | FFmpeg `ffprobe` and `ffmpeg` | Metadata, normalization, and chunk extraction. Argument lists, no shell. |
| Validation | Pydantic v2 | Response models and environment settings. Invalid limits fail at startup. |
| Tests | pytest and httpx | API tests with a dependency-injected fake transcriber. |

These are the choices for this prototype. They would be revisited if measured accuracy, latency, cost, deployment limits, or privacy requirements changed.

## 5. Why faster-whisper?

faster-whisper runs locally and returns text with segment timestamps. Audio stays on the machine that runs the process. No speech API key is required.

The application owns model deployment, the PyAV pin, CPU capacity, and model size. `WHISPER_MODEL=base` with `cpu` and `int8` is the default. It is a speed and memory tradeoff, not a measured accuracy claim. Accuracy and throughput depend on model size, hardware, language, accent, noise, and recording quality. Names such as `small` or `medium` need more RAM and a process restart so `load()` runs again.

**Alternatives considered**

- **Hosted speech-to-text API.** It can remove model operations, and it adds provider cost, a network dependency, and a decision about where audio is sent. This service does not call one.
- **Another local ASR model.** It might fit a language or latency target better. It should be chosen with representative audio. The `Transcriber` protocol is the seam for that change.
- **Custom training or fine-tuning.** This repository is a transcription pipeline. It does not train a model.

A later comparison would use word error rate, latency, real-time factor, resource usage, and cost per audio minute on a set of recordings this service does not ship.

## 6. Why FastAPI?

The service needs one upload route, typed JSON, and generated docs. FastAPI provides those with `UploadFile`, Pydantic response models, and `/docs`. Flask or Django REST Framework could host the same pipeline. FastAPI fits this small service. It is not a claim that it is the right framework for every workload.

Blocking probe, normalize, chunk extraction, and `transcribe()` run in `run_in_threadpool`. The route stays `async` for the upload read, so `/health` can still be served while inference runs.

## 7. Audio format handling

The client filename and `Content-Type` are not proof of audio. The response `filename` is the client basename only. `/` and `\` are separators, and that name is never a filesystem path.

ffprobe is called as an argument list with `FFPROBE_TIMEOUT_SECONDS` (default 30). It checks three things: an audio stream exists, the format name includes `wav` or `mp3`, and the duration is finite and greater than zero. A successful probe does not prove the whole file decodes.

FFmpeg then converts the file to 16 kHz mono WAV with `FFMPEG_TIMEOUT_SECONDS` (default 60). A timeout, non-zero exit, or empty output deletes that WAV and returns `422` `UNDECODABLE_AUDIO`. FLAC, M4A, and OGG are not allowlisted. They were not given an end-to-end normalization test here, so they return `415` `UNSUPPORTED_MEDIA`.

## 8. API contract

### `POST /v1/transcriptions`

Multipart field name: `file`.

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/transcriptions" -F "file=@sample.wav"
```

Success `200`. `filename` is part of the implemented schema. `transcription_id` is a new UUID4 and is not stored.

```json
{
  "transcription_id": "3f1c0c2e-7b2a-4d6e-9a1f-0c8e5b7a4d21",
  "filename": "sample.wav",
  "status": "completed",
  "language": "en",
  "duration_seconds": 12.5,
  "text": "Hello and welcome.",
  "segments": [
    {"start": 0.0, "end": 2.4, "text": "Hello and welcome."}
  ]
}
```

`start` and `end` are seconds from the beginning of the original recording. They are segment bounds from the model, shifted by the chunk offset when chunking runs. They are not word-level alignment. The service does not invent times by slicing `text` into equal parts. `language` is null only when the engine does not provide one. `duration_seconds` comes from ffprobe.

Before the response is sent, times must be finite, `start >= 0`, `end >= start`, and in chronological order. When duration is known, `end` must be within `duration + 0.5` seconds. A segment that fails those checks is `500` `TRANSCRIPTION_FAILED`.

`GET /health` returns `{ "status": "ok" }`. `GET /ready` returns `{ "status": "ready", "model": "base", "device": "cpu" }` after load, or `503` when the model is not loaded.

## 9. Long-audio strategy

**Current implementation:** The prototype enforces a maximum duration of 600 seconds and rejects longer recordings with `422` `DURATION_EXCEEDED`. Recordings longer than `CHUNK_SECONDS` (default 120) and within that cap are split inside the same HTTP request. They are not queued.

Chunk rule, with the default 2 second overlap:

```text
step = CHUNK_SECONDS - CHUNK_OVERLAP_SECONDS
offset_i = i * step
global_start = offset_i + local_start
global_end = offset_i + local_end
```

FFmpeg extracts each window with `-ss <offset> -t CHUNK_SECONDS`. The last chunk may be shorter. On chunks after the first, a segment is dropped when its global end is at or before `offset + CHUNK_OVERLAP_SECONDS`. A repeated word later than that window is kept. Overlap can still duplicate words outside that window. Chunk size and overlap should be judged on real recordings. This repository tests the offset math and the drop rule. It does not claim the 120/2 defaults are optimal for every language.

Chunks of one file run one after another. If any chunk fails, the HTTP call fails and no partial transcript is returned. `status` is `completed` only for a full success.

For production jobs beyond this cap, the next design is object storage, a durable queue, workers, persistent job state, bounded retries, and `202` plus `GET /v1/transcriptions/jobs/{job_id}`. Those pieces are not in this process. In-process background tasks are not used in their place. Detail is in [ENTERPRISE_SCALE.md](ENTERPRISE_SCALE.md).

## 10. Validation, errors, and resource limits

The service does the following.

- Caps uploads at `MAX_UPLOAD_BYTES` (default 25 MiB). `Content-Length`, when present and over the cap, is rejected before a temp file is created. The body is still read in 1 MiB chunks and counted. The extra chunk is not written.
- `Content-Length` includes multipart framing, so a file very close to the cap can be rejected before the byte counter runs. A reverse proxy needs its own body limit as well.
- Applies `MAX_DURATION_SECONDS` (default 600) and rejects longer media before inference.
- Rejects an empty file, a non-multipart body, a file with no audio stream, a format outside WAV and MP3, and audio that ffprobe or FFmpeg cannot decode.
- Writes server-generated temp names (`*.upload`, `*.norm.wav`, `*.chunk.wav`). `save_upload` deletes a partial file when the write fails. The route deletes every artifact in `finally` on success and on failure.
- Returns one error shape. The body has `error.code` and `error.message`. It has no stack trace and no local path.

```json
{
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "The uploaded file exceeds the configured size limit."
  }
}
```

| HTTP | `error.code` | When |
| --- | --- | --- |
| 400 | `EMPTY_UPLOAD` | Missing `file` field, or zero bytes. |
| 400 | `INVALID_REQUEST` | Malformed multipart body. |
| 413 | `FILE_TOO_LARGE` | Header or streamed bytes exceed `MAX_UPLOAD_BYTES`. |
| 415 | `UNSUPPORTED_MEDIA` | No audio stream, or format other than WAV or MP3. |
| 422 | `UNDECODABLE_AUDIO` | Probe, normalize, or chunk extraction failed, timed out, or saw a non-positive duration. |
| 422 | `DURATION_EXCEEDED` | Duration is over `MAX_DURATION_SECONDS`. |
| 503 | `ENGINE_UNAVAILABLE` | The model is not loaded. |
| 500 | `TRANSCRIPTION_FAILED` | The file was valid and the engine failed, or segment times failed validation. |
| 500 | `INTERNAL_ERROR` | Unexpected server failure. |

- Loads `WhisperModel` once per process during startup. `transcribe()` holds a `threading.Lock` until the segment generator is consumed, including once per chunk. One inference runs at a time in that process.
- Keeps FFmpeg and model inference off the event loop via `run_in_threadpool`.
- Does not claim an inference timeout. `FFPROBE_TIMEOUT_SECONDS` and `FFMPEG_TIMEOUT_SECONDS` are enforced. There is no `TRANSCRIBE_TIMEOUT_SECONDS`, because cancelling the coroutine does not stop native inference. The synchronous cap is the duration limit plus the single-flight lock.

Each transcription logs request id, HTTP status, byte size, duration, elapsed milliseconds, and error code. `X-Request-ID` is reused when the client sends one safe line of at most 200 characters. Otherwise the server generates a UUID4. Audio bytes and transcript text are not logged.

Authentication and rate limits are omitted. Size and duration limits are the abuse controls in this prototype. Do not expose this process to the public internet as it is.

## 11. Testing strategy

Default `pytest` uses `FakeTranscriber` through the FastAPI dependency. No environment variable selects that fake, and startup never falls back to it if model load fails. Default tests do not import or download faster-whisper. They cover API behavior, response mapping, upload limits, chunk offsets, and error codes. They do not prove that the real model and FFmpeg work together.

The real-model smoke test is `tests/test_integration.py`. It runs only when `RUN_INTEGRATION=1`. Tests that need the `ffmpeg` and `ffprobe` binaries skip, with a reason, when those tools are missing.

Covered cases include:

- `GET /health` and `GET /ready`, including model-not-loaded.
- Empty upload, the exact byte cap, one byte over, an unsafe basename, and a partial write, with no leftover `*.upload`.
- WAV and MP3 probe payloads accepted, and other format names rejected.
- Corrupt, text, and over-duration mapping.
- A WAV through the fake engine, ordered segments, and temp files removed.
- The engine raising, a 500 body without a traceback, and temp files removed.
- Three chunks: global offsets, overlap drop, and a later repeated word kept.
- A later chunk raising: the error propagates, no partial transcript, chunk files removed.
- Segment schema rejection for NaN, negative start, `end < start`, out-of-order starts, and an end more than 0.5 seconds past the duration.

On the machine used for this repository, after `RUN_INTEGRATION` was unset:

```text
.\.venv\Scripts\python.exe -m pytest -ra
```

58 passed, 5 skipped. The skips are three ffprobe/ffmpeg cases in `tests/test_audio.py`, one API media case, and the integration smoke test. Word-error-rate evaluation against a human transcript is not implemented.

Run the default suite with `pytest` from the repository root.

## 12. Running locally

From the repository root, with Python 3.11 when it is available. This machine used Python 3.12.10 because `py -3.11` was not installed.

```bash
python -m venv .venv
```

Activate that environment, install dependencies, and confirm `ffmpeg` and `ffprobe` are on `PATH`:

```bash
pip install -r requirements.txt
```

`requirements.txt` pins PyAV below 19. faster-whisper 1.2.1 calls `av.open(..., metadata_errors="ignore")`, which PyAV 19 removed. The first process start downloads `WHISPER_MODEL` (default `base`).

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/docs . Wait until `GET /ready` is 200 before sending audio. Full install notes, including Windows FFmpeg, are in [INSTALLATION.md](INSTALLATION.md).

## 13. Limitations and production path

This prototype is the core pipeline. It has no database, object storage, durable queue, job table, or background worker. The caller waits on the HTTP connection. A 10 minute file on CPU can take longer than playback. Concurrent uploads are accepted, and inference is serialized by the in-process lock.

When the workload needs it, production would add:

- Asynchronous job creation and `GET /v1/transcriptions/jobs/{job_id}`.
- Object storage and a durable queue for long recordings.
- Worker processes, each with its own model, plus bounded retries, idempotency, and lease recovery after a crash.
- Authentication, rate limits, and per-customer quotas.
- Metrics for queue depth, latency, failure rate, throughput, real-time factor, and worker resource use.
- Retention and deletion for audio and transcripts.
- A representative evaluation set for word error rate and timestamp quality.

Those items are specified in [ENTERPRISE_SCALE.md](ENTERPRISE_SCALE.md). They are not implemented in this repository.

## Closing note

The running service is the synchronous path: stream, probe, normalize, transcribe or chunk, validate segments, delete temp files, and return one JSON body. The design stays small enough to test, and the production path is written down separately so this process does not pretend to be a queue.
