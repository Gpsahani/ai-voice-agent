# Technical Plan — Audio Transcription API

Implementation reference for the transcription prototype. Installation and request examples live in [INSTALLATION.md](INSTALLATION.md) and [USAGE.md](USAGE.md).

## 1. Goal

The service:

1. Accepts WAV and MP3 over HTTP. Other containers are rejected.
2. Rejects empty, oversized, unsupported, undecodable, and over-duration uploads with a stable error body.
3. Inspects metadata with ffprobe, then normalizes supported audio with FFmpeg to 16 kHz mono WAV. ffprobe does not prove that the whole file decodes.
4. Transcribes speech with faster-whisper behind a replaceable interface. Tests override that interface; production never returns fake text.
5. For audio longer than `CHUNK_SECONDS` and within `MAX_DURATION_SECONDS`, transcribes sequential overlapping chunks and maps local timestamps back onto the original timeline.
6. Returns a joined transcript plus segment `start`, `end`, and `text`.
7. Deletes temporary upload, normalized, and chunk files after success and failure.
8. Lets default unit tests run with a mock engine and no model download.

Do not train a model. Do not add a queue, database, object store, or `BackgroundTasks` in this prototype. The production path is described in the README only.

## 2. Stack

| Piece | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11 (3.12 runs the same code) | Matches the service target and faster-whisper wheels. |
| HTTP | FastAPI + Uvicorn | Upload handling and OpenAPI docs. |
| Validation | Pydantic v2 and pydantic-settings | Response models and environment settings. |
| Speech | faster-whisper | Local segment timestamps. Imported only inside adapter `load()`. |
| Media | FFmpeg `ffprobe` and `ffmpeg` | Metadata, normalization, and chunk extraction. Argument lists, no shell. |
| Tests | pytest + httpx | API tests with a dependency-injected fake transcriber. |
| Packaging | `requirements.txt` + Dockerfile | Local install and a container definition. |

## 3. Layout

Implemented at the repository root, next to `docs/`.

```text
voice_agent/
├── app/
│   ├── main.py                 # FastAPI app, lifespan, route, exception handlers
│   ├── config.py               # Environment settings
│   ├── schemas.py              # Response and error models
│   ├── dependencies.py         # Transcriber injection
│   ├── services/
│   │   ├── audio.py            # Stream upload, ffprobe, normalize, cleanup
│   │   ├── chunking.py         # Offsets, extraction, overlap dedup
│   │   ├── transcriber.py      # Protocol and shared dataclasses
│   │   ├── fake.py             # Test double, never selected in production
│   │   └── faster_whisper.py   # Real engine adapter
│   └── core/
│       ├── errors.py           # App exceptions and HTTP mapping
│       └── logging.py          # Log setup, no transcript logging
├── tests/
│   ├── conftest.py
│   ├── helpers.py
│   ├── test_health.py
│   ├── test_validation.py
│   ├── test_api.py
│   ├── test_audio.py
│   ├── test_chunking.py
│   └── test_integration.py
├── docs/
│   ├── TECHNICAL_PLAN.md
│   ├── INSTALLATION.md
│   └── USAGE.md
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

Routes stay thin. Validation, media preparation, chunking, and transcription live in services. Blocking inference and FFmpeg run in a worker thread via `starlette.concurrency.run_in_threadpool`.

## 4. Runtime flow

```text
POST /v1/transcriptions
        |
        v
if Content-Length is present and greater than MAX_UPLOAD_BYTES: FILE_TOO_LARGE
stream UploadFile to a server-generated temp path in 1 MiB reads
count bytes; if the next chunk would exceed the cap, delete the partial and reject
empty file -> EMPTY_UPLOAD
save_upload deletes its own partial file if writing fails
        |
        v
if the injected engine is not loaded: ENGINE_UNAVAILABLE
        |
        v
ffprobe metadata only: audio stream, allowlisted format, finite duration > 0
duration over MAX_DURATION_SECONDS -> DURATION_EXCEEDED
        |
        v
ffmpeg normalize to 16 kHz mono WAV
failure, timeout, or empty output -> UNDECODABLE_AUDIO and delete that WAV
        |
        v
duration <= CHUNK_SECONDS: one transcribe() of the normalized WAV
duration > CHUNK_SECONDS: extract chunks, transcribe each, shift timestamps, drop overlap
        |
        v
validate segments (finite, start >= 0, end >= start, chronological,
end <= duration + 0.5 when duration is known)
        |
        v
JSON 200
        |
        finally: delete upload, normalized WAV, and chunk files
```

Model load happens once per process during lifespan startup, not per request. If `load()` raises, the exception is logged, `loaded` stays false, and the process keeps serving `/health`. `/ready` then returns 503. There is no fallback transcript.

`transcribe()` fully iterates the faster-whisper segment generator while holding a `threading.Lock`. The route does not acquire that lock. The lock is taken inside the threadpool worker, including when chunking calls `transcribe()` once per chunk.

## 5. API contract

### `GET /health`

Process is up. Does not check the model.

```json
{ "status": "ok" }
```

### `GET /ready`

`200` when the transcriber loaded. `503` with `ENGINE_UNAVAILABLE` when it did not.

```json
{ "status": "ready", "model": "base", "device": "cpu" }
```

### `POST /v1/transcriptions`

`multipart/form-data` field name: `file`.

Success `200`:

```json
{
  "transcription_id": "uuid",
  "filename": "meeting.mp3",
  "status": "completed",
  "language": "en",
  "duration_seconds": 83.42,
  "text": "Hello everyone. Let's begin.",
  "segments": [
    { "start": 0.0, "end": 2.4, "text": "Hello everyone." },
    { "start": 2.4, "end": 4.8, "text": "Let's begin." }
  ]
}
```

Rules:

- `transcription_id` is a new UUID4 generated by the server.
- `filename` is the client basename only. Both `/` and `\` are separators. Never use the client name as a filesystem path. The temp file comes from `tempfile.NamedTemporaryFile(delete=False, suffix=".upload")`.
- `text` is segment texts joined with a single space, stripped. Empty segment text is skipped.
- Timestamps come from the engine, plus the chunk offset when chunking runs. Never invent them by splitting plain text.
- Segment times are approximate segment bounds, not word-level alignment.
- `language` may be null only if the engine does not provide it.
- `duration_seconds` prefers ffprobe. The response is rejected if a segment end is more than 0.5 seconds past that duration.

Error body, every failure:

```json
{
  "error": {
    "code": "UNDECODABLE_AUDIO",
    "message": "The uploaded audio could not be decoded."
  }
}
```

| Status | Code | When |
| --- | --- | --- |
| 400 | `EMPTY_UPLOAD` | Missing file field or zero bytes. |
| 400 | `INVALID_REQUEST` | Malformed multipart body. |
| 413 | `FILE_TOO_LARGE` | `Content-Length` when present, or streamed bytes, exceed `MAX_UPLOAD_BYTES`. The extra chunk is not written. |
| 415 | `UNSUPPORTED_MEDIA` | No audio stream, or format not allowlisted. |
| 422 | `UNDECODABLE_AUDIO` | Probe/normalize/chunk failure, timeout, invalid JSON, or non-positive duration. |
| 422 | `DURATION_EXCEEDED` | Duration exceeds `MAX_DURATION_SECONDS`. |
| 503 | `ENGINE_UNAVAILABLE` | Model failed to load or the injected engine is not ready. |
| 500 | `TRANSCRIPTION_FAILED` | Engine raised after a valid file, or segment output failed validation. No stack trace in the body. |
| 500 | `INTERNAL_ERROR` | Anything else unexpected. No stack trace in the body. |

Supported formats are WAV and MP3 (`format_name` tokens `wav` and `mp3`). FLAC, M4A, and OGG are not allowlisted. They were not given an end-to-end normalization test on a machine with FFmpeg.

Do not add job endpoints. Synchronous processing is the prototype. Concurrency of the model is one in-process lock.

Multipart parsers may spool the body before the route reads `UploadFile`. Configure a reverse-proxy body limit in deployment. The route still checks `Content-Length` when that header is present and still counts file bytes.

## 6. Configuration

Read from the environment. Defaults live in code and `.env.example`. No machine-specific paths and no secrets. `get_settings()` is cached with `lru_cache`. Non-positive limits fail validation. `CHUNK_OVERLAP_SECONDS` must be `>= 0` and `< CHUNK_SECONDS`. Empty `TEMP_DIR` means the system temp directory. Empty `WHISPER_LANGUAGE` means auto-detect.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | Hard cap. Header check plus streaming counter. |
| `MAX_DURATION_SECONDS` | `600` | Reject longer audio before inference. |
| `CHUNK_SECONDS` | `120` | Chunk when duration is greater than this. |
| `CHUNK_OVERLAP_SECONDS` | `2` | Overlap removed from later chunks. |
| `TEMP_DIR` | empty (system temp) | Directory for server-named temp files. |
| `WHISPER_MODEL` | `base` | faster-whisper model size. |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda`. |
| `WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 compute type. |
| `WHISPER_LANGUAGE` | empty | Empty means auto-detect. |
| `FFPROBE_TIMEOUT_SECONDS` | `30` | ffprobe subprocess timeout. |
| `FFMPEG_TIMEOUT_SECONDS` | `60` | Normalization and chunk-extraction timeout. |
| `LOG_LEVEL` | `INFO` | Standard library log level. |

`TRANSCRIBE_TIMEOUT_SECONDS` is not a setting. It is not enforced. FFmpeg is the subprocess that has a timeout.

## 7. Design decisions

1. **Interface first.** `Transcriber` is a `Protocol`. Production lifespan constructs `FasterWhisperTranscriber` only. Tests override `get_transcriber` with `FakeTranscriber`. No environment flag selects the fake. Load failure does not switch to the fake.
2. **One model instance.** `load()` imports `faster_whisper` and builds `WhisperModel` once. Importing `app` does not import or download the model.
3. **Off the event loop.** The route is `async` for the upload read. Probe, normalize, chunk extraction, and `transcribe()` run inside `run_in_threadpool`. The inference lock is acquired inside `transcribe()`, on that worker.
4. **Streaming size limit.** Read `UploadFile` in 1 MiB chunks and count bytes. If the next chunk would exceed `MAX_UPLOAD_BYTES`, delete the partial file and raise `FILE_TOO_LARGE` without writing that chunk. Also reject when `Content-Length` is present and greater than the cap, before creating the temp file.
5. **Server temp names.** `tempfile.NamedTemporaryFile(delete=False, dir=TEMP_DIR or None, suffix=".upload")`. `cleanup()` ignores missing files. `save_upload` deletes its partial file in `finally` when it does not succeed. The route `finally` deletes the upload, the normalized WAV, and any chunk paths.
6. **ffprobe, argument list, no shell.** `["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]`. Timeout, non-zero exit, and invalid JSON map to `UNDECODABLE_AUDIO`, except a text-like file with no audio magic, which maps to `UNSUPPORTED_MEDIA`. No audio stream, or a format outside `{wav, mp3}`, maps to `UNSUPPORTED_MEDIA`. Non-positive or non-finite duration maps to `UNDECODABLE_AUDIO`. Duration over the cap maps to `DURATION_EXCEEDED`. A successful probe is metadata only.
7. **Normalize before transcription.** `ffmpeg -y -i <src> -ac 1 -ar 16000 -f wav <dest>`. Timeout is `FFMPEG_TIMEOUT_SECONDS`. Failure deletes the destination and raises `UNDECODABLE_AUDIO`.
8. **Chunking.** Used only when duration `> CHUNK_SECONDS`. `step = CHUNK_SECONDS - CHUNK_OVERLAP_SECONDS`. Offset `i` is `i * step`. Extract with `ffmpeg -y -ss <offset> -i <wav> -t <CHUNK_SECONDS> -ac 1 -ar 16000 -f wav <chunk>`. `global_start = chunk_offset + local_start` and the same for `end`. For chunk index `> 0`, drop segments whose global end is `<= chunk_offset + CHUNK_OVERLAP_SECONDS`. Do not drop a later repeated word outside that window. If any chunk raises, the error propagates and no partial transcript is returned. Chunk files are deleted in `finally`, including on failure. The synchronous cap remains `MAX_DURATION_SECONDS`.
9. **Segment check before response.** Skip empty text. Reject with `TRANSCRIPTION_FAILED` when any segment is non-finite, `start < 0`, `end < start`, starts go backwards, or, when duration is known, `end > duration + 0.5`.
10. **Consume the generator.** `transcribe()` iterates every segment inside the lock before it returns. Iteration errors are not replaced with empty text.
11. **Logs.** One info line per transcription: request id, status, byte size, duration, elapsed ms, error code. Never log audio bytes, transcript text, or environment secrets. Tracebacks for unexpected failures stay in the server log.
12. **Request id.** Middleware uses the incoming `X-Request-ID` when it is a single safe line up to 200 characters; otherwise it generates a UUID4.
13. **Public exposure.** No auth and no rate limit. Size and duration limits are the prototype's abuse controls.
14. **Long audio beyond the cap.** Reject. The production note is object storage, a durable queue, workers, and `202` plus a job id. Do not implement in-memory `BackgroundTasks` and call them durable.

## 8. Chunk timestamp rule

```text
step = CHUNK_SECONDS - CHUNK_OVERLAP_SECONDS
offset_i = i * step
global_start = offset_i + local_start
global_end = offset_i + local_end
drop when i > 0 and global_end <= offset_i + CHUNK_OVERLAP_SECONDS
```

The last chunk may be shorter because FFmpeg stops at end of file. Chunks run sequentially so the single inference lock is not asked to run two models at once.

## 9. Tests

| Case | Expected |
| --- | --- |
| `GET /health` | 200 `ok` |
| `GET /ready` model down | 503 `ENGINE_UNAVAILABLE` |
| `GET /ready` state marked loaded | 200 |
| Schema out of order, NaN, negative, `end < start`, end past duration + 0.5 | validation error |
| Empty upload, exact byte cap, one byte over, unsafe basename, partial write | stable codes and no leftover `*.upload` |
| WAV/MP3 probe payloads | accepted; other format names rejected |
| Corrupt / text / duration cap | 422, 415, 422 when ffprobe is available; mapping is also unit-tested without the binary |
| WAV via fake engine | 200, ordered segments, temp files removed |
| Engine raises | 500 body without `Traceback`, temp files removed |
| Three chunks | global offsets, overlap drop, later repeated word kept |
| Later chunk raises | exception, no partial return, chunk files removed |
| Default pytest | `faster_whisper` is not imported |
| `tests/test_integration.py` | skipped unless `RUN_INTEGRATION=1` |

`FakeTranscriber` is constructed only in tests. Default pytest monkeypatches `FasterWhisperTranscriber.load` so lifespan cannot download weights.

## 10. Production path (document only)

For files beyond the synchronous cap, or for concurrent load:

1. Store the upload in object storage.
2. Insert a job row and enqueue a durable message. Return `202` and `job_id`.
3. Workers pull jobs, transcribe with the same offset and overlap rules, and write the result.
4. `GET /v1/transcriptions/jobs/{job_id}` returns status and, when complete, the same body as section 5.
5. Retry transient failures with exponential backoff and a maximum attempt count. Permanent failures stay failed.
6. Isolate hard cancellation in a worker process that can be killed. Do not pretend a coroutine timeout stops native inference.

In-process background tasks are not a durable queue. They are not part of this service.

## 11. Acceptance checklist

- Valid WAV returns transcript and timestamped segments when FFmpeg and the model are available.
- MP3 is allowlisted and normalized through the same FFmpeg command when FFmpeg can decode it.
- Timestamps are numeric, chronological, and `end >= start`, and they stay within the probed duration plus 0.5 seconds.
- Empty and oversized uploads fail with the codes in section 5.
- Audio longer than one chunk and within the duration cap is chunked. Audio past the duration cap is rejected.
- Temp files are removed after success and failure.
- Client errors never include a stack trace.
- `pytest` passes with the mock engine and does not download a model.
- README, `docs/INSTALLATION.md`, and `docs/USAGE.md` match this behavior.
- Synchronous limits versus the section 10 production path are explained in the README.
- Auth is omitted on purpose.
