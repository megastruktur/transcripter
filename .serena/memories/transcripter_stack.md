# transcripter_stack

Docker dev stack for the transcripter project (repo `server/` dir). Compose project name: `transcripter`.

## Core services (always on)

`api` (FastAPI, :8090), `worker` (Temporal worker), `postgres` (:16-alpine), `temporal` (auto-setup 1.28.2), `temporal-ui` (:8082). Worker readiness = log line `worker started on queue transcripter-pipeline` (it has NO healthcheck).

## ML services are compose profiles (since 2026-08-23)

- `diarization` (lintoai/linto-diarization-pyannote:2.3.0, host :8070) — profile `diarization`. Worker no longer `depends_on` it; its cold start (~2 min weight load) is bridged by the diarize activity's Temporal retry policy (4 attempts × 30 s initial backoff) plus a one-shot startup probe in `worker/main.py` that warns with remediation if `{endpoint}/healthcheck` fails.
- `speaches` (ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cpu) — profile `stt`, OpenAI-compatible STT on :8000 (NOT published to host by default). `PRELOAD_MODELS=["Systran/faster-whisper-small"]` is mandatory: Speaches 404s on uncached models. HF cache volume `speaches-hf-cache`; first start downloads weights (healthcheck start_period 5m).

## Backend routing (config.yaml, read once per worker start → `docker compose restart worker` after edits)

- `transcribe.backend: local|api` — api = any OpenAI-compatible `/v1/audio/transcriptions`. `base_url` MUST include `/v1`. `model` must be the full HF id for Speaches. `api_key_env` names an env var; empty key = Authorization header omitted (httpx rejects empty `Bearer `).
- `diarization.enabled: true|false` — false → diarize/merge stages honestly `skipped`, no container/contact needed. `diarization.endpoint` or env `DIARIZATION_ENDPOINT` (env wins; compose interpolates it to EMPTY by default so yaml stays effective).
- Fail-fast at worker start: `backend: api` + empty `base_url` → ValueError.
- ApiTranscriber sends `timestamp_granularities[]=[word,segment]` and parses words from both top-level `words` (OpenAI/Speaches) and `segments[].words` (Groq) — word timestamps are the diarization-merge input.
- Same-host external voice stack: shared docker network `voice` (top-level commented block in docker-compose.yml; `docker network create voice`). Multi-host: publish speaches port, point base_url at `http://<host>:8000/v1`.

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
