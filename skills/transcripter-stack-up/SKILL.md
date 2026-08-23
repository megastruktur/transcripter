---
name: transcripter-stack-up
description: Bring the transcripter Docker server stack (api, worker, postgres, temporal, temporal-ui, diarization) UP and DOWN from server/. Use when asked to start, stop, restart, rebuild, or reset the server, when a service is unhealthy or crash-looping, or when preparing the stack for the e2e smoke test. Covers the 4-step bring-up, per-service readiness checks (the worker has no healthcheck), rebuild-after-edit, and volume teardown.
metadata:
  version: "1.0"
---

# Transcripter server stack up/down

All commands run from `server/`. Compose project name: `transcripter`.

Six services:

| service | image/build | host port | healthcheck |
|---|---|---|---|
| `api` | `Dockerfile.api` (FastAPI) | 8090 → 8080 | yes (`/health`) |
| `worker` | `Dockerfile.worker` (Temporal worker) | — | **none** — ready only when its log line appears (see below) |
| `postgres` | `postgres:16-alpine` | — | yes (`pg_isready`) |
| `temporal` | `temporalio/auto-setup:1.28.2` | — | none |
| `temporal-ui` | `temporalio/ui:2.35.0` | 8082 → 8080 | none |
| `diarization` | `lintoai/linto-diarization-pyannote:2.3.0` | 8070 → 80 | yes, **120s start_period** |

Named volumes: `pgdata` (postgres data) and `models` (faster-whisper cache, `download_root=/models` in the worker). Bind mounts: `./storage` → `/storage` (api + worker) and `./config.yaml` → `/etc/transcripter/config.yaml` (ro). Recordings land on the host at `server/storage/recordings/<uuid>/`.

## Bring up (4 steps, from `server/`)

```bash
cd server
cp -n config.example.yaml config.yaml          # -n: never overwrite an existing config.yaml
printf 'TRANSCRIPTER_TOKEN=dev-local-token\n' > .env
docker compose build                            # ~37s
docker compose up -d                            # ~144s on first run
```

## Readiness checks

```bash
docker compose ps
# api, postgres, diarization: (healthy); worker, temporal, temporal-ui: Up

curl -s http://localhost:8090/health
# -> {"status":"ok"}   (/health, /docs, /openapi.json are public; everything else needs the token)

curl -s -H "authorization: Bearer dev-local-token" http://localhost:8090/recordings
# -> []
```

Temporal UI: open http://localhost:8082 (namespace `default`, worker visible once the worker line appears).

**The worker has no healthcheck — `docker compose ps` showing it `Up` is NOT enough.** It is ready only when this line appears in its log:

```bash
docker compose logs -f worker
# wait for: worker started on queue transcripter-pipeline
```

On first start the worker downloads faster-whisper `small` from HuggingFace into the `models` volume before that line appears — the download dominates first-start time.

## Lifecycle

- **Edited a service's code?** Rebuild and restart just that service: `docker compose up -d --build api` (swap `api` for `worker`).
- **Stop everything:** `docker compose down` — removes containers, keeps the `pgdata` and `models` named volumes.
- **`docker compose down -v`** — also destroys the named volumes: wipes the database AND the model cache. The next bring-up re-downloads the whisper model and starts an empty DB. Do not run it casually.

Config keys in `config.yaml` (from `config.example.yaml`): `transcribe.backend`/`transcribe.model` (defaults `local`/`small`), `summarize.enabled` (**`false` by default** — the summarize stage reports `skipped`, which is success, not failure), `diarization.endpoint` (`http://diarization:8080`, the compose service name), `database.url`. Auth token comes from the `TRANSCRIPTER_TOKEN` env var only, never from `config.yaml`.

To prove the whole pipeline works end to end, see `transcripter-e2e-smoke`. For known failure modes and diagnostics, see `transcripter-troubleshooting`.

## Gotchas

- **Missing `.env` → compose fails fast.** `docker compose up` aborts before creating anything because the compose file interpolates `${TRANSCRIPTER_TOKEN:?set TRANSCRIPTER_TOKEN in .env}`. The error names the variable; fix is writing `.env` (step 2), not editing the compose file.
- **Skipping the `config.yaml` copy → api crash-loops with an explicit exit.** The ro bind-mount `./config.yaml:/etc/transcripter/config.yaml` creates a **directory** at the target when the host file is absent; `_check_startup()` in `server/api/app/main.py` detects `os.path.isdir(config_path)` and exits: `config path ... is a directory — this usually means the compose bind-mount found no config.yaml on the host. Copy server/config.example.yaml to server/config.yaml first.` Fix: `docker compose down` (removes the stray dir), run step 1, `docker compose up -d`.
- **Diarization is linux/amd64-only.** On Apple Silicon compose logs `The requested image's platform (linux/amd64) does not match the detected host platform (linux/arm64/v8)`; the service runs emulated — it works, but slow — which is why its healthcheck has a 120s `start_period` and why the first `docker compose up -d` takes ~2.5 min (worker waits on `diarization: service_healthy`). Expect the warning; it is not an error.
- **Host ports 8090 / 8082 / 8070** were deliberately picked to dodge host conflicts (see SPECS.md). If `up` reports a port in use, something else owns that port — free it or change the compose mapping; don't assume the transcripter stack is stale.
