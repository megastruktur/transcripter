# transcripter_stack

> **STATUS 2026-09-04:** api/worker deployed as v0.23.1 images =
> **Python 3.14.7** (first 3.14 release; prior v0.23.0/v0.22.0 were 3.12).
> Deployed via Komodo DeployStack after the v0.23.1 release; pre-deploy gate
> (no Running ProcessRecording) respected. Auto-deploy route
> `github-release-transcripter` (devops gateway) was DOWN at deploy time —
> pin commit `0fdd388` + DeployStack done manually.

Docker dev stack for the transcripter project (repo `server/` dir). Compose project name: `transcripter`.

## Core services (always on)

`api` (FastAPI, :8090), `worker` (Temporal worker), `postgres` (:16-alpine), `temporal` (auto-setup 1.28.2), `temporal-ui` (:8082). Worker readiness = log line `worker started on queue transcripter-pipeline` (it has NO healthcheck).

## Pipeline stages (since 2026-08-25: FIVE)

`chunk → transcribe → diarize → merge_speakers → summarize` (+ always `finalize_recording`, best-effort `export_transcript`). `chunk` is a Postgres enum value of `stage_kind` — the API startup runs idempotent `ALTER TYPE ... ADD VALUE IF NOT EXISTS` (`_migrate_stage_kind_enum` in `api/app/main.py`; create_all never alters existing enums). Regenerate backfills missing stage rows for pre-chunk recordings (`api/app/routes/regenerate.py`).

## Server-side chunking (`chunk.*` config, OFF by default; ON in dev config.yaml)

Why: whisper repetition loop lives in one request's decoder context — slicing resets it, so a poisoned chunk costs ~10 min instead of hours, independent of Speaches version (0.8.3 has no `condition_on_previous_text` knob).

- `worker/chunk.py`: `plan_chunks` (even ~10-min chunks, 2 s overlap, short tail), ffmpeg `-ss/-t` input options + `-c:a flac` re-encode (`-c copy` can't cut FLAC exactly), per-chunk subprocess with kill-group+abandon on timeout (same pattern as export_transcript). Manifest `meta/chunks/chunks.json` = per-chunk start/end + transcribe/diarize status + suspect flag → resume boundary.
- transcribe/diarize loop chunks SEQUENTIALLY (never parallel — one CPU voice stack; two contending large-v3 jobs run at ~half speed). Per-chunk HTTP budget = 300 s + 40 s/min OF THE CHUNK − 30 s; Temporal budget = Σ per-chunk + 300 s slack (`_ml_budget` in workflows.py).
- Retry: ×2 inside the transcribe activity (backoff 5 s); stage fails with `chunk N of M` in last_error; regenerate re-POSTs only non-done chunks. Diarize resumes via persisted per-chunk status across its Temporal ×4 retry.
- Seams: overlap split at midpoint (`keep_window`/`shift_into`) — no duplicated/lost speech.
- Suspect: >50% identical normalized segment texts (≥4 segments) → manifest `transcribe_suspect`; regenerate re-runs suspect chunks with `prompt=""` + `condition_on_previous_text=false` form fields (0.8.3 ignores unknown fields; hook for newer Speaches).
- Retention `until_merged`: chunk FLACs deleted by merge_speakers (done OR skipped); manifest + per-chunk JSONs stay. Re-running transcribe/diarize after cleanup errors with "regenerate from stage 'chunk'".
- Diarize speaker labels stay per-chunk (accepted — merge attributes words by time overlap).

## ML services are compose profiles (since 2026-08-23)

- `diarization` (lintoai/linto-diarization-pyannote:2.3.0, host :8070) — profile `diarization`. Worker no longer `depends_on` it; its cold start (~2 min weight load) is bridged by the diarize activity's Temporal retry policy (4 attempts × 30 s initial backoff) plus a one-shot startup probe in `worker/main.py` that warns with remediation if `{endpoint}/healthcheck` fails.
- `speaches` (ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cpu) — profile `stt`, OpenAI-compatible STT on :8000 (NOT published to host by default). `PRELOAD_MODELS=["Systran/faster-whisper-small"]` is mandatory: Speaches 404s on uncached models. HF cache volume `speaches-hf-cache`; first start downloads weights (healthcheck start_period 5m).

## Backend routing (config.yaml, read once per worker start → `docker compose restart worker` after edits)

- `transcribe.backend: local|api` — api = any OpenAI-compatible `/v1/audio/transcriptions`. `base_url` MUST include `/v1`. `model` must be the full HF id for Speaches. `api_key_env` names an env var; empty key = Authorization header omitted (httpx rejects empty `Bearer `).
- `diarization.enabled: true|false` — false → diarize/merge stages honestly `skipped`, no container/contact needed. `diarization.endpoint` or env `DIARIZATION_ENDPOINT` (env wins; compose interpolates it to EMPTY by default so yaml stays effective).
- Fail-fast at worker start: `backend: api` + empty `base_url` → ValueError.
- ApiTranscriber sends `timestamp_granularities[]=[word,segment]` and parses words from both top-level `words` (OpenAI/Speaches) and `segments[].words` (Groq) — word timestamps are the diarization-merge input.
- Same-host external voice stack: shared docker network `voice` (top-level commented block in docker-compose.yml; `docker network create voice`). Multi-host: publish speaches port, point base_url at `http://<host>:8000/v1`.
- GOTCHA: single-file bind mount `./config.yaml:...:ro` pins the inode — an editor that replaces the file leaves containers reading the OLD content. After editing config.yaml use `docker compose up -d --force-recreate worker api`, NOT `restart`.

## Summarize stage (enabled 2026-08-26)

`summarize` in config.yaml points at the platform LiteLLM proxy (`http://192.168.3.23:4000/v1`, model `qwen3.8-27b-q4_k_m` on llama-server); key = dedicated LiteLLM virtual key scoped to that model (metadata `transcripter-summarize`), injected as `LITELLM_API_KEY` via .env → worker env. Budgets: Temporal start_to_close 2400s (= LiteLLM deployment timeout), httpx 2370s (30s under, so ReadTimeout → stage `failed` beats Temporal cancellation), `_no_retry()`, heartbeat 60s/timeout 120s, sync httpx wrapped in `_heartbeat_while(asyncio.to_thread(...))`. A full 106KB/83-min transcript summarization took >870s on a contended llama-server (FIFO queue) — hence the 2400s ceiling; an identical immediate retry then returned in ~11s from llama-server's warm slot/KV cache.

## Smoke tests

`bash server/scripts/e2e_smoke.sh` (local modes; tone audio) and
`STT=speaches bash server/scripts/e2e_smoke.sh` (api backend; uses committed
`scripts/fixtures/speech-2voices.flac` — Speaches' Silero VAD rejects tones;
asserts non-empty word timestamps end-to-end).

## SECURITY.md

Repo-root `SECURITY.md` pins every external image (speaches, LinTO, postgres,
temporal, ui) with a pre-update checklist — required reading before any image
bump; Speaches updates specifically re-verify form-field/words/VAD contract
via `STT=speaches` smoke.
