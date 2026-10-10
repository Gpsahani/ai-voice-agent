# Usage

How to call the transcription prototype once it is running. Install and start commands are in [INSTALLATION.md](INSTALLATION.md). Behavior and error codes are defined in [TECHNICAL_PLAN.md](TECHNICAL_PLAN.md).

Default base URL: `http://127.0.0.1:8000`

## 1. Check the process

PowerShell:

```powershell
curl.exe http://127.0.0.1:8000/health
curl.exe http://127.0.0.1:8000/ready
```

bash:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

Health:

```json
{ "status": "ok" }
```

Ready, after the model has loaded:

```json
{ "status": "ready", "model": "base", "device": "cpu" }
```

If the model is not loaded, `/ready` returns HTTP 503:

```json
{
  "error": {
    "code": "ENGINE_UNAVAILABLE",
    "message": "The transcription engine is not ready."
  }
}
```

`/health` can be 200 while `/ready` is 503. Wait until `/ready` is 200 before sending audio. A 503 on `/ready` means a valid upload will also return 503. The service does not invent a transcript in that case.

## 2. Transcribe a file

Field name is `file`. WAV and MP3 are the supported formats. FLAC, M4A, and OGG are rejected as `UNSUPPORTED_MEDIA` because that allowlist was not verified end to end here.

PowerShell:

```powershell
curl.exe -s -X POST "http://127.0.0.1:8000/v1/transcriptions" `
  -F "file=@C:\path\meeting.mp3"
```

bash:

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/transcriptions" \
  -F "file=@/path/meeting.mp3"
```

Success, HTTP 200:

```json
{
  "transcription_id": "3f1c0c2e-7b2a-4d6e-9a1f-0c8e5b7a4d21",
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

| Field | Meaning |
| --- | --- |
| `transcription_id` | New UUID4 for this response. It is not stored after the response. |
| `filename` | Basename only. `/` and `\` are both treated as separators. The client name is never used as a filesystem path. |
| `status` | `completed` on this synchronous endpoint. |
| `language` | Language code from the model, or the code forced by `WHISPER_LANGUAGE`. Null only when the engine does not provide one. |
| `duration_seconds` | Duration from ffprobe when the probe succeeded. |
| `text` | Segment texts joined with a single space, then stripped. |
| `segments` | Model segments after chunk-offset mapping. `start` and `end` are seconds from the beginning of the file. |

There is no word-level timing. `start` and `end` are segment bounds from the model, shifted by the chunk offset when the file is chunked. They are approximate. The service does not invent times by splitting `text` into equal slices.

The call blocks until transcription finishes. Defaults are 25 MiB, 10 minutes, and 120-second chunks with 2 seconds of overlap. Only one transcription runs the model at a time; a second request waits on that lock inside a worker thread, not on the event loop. `/health` can still be served while inference runs.

### Chunking

If ffprobe duration is greater than `CHUNK_SECONDS` and not greater than `MAX_DURATION_SECONDS`:

- step = `CHUNK_SECONDS - CHUNK_OVERLAP_SECONDS`
- chunk `i` starts at `i * step` and is extracted with `ffmpeg -ss <offset> -t CHUNK_SECONDS`
- the last chunk may be shorter than `CHUNK_SECONDS`
- local segment times are converted with `global = offset + local`
- on chunks after the first, a segment is dropped when its global end is at or before `offset + CHUNK_OVERLAP_SECONDS`
- a later repeated word outside that window is kept
- if any chunk fails, the HTTP call fails and no partial transcript is returned

Files at or under `CHUNK_SECONDS` are transcribed once from the normalized WAV.

## 3. Errors

Every error uses the same JSON shape. The body does not include a stack trace, a local path, or model internals.

```json
{
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "The uploaded file exceeds the configured size limit."
  }
}
```

| HTTP | `error.code` | Typical cause |
| --- | --- | --- |
| 400 | `EMPTY_UPLOAD` | No `file` field, or the file has zero bytes. |
| 400 | `INVALID_REQUEST` | Body is not valid multipart form data. |
| 413 | `FILE_TOO_LARGE` | `Content-Length`, when present, or the streamed file bytes exceed `MAX_UPLOAD_BYTES`. |
| 415 | `UNSUPPORTED_MEDIA` | No audio stream, or the format is not WAV or MP3. |
| 422 | `UNDECODABLE_AUDIO` | Probe, normalization, or chunk extraction failed, timed out, returned invalid JSON, or saw a non-positive duration. |
| 422 | `DURATION_EXCEEDED` | Duration is over `MAX_DURATION_SECONDS`. |
| 503 | `ENGINE_UNAVAILABLE` | Model is not loaded. |
| 500 | `TRANSCRIPTION_FAILED` | The file was valid and the engine failed, or segment times were invalid (including an end more than 0.5 seconds past the known duration). |
| 500 | `INTERNAL_ERROR` | Unexpected server failure. |

Example, empty file:

```powershell
curl.exe -s -D - -X POST "http://127.0.0.1:8000/v1/transcriptions" -F "file=@C:\path\empty.wav"
```

```json
{
  "error": {
    "code": "EMPTY_UPLOAD",
    "message": "The uploaded file is missing or empty."
  }
}
```

A response header `X-Request-ID` is set on every response. Send one to reuse it; otherwise the server generates a UUID4. The server log line for a transcription uses the same id and records status, byte size, duration, elapsed milliseconds, and error code. It does not include the audio bytes or the transcript.

## 4. OpenAPI

With the server running, open http://127.0.0.1:8000/docs and use **POST /v1/transcriptions**.

## 5. Language and model

Leave `WHISPER_LANGUAGE` empty to auto-detect. Set it to a language code such as `en` when the language is known.

`WHISPER_MODEL=base` is the prototype default. It is a speed and quality tradeoff, not a measured accuracy claim. Names such as `small` or `medium` increase CPU time and RAM. Change the variable and restart so the model loads again.

## 6. Limits that affect callers

- Synchronous HTTP. Clients need a timeout longer than the recording. A 10-minute file on CPU can take longer than playback. There is no inference timeout setting; `FFMPEG_TIMEOUT_SECONDS` limits FFmpeg only.
- Files over `MAX_DURATION_SECONDS` are rejected. They are not chunked and they are not queued.
- Files between `CHUNK_SECONDS` and `MAX_DURATION_SECONDS` are chunked inside this request.
- The uploaded bytes and the transcript are not stored. Temp files are deleted when the request finishes, including when it fails.
- No API key is checked. Do not expose this process to the public internet as-is.
- Results vary with noise, accents, sample rate, and model size.

## 7. What this API does not do

- It does not accept a URL to a remote audio file. Send the bytes.
- It does not return a job id. There is no `POST /v1/transcriptions/jobs`.
- It does not diarize speakers, translate, or return word timestamps.
- It does not keep history. Repeat the upload to transcribe again.
- It does not authenticate callers.
