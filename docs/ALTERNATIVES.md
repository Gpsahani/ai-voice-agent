# Approaches considered and left out

This document lists designs that were considered for the transcription service and explains why they are not in this repository. The decisions that were implemented are in [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md). The later production design is in [ENTERPRISE_SCALE.md](ENTERPRISE_SCALE.md).

The prototype is a synchronous HTTP service: one upload, one response, temp files deleted when the request ends. Anything that needs durable storage or a second process was written down and left unimplemented.

## Hosted speech APIs

Google Speech-to-Text, Amazon Transcribe, Azure Speech, and the hosted Whisper API can return transcripts without a local model.

They were left out because this service runs on a machine the operator controls. A hosted API adds a vendor account, audio leaving the process, and a per-minute cost. faster-whisper keeps inference local, returns segment times, and can be swapped behind the `Transcriber` protocol if a hosted engine is added later. Tests already prove that swap by injecting `FakeTranscriber`.

## A durable queue inside the prototype

The production sketch is: store the upload, insert a job, enqueue a message, return `202` and a job id, and let workers transcribe. Redis, SQS, RabbitMQ, or a database-backed queue would all fit that sketch.

They were left out of this repository because a queue without storage and a status API is an incomplete product. The prototype would still need object storage, a job table, retries, and `GET /v1/transcriptions/jobs/{job_id}`. Implementing half of that would look durable and would lose jobs on process exit. The synchronous route is complete for files inside the 25 MiB and 10 minute caps. The full job design stays in the enterprise document until those pieces exist together.

## `BackgroundTasks` or an in-memory queue

FastAPI `BackgroundTasks`, a `asyncio.Queue`, or a thread inside the same process could accept the upload and return quickly.

They were left out because they die with the process. There is no job id to poll, no retry after a crash, and a caller cannot tell success from a lost task. The technical plan treats that pattern as out of scope. A coroutine timeout also does not stop native faster-whisper once inference has started, so "run it in the background and cancel the task" does not recover a stuck model call.

## Several model calls at once in one process

The thread pool could run `transcribe()` for many requests together, and Uvicorn could be started with several workers that each think they share one model.

They were left out because one `WhisperModel` is not safe for overlapping `transcribe()` calls. The implemented control is a `threading.Lock` inside `transcribe()`, held until the segment generator is fully consumed. Extra Uvicorn workers would each load their own model and multiply RAM, which is a deployment choice for later, when each worker is sized for that. This prototype loads the model once per process and runs one inference at a time.

## An inference timeout setting

`FFPROBE_TIMEOUT_SECONDS` and `FFMPEG_TIMEOUT_SECONDS` bound the subprocesses. A `TRANSCRIBE_TIMEOUT_SECONDS` around `transcribe()` looks like the same idea.

It was left out because cancelling the Python task does not stop the native CTranslate2 work. The request could return an error while the model keeps using CPU inside the lock, and the next caller would wait on a call that the API already abandoned. The prototype bounds the problem with `MAX_DURATION_SECONDS` and the single-flight lock. Hard cancellation belongs in a worker process that can be killed. That is an enterprise-worker behavior, not a setting on this route.

## Trust the client `Content-Type` or the file extension

The route could accept anything labeled `audio/wav` or `audio/mpeg`, or anything whose name ends in `.wav` or `.mp3`.

That was left out because the client controls both the header and the filename. ffprobe reads the container. The allowlist is the format-name tokens `wav` and `mp3`. A text file, a file with no audio stream, or another container returns `415` `UNSUPPORTED_MEDIA` or `422` `UNDECODABLE_AUDIO`. The filename is only the basename returned in the JSON.

## Allow FLAC, M4A, and OGG now

FFmpeg can decode those containers, and the normalize command is the same shape for each.

They were left out of the allowlist because this repository did not give them an end-to-end normalization test. Shipping them as supported would promise a path that was not verified here. Adding a format later means extending the allowlist after that test exists.

## Decode with PyAV inside the request and skip FFmpeg

The environment already has PyAV because faster-whisper uses it. The service could read packets in Python and write the 16 kHz WAV itself.

That was left out because FFmpeg is the tool that already handles broken containers, sample-rate conversion, and chunk extraction with `-ss` and `-t`. Duplicating that in Python would be a second media stack to test. The service shells out with an argument list, captures the exit code, and applies `FFMPEG_TIMEOUT_SECONDS`. `shell=True` is not used.

## Use the client filename as the temp path

Saving to `TEMP_DIR / upload.filename` would make files easy to find while debugging.

That was left out because the filename is attacker-controlled. `../`, absolute paths, and backslashes would write outside the temp directory, and two clients could choose the same name. The implementation uses `NamedTemporaryFile`. The response still shows the client basename so the caller can recognize their file.

## Invent timestamps by splitting the transcript

If the model returned only plain text, the service could divide `duration_seconds` by the number of sentences and assign equal slices.

That was left out because those times would be false. Segment `start` and `end` come from faster-whisper, plus the chunk offset when the file is chunked. Empty model text is skipped. Segments that are non-finite, out of order, or more than 0.5 seconds past the probed duration fail the request with `TRANSCRIPTION_FAILED`.

## Return a partial transcript when a later chunk fails

A long file could return the chunks that succeeded and an error flag for the rest.

That was left out because the API contract is one completed transcript or one error. A partial body is easy for a client to store as if it were complete. Chunk files are deleted, and the caller can send the file again.

## Keep the audio and the transcript

Writing the upload and the JSON to disk or to a database would allow replay without a second upload.

That was left out because this prototype has no retention policy, no auth, and no delete API. Keeping speech without those controls is the wrong default. Temp files are removed in `finally`. `transcription_id` identifies the response and is not a key in a store. Repeat the upload to transcribe again.

## Fall back to the fake engine if the model fails to load

`FakeTranscriber` already returns stable text in tests. Startup could catch a load error and serve that text so the demo still responds.

That was left out because a production process would then look healthy while returning text that is not a transcript. Load failure is logged, `/ready` stays `503`, and `/v1/transcriptions` returns `ENGINE_UNAVAILABLE`. The fake exists only when a test overrides `get_transcriber`.

## Word timestamps, speaker diarization, and translation

faster-whisper can emit word times. A diarization model can label speakers. A translation pass can return another language.

They were left out because the contract is segment bounds and the spoken-language transcript. Word times are a different accuracy claim. Diarization is a second model and a second failure mode. Translation is a different product behavior. `language` is the detected or forced language code of the source audio.

## Accept a remote URL instead of a file upload

`POST` could take `{"url": "https://..."}` and download the audio.

That was left out because the service would then fetch arbitrary URLs from the server network (SSRF), follow redirects, and need a download size limit separate from the multipart reader. Callers send the bytes in the `file` field. The same streaming cap applies.

## Authentication and rate limits in the prototype

API keys, OAuth, and per-caller quotas are normal on a public transcription API.

They were left out so the prototype stays runnable with `curl` and the media pipeline can be called without an account system. The README states that omission. Size and duration limits are the only abuse controls. A public deployment needs the controls in [ENTERPRISE_SCALE.md](ENTERPRISE_SCALE.md) before it is exposed.

## What stays in this repository

The unimplemented pieces are deferred on purpose. The running service is the synchronous path: stream, probe, normalize, transcribe or chunk, validate segments, delete temp files, and return one JSON body. The next increment is the job pipeline, built as a whole, rather than a background task inside this process.
