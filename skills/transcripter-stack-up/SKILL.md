---
name: transcripter-stack-up
description: Bring the transcripter dev stack UP from server/ in any deployment mode — base (no ML containers), bundled diarization/Speaches via compose profiles, or routed at an EXTERNAL voice stack via config.yaml/env. Use when asked to start, restart, or reconfigure the dev stack, switch transcription/diarization backends, or connect a separate voice stack.
metadata:
  version: "2.0.0"
---

# Transcripter dev stack — bring-up & backend routing

All commands run from `server/`. Compose project: `transcripter`. Paths below
are relative to the repo root.

## Core services (always on)

`api` (:8090), `worker`, `postgres`, `temporal`, `temporal-ui` (:8082).
ML containers are **profile-gated** and never required for the base stack.

## Deployment modes

| Mode | Transcribe | Diarization | Bring-up |
|---|---|---|---|
| A. Base + bundled LinTO (default dev) | faster-whisper in worker | bundled, profile | `docker compose up -d --profile diarization` |
| B. No ML containers | faster-whisper in worker | disabled → `skipped` | set `diarization.enabled: false` in config.yaml, then `docker compose up -d` |
| C. Bundled voice (Speaches STT) | Speaches, profile `stt` | bundled, profile | `docker compose --profile stt --profile diarization up -d` + config below |
| D. External voice stack | any OpenAI-compatible STT | any LinTO endpoint | base `up -d` + routing options below |

## Mode C config (bundled Speaches)

`server/config.yaml`:

```yaml
transcribe:
  backend: api
  model: Systran/faster-whisper-small   # full HF id — plain "small" 404s
  base_url: http://speaches:8000/v1     # /v1 suffix is REQUIRED
  api_key_env: ""                       # bundled speaches is keyless
```

Speaches preloads the model at container start; first start downloads weights
from huggingface.co into the `speaches-hf-cache` volume (minutes; later starts
work offline). Its healthcheck `start_period` is 5m to cover this.

## Mode D routing options (external voice stack)

Two independent knobs; set only what is external:

| What | Where | Notes |
|---|---|---|
| Transcription endpoint | `config.yaml`: `transcribe.backend: api` + `base_url` (incl. `/v1`) + `model` (HF id) | no env override exists; `api_key_env` names an ENV VAR holding the key for keyed endpoints |
| Diarization endpoint | `config.yaml`: `diarization.endpoint` OR `.env`: `DIARIZATION_ENDPOINT=http://host:8070` | env beats yaml; the compose default interpolates to EMPTY so yaml stays effective |
| Same-host cross-stack DNS | uncomment top-level `networks: voice` block in docker-compose.yml (create first: `docker network create voice`), add `networks: [default, voice]` to worker + ML services | lets worker reach a separate voice stack by service name |
| Multi-host | publish speaches port (uncomment `# - "8000:8000"`) and point `base_url` at `http://<host>:8000/v1` | LAN IP |

## The config-change ritual (ALWAYS)

`config.yaml` is read once per process start:

```bash
docker compose restart worker   # applies transcribe/diarization changes
docker compose restart api      # only needed for the /settings view to match
```

Fail-fast: worker refuses to start with `backend: api` and an empty `base_url`.
Startup probe: with `diarization.enabled: true` the worker pings
`{endpoint}/healthcheck` once; a failure logs a warning that names the
remediation (profile, env override, or `enabled: false`). During LinTO's ~2min
cold start this warning is expected — Temporal's diarize retries (4×30s)
absorb it.

## Readiness checks

```bash
docker compose ps                                   # api/postgres healthy
curl -s localhost:8090/health                       # {"status":"ok"}
docker compose logs worker | grep 'worker started'  # THE readiness line (no healthcheck)
curl -s -H "authorization: Bearer $TRANSCRIPTER_TOKEN" localhost:8090/settings \
  | jq .transcribe.backend, .diarization           # verify effective config
```

Speaches (mode C): wait for `docker compose ps speaches` → healthy, or poll
`docker compose exec speaches python -c "import urllib.request as u; u.urlopen('http://localhost:8000/v1/models')"`.

Prove a mode end-to-end: `STT=speaches bash scripts/e2e_smoke.sh` (asserts
non-empty word timestamps) or plain `bash scripts/e2e_smoke.sh` for A/B.

## Gotchas

- `base_url` without `/v1` → 404 on every transcription (valid URL, wrong path).
- Speaches resolves models strictly: uncached model → 404. The compose service
  sets `PRELOAD_MODELS=["Systran/faster-whisper-small"]`; a different model
  needs that env changed AND the config `model` to match.
- Speaches runs Silero VAD on every request — sine-tone test audio yields zero
  words; the committed `scripts/fixtures/speech-2voices.flac` is the VAD-safe
  fixture.
- Empty `api_key_env` is correct for keyless local endpoints (the worker omits
  the Authorization header rather than sending an illegal empty `Bearer `).
- Image tags are pinned; updates go through `SECURITY.md` (pre-update checklist).
- `docker compose down` keeps volumes; `down -v` wipes postgres AND model caches.
