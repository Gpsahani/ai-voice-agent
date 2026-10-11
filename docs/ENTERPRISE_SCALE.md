# Scaling, failure handling, and an enterprise deployment

This document describes how the transcription prototype becomes a system that accepts concurrent load, keeps jobs across process restarts, and operates as a multi-tenant service. It is a design. It is not implemented in this repository. The running service is still the synchronous `POST /v1/transcriptions` route documented in [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md).

## What the prototype already guarantees

Those guarantees stay in the enterprise design. Workers reuse them.

- Stream the upload with a hard byte cap. A chunk that would pass the cap is not written.
- Identify WAV and MP3 with ffprobe. Normalize to 16 kHz mono WAV with FFmpeg before inference.
- Reject audio longer than the configured duration before the model runs.
- Map chunk-local timestamps with `global = chunk_offset + local`, and drop the overlap window on later chunks.
- If any chunk fails, fail the job. Do not store a partial transcript as success.
- Validate segment times before the result is visible to the caller.
- Delete local temp files on success and on failure.
- Log job id, status, bytes, duration, elapsed time, and error code. Do not log audio bytes or transcript text.
- Run one inference at a time per model instance.

The prototype holds the HTTP connection until transcription finishes, stores nothing, and serializes inference with one in-process lock. That is the limit this design removes.

## Split intake from inference

```text
client
  |
  |  POST /v1/transcriptions
  v
API tier
  |  store bytes, insert job, enqueue
  |  202 { "job_id" }
  v
object storage          job table           queue
  ^                       ^                   |
  |                       |                   v
  +-------- workers pull a job, transcribe, write the result
                                  |
                                  v
                    GET /v1/transcriptions/jobs/{job_id}
```

1. The API streams the upload into object storage under a key the client does not choose. The same byte cap applies. The original filename is metadata, never the object key.
2. A job row is inserted: `job_id`, object key, status `queued`, byte size, owner, attempt count, and timestamps.
3. A durable message is published. The API returns `202` and `job_id`. The connection is no longer tied to inference time.
4. Workers pull jobs. Each worker probes, normalizes, chunks, and transcribes with the same rules as the prototype, then writes the transcript onto the job.
5. `GET /v1/transcriptions/jobs/{job_id}` returns `queued`, `running`, `completed`, or `failed`. On `completed`, the body matches today's success JSON. On `failed`, the body uses today's error shape.

The synchronous route can remain for short files. Past the duration cap, or whenever the caller opts in, the job route is the one that accepts the work.

In-process `BackgroundTasks` are not this queue. A restart would drop them, and the caller would have no status to read.

## How concurrent uploads are handled

Intake scales separately from the model.

- The API tier is stateless. Several API processes can accept uploads. They write to object storage and the queue. They do not load `WhisperModel`.
- Each object key is unique. Two uploads cannot overwrite each other.
- A reverse proxy enforces a body limit. The API still counts bytes while streaming.
- Queue depth is the admission control. When the queue or the in-flight cap is full, the API returns `429` and does not accept more audio. Callers retry with backoff.
- Each worker process loads one model and runs one transcription at a time, which is the same rule as the current lock. Scale by adding worker processes, each with its own model and its own RAM budget. Do not add threads that call one shared `WhisperModel` together.
- CPU workers and GPU workers can be separate pools, selected by config (`WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`) and by queue. A GPU pool is a capacity change, not a change to the timestamp rules.
- FFmpeg stays inside the worker, next to the job it belongs to, so a burst of uploads does not start an unbounded number of ffmpeg processes on the API machines.

`/health` still means the process is up. `/ready` on an API instance means it can reach storage and the queue. `/ready` on a worker means the model loaded. A worker that failed to load the model does not pull jobs and does not emit a fake transcript.

## How jobs are stored

| Data | Where | Why |
| --- | --- | --- |
| Original audio | Object storage | Large, durable, and independent of the API process. |
| Normalized WAV and chunks | Worker local disk only | Scratch space. Deleted when the job attempt finishes. |
| Job status, owner, attempts, error code | Database | Small rows, queries, and idempotency. |
| Transcript and segments | Database, or object storage when the JSON is large | The `GET` job API can return the same shape as the synchronous response. |
| Queue message | Durable queue | Survives API and worker restarts. The message carries `job_id`, not the audio bytes. |

Retention is explicit. Audio and transcripts expire on a configured lifetime, and a delete API removes both the object and the row. The prototype deletes everything at the end of the request because it has no retention policy. An enterprise deployment needs one before it keeps speech.

## How failure is handled

Failures are split into permanent and transient. Permanent failures are not retried. Transient failures are retried with exponential backoff and a maximum attempt count.

| Failure | Class | What the system does |
| --- | --- | --- |
| Empty file, bad multipart, unsupported format, duration over the cap, file over the byte cap | Permanent | Mark `failed` with the same code the prototype returns (`EMPTY_UPLOAD`, `INVALID_REQUEST`, `UNSUPPORTED_MEDIA`, `DURATION_EXCEEDED`, `FILE_TOO_LARGE`). |
| ffprobe or ffmpeg timeout, corrupt audio, empty normalize output | Permanent after the media tools have actually failed | `UNDECODABLE_AUDIO`. Retrying the same bytes will fail again. |
| Worker crash, machine loss, network blip talking to storage or the queue | Transient | Re-queue until the attempt cap. The audio object is still in storage, so the caller does not upload again. |
| Model process hung | Transient, then failed | Kill the worker process. A replacement worker picks up the job if attempts remain. A coroutine cancel is not treated as a stop. |
| Segment times fail validation, or the engine raises after a valid file | Transient up to the attempt cap, then `TRANSCRIPTION_FAILED` | The result is not published as `completed`. |
| Queue poison message or attempt cap exceeded | Terminal | Move to a dead-letter queue and alert. The job stays `failed`. An operator can replay it on purpose. |

Recovery rules:

- The job row is updated with a compare-and-set on status, so two workers cannot complete the same job.
- Submission is idempotent. The client sends an idempotency key. A repeat `POST` with the same key returns the existing `job_id` and does not enqueue a second copy.
- A crash mid-transcription leaves the job `running` with a lease. When the lease expires, another worker may take it. Temp files from the dead worker are on that machine's disk and are cleaned when the worker starts, or by a temp-directory reaper.
- A failed chunk fails the attempt. The stored result is never a prefix of the transcript presented as success.
- Clients recover by reading `GET /v1/transcriptions/jobs/{job_id}`. They do not need the original HTTP connection to stay open.

## How this becomes an enterprise service

### Identity and quotas

Every request carries an API credential. The job row records the owner. Quotas limit uploads per minute, concurrent jobs, and audio minutes per day. The prototype omits auth on purpose. A deployment that other teams call does not.

### Isolation

Workers run as separate processes, ideally separate containers, with a read-only root, a writable temp directory, and no inbound network except the queue and object storage. One tenant's file is one object key and one job. Temp paths remain server-generated. Customer audio is encrypted at rest in object storage and on the database volume, and in transit on every hop.

### Observability

Metrics, not transcript logs:

- queue depth and age of the oldest job
- jobs completed, failed, and dead-lettered
- attempt counts
- audio duration and byte size
- time from accept to complete
- worker CPU, RAM, and model-load state
- FFmpeg timeouts

Traces use the job id and the `X-Request-ID`. Alerts fire on queue age, dead-letter growth, and workers that are not ready. Dashboards show capacity. They do not show transcript text.

### Service levels

Publish a target for short jobs and a separate target for jobs near the duration cap, because CPU transcription can take longer than playback. The API tier has a tight latency target because it only stores and enqueues. Inference latency is a worker target. Error budgets page on elevated `5xx` and on queue delay, not on a single corrupt upload. Permanent `4xx` results are caller errors and stay out of that burn.

### Release and rollback

The API image and the worker image can ship separately. A worker rollout drains the lease, stops pulling, and lets the current job finish or be killed and retried. Model name, device, and compute type are config, loaded once at worker start, which matches the prototype. Changing `WHISPER_MODEL` is a worker restart, not a request flag. A bad model build is rolled back by returning the previous worker image to the pool. In-flight jobs retry on the healthy image.

### Multi-instance operation

Run more than one API instance and more than one worker. The database and the queue are the shared state. Prefer a queue and object store that survive an availability-zone loss. Workers are replaceable. A lost worker costs one attempt, not the audio.

### Security review items before public exposure

- Authentication and authorization on submit and on job read. A caller can read only their jobs.
- Rate limits and the `429` backpressure above.
- A maximum request body on the proxy and in the API.
- No fetch of caller-supplied URLs.
- Secrets in a secret store, not in the image and not in logs.
- Retention and deletion.
- Dependency and image scanning on the container that already installs FFmpeg and Python.

## What stays out even in this design

The enterprise system still does not invent timestamps, still does not return a partial transcript as success, and still does not treat an in-process background task as durability. Speaker diarization, word-level timing, and translation remain separate features with their own models and their own failure codes.

## Suggested build order

1. Object storage and the job row, with `202` and `GET` job status. Workers can still be a single process.
2. A durable queue, leases, and the retry table above.
3. Horizontal workers, queue-depth backpressure, and metrics.
4. Auth, quotas, encryption, and retention before any public hostname.

Each step is deployable. Step 1 is already enough to stop holding the HTTP connection open. The prototype in this repository is the transcription core those workers call.
