# Installation

Setup for the audio transcription prototype. Behavior is defined in [TECHNICAL_PLAN.md](TECHNICAL_PLAN.md). Request examples are in [USAGE.md](USAGE.md).

## 1. What you need

| Tool | Version | Used for |
| --- | --- | --- |
| Python | 3.11.x preferred; 3.12 also runs the app | API and tests. |
| FFmpeg | a build that provides `ffmpeg` and `ffprobe` | Metadata probe, normalization to 16 kHz mono WAV, and chunk extraction. |
| pip | matching that Python | Packages in `requirements.txt`. |
| Docker | optional | Container image. The image installs FFmpeg itself. |

The default `base` model is about 150 MB on first download and roughly 1 GB of RAM on CPU with `int8`. Larger names (`small`, `medium`) need more RAM and a longer first download. The default device is CPU. No speech API key is required.

## 2. Install FFmpeg

Both binaries must be on `PATH`:

```powershell
ffprobe -version
ffmpeg -version
```

bash:

```bash
ffprobe -version
ffmpeg -version
```

Windows: install from https://www.gyan.dev/ffmpeg/builds/ (release essentials), or:

```powershell
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```

Open a new terminal afterward and confirm `ffprobe -version`. A server that is already running keeps the `PATH` from when it started, so start `uvicorn` again after FFmpeg is installed.

On the machine used to transcribe `tests\section1.mp3`, that `winget` command installed Gyan FFmpeg 9.0.2. Before it, the server log was `ffprobe executable not found` and `POST /v1/transcriptions` returned 422 `UNDECODABLE_AUDIO` for that valid MP3.

macOS: `brew install ffmpeg`

Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y ffmpeg`

ffprobe reads container metadata. It does not prove that every frame decodes. The service still runs FFmpeg normalization before transcription, and that step is what maps corrupt media to `UNDECODABLE_AUDIO`.

## 3. Local Python setup

From the repository root (`c:\Work\voice_agent`).

PowerShell, when the 3.11 launcher is installed:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

If `py -3.11` is not available, use the `python` on `PATH`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

bash:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

On the machine used to implement this service, `py -3.11` was not installed, so the virtualenv was created with Python 3.12.10. The Dockerfile still uses `python:3.11-slim`.

`requirements.txt` pins `av>=11,<19` beside `faster-whisper>=1.1.1`. faster-whisper 1.2.1 decodes audio with `av.open(..., metadata_errors="ignore")`. PyAV 19 removed that argument. An unconstrained install pulled `av` 19.0.1, and the first real transcription then returned 500 `TRANSCRIPTION_FAILED`. The server log was:

```text
TypeError: open() got an unexpected keyword argument 'metadata_errors'
```

`pip install -r requirements.txt` installs PyAV 18.1.0, which still accepts `metadata_errors`. If the virtualenv already has PyAV 19, run that install again, then start `uvicorn` again. The process loads the native `av` module at startup, so a package change does not apply to the process that is already running.

After FFmpeg 9.0.2 and PyAV 18.1.0 were in place, `tests\section1.mp3` (MP3, 496 seconds) returned HTTP 200, language `en`, and 101 segments.

Edit `.env` only to change a limit or model. Every variable is listed in `.env.example` and in [TECHNICAL_PLAN.md](TECHNICAL_PLAN.md). There is no `TRANSCRIBE_TIMEOUT_SECONDS`. The process reads the environment. pydantic-settings also reads a local `.env` when that file exists.

Do not commit `.env`. Commit `.env.example` only.

## 4. Model download

faster-whisper downloads `WHISPER_MODEL` (default `base`) on first process start into the Hugging Face cache (`%USERPROFILE%\.cache\huggingface` on Windows, `~/.cache/huggingface` elsewhere).

The first `uvicorn` start waits on that download. Later starts reuse the cache.

If the download or load fails, the process still boots:

- `GET /health` returns 200
- `GET /ready` returns 503 `ENGINE_UNAVAILABLE`
- `POST /v1/transcriptions` returns 503 until a later process loads the model

The service does not substitute a fake transcript when load fails.

Unit tests do not download a model. They override the transcriber dependency with an in-memory fake. Nothing in the environment selects that fake.

Optional prefetch, on a machine with network access:

```powershell
python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"
```

Keep `WHISPER_DEVICE=cpu` and `WHISPER_COMPUTE_TYPE=int8` unless CUDA and a matching CTranslate2 build are installed. `cuda` with `float16` is optional and is not the default.

## 5. Run the API

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Interactive docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health
- Readiness: http://127.0.0.1:8000/ready

Reload during development:

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`--reload` starts a new process and loads the model again.

## 6. Run tests

Default suite, from the repository root. This does not download a model:

```powershell
pytest
```

bash:

```bash
pytest
```

Tests that call the real ffprobe/ffmpeg binaries skip, with a reason, when those tools are missing. Schema, upload, chunk offset, and fake-engine API tests still run.

Optional real-model smoke test, after FFmpeg is installed and you accept a model download:

```powershell
$env:RUN_INTEGRATION = "1"
pytest -m integration
```

bash:

```bash
RUN_INTEGRATION=1 pytest -m integration
```

Unset `RUN_INTEGRATION` for normal runs. The integration test is skipped unless that variable is exactly `1`. It was not executed as part of the default suite, and the `base` model was not downloaded for the default tests.

## 7. Docker

Build and run from the repository root once the Docker daemon is running:

```powershell
docker build -t transcription-service .
docker run --rm -p 8000:8000 `
  -e WHISPER_MODEL=base `
  -e WHISPER_DEVICE=cpu `
  -e WHISPER_COMPUTE_TYPE=int8 `
  -v transcription-hf-cache:/root/.cache/huggingface `
  transcription-service
```

bash:

```bash
docker build -t transcription-service .
docker run --rm -p 8000:8000 \
  -e WHISPER_MODEL=base \
  -e WHISPER_DEVICE=cpu \
  -e WHISPER_COMPUTE_TYPE=int8 \
  -v transcription-hf-cache:/root/.cache/huggingface \
  transcription-service
```

The named volume keeps the model between container runs. The first start still needs network access to fill that volume. The cache path inside the image is `/root/.cache/huggingface`.

Override limits the same way, for example `-e MAX_UPLOAD_BYTES=10485760 -e MAX_DURATION_SECONDS=120 -e CHUNK_SECONDS=30 -e CHUNK_OVERLAP_SECONDS=2`.

The image listens on `0.0.0.0:8000`. Temporary files live inside the container and are removed per request.

`docker build -t transcription-service .` was run from this repository and failed because the Docker daemon is not running:

```text
ERROR: error during connect: Head "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/_ping": open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

## 8. Troubleshooting

| Symptom | What to check |
| --- | --- |
| `ffprobe` not recognized, or the log says `ffprobe executable not found` | FFmpeg is not on the `PATH` of this shell, or not on the `PATH` of the already running server. Install FFmpeg, open a new shell, and start `uvicorn` again. A valid MP3 still returns 422 `UNDECODABLE_AUDIO` when the binary is missing. |
| `/ready` stays 503 | Model download failed, or `WHISPER_DEVICE=cuda` without a working CUDA stack. Read the server log for the load exception. The process should still serve `/health`. |
| Upload returns 413 immediately | `Content-Length` or the streamed file is over `MAX_UPLOAD_BYTES` (default 25 MiB). Multipart framing counts toward `Content-Length`. |
| Upload returns 422 `DURATION_EXCEEDED` | Audio is longer than `MAX_DURATION_SECONDS` (default 600). Chunking does not extend past that cap. |
| Upload returns 422 `UNDECODABLE_AUDIO` | ffprobe, FFmpeg normalization, or chunk extraction failed, timed out, or saw a non-positive duration. |
| Upload returns 415 | No audio stream, or the container is not WAV or MP3. |
| `python-multipart` import error | Reinstall `requirements.txt`. File uploads need that package. |
| Upload returns 500 `TRANSCRIPTION_FAILED` and the log mentions `metadata_errors` | PyAV 19 is installed. `requirements.txt` requires `av>=11,<19`. Run `pip install -r requirements.txt` again and restart the server. PyAV 18.1.0 is the version verified with `tests\section1.mp3`. |
| First request is very slow | Model load or first download. Check `/ready` before sending audio. |

## 9. What this setup does not include

Authentication, rate limits, a database, a queue, and GPU drivers inside the Docker image. The local process is one worker with one inference at a time. The production queue design is described in the README and is not installed here.
