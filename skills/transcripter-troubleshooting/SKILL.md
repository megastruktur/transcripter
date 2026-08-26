---
name: transcripter-troubleshooting
description: Diagnose and fix known failure modes of the transcripter stack (Docker server: api/worker/postgres/temporal/temporal-ui/diarization, Tauri client on ports 8090/8082/8070/5173). Use when the webview/client says "Failed to fetch" or CORS errors, the api container exits about a missing config or directory, compose refuses to start citing TRANSCRIPTER_TOKEN, stages stay pending, diarization healthcheck stalls on Apple Silicon, shell scripts die on BSD coreutils (stat -c%s / sha256sum), a stage is failed, or a port is already in use.
metadata:
  version: "1.0"
---

# Transcripter Troubleshooting

Symptom → cause → confirm → fix, for failures actually hit in this repo. All commands run from the repo root unless noted; the stack is started with `transcripter-stack-up` and exercised with `transcripter-e2e-smoke`.

## First five commands (from `server/`)

```bash
cd server
docker compose ps                                   # which service is down/unhealthy
curl -s http://localhost:8090/health                # expect {"status":"ok"}
curl -s -H "authorization: Bearer dev-local-token" http://localhost:8090/recordings   # expect {"items":[],"total":0,"limit":50,"offset":0}
docker compose logs --tail=50 worker                # model download? queue connected?
docker compose logs --tail=50 api                   # startup exit? auth errors?
```

If a command needs the real token, it is in `server/.env` (`TRANSCRIPTER_TOKEN=...`).

## Pipeline quick reference

Stages run in order `transcribe` → `diarize` → `merge_speakers` → `summarize` (Temporal workflow `ProcessRecording`, workflow ids prefixed `process-recording-`); statuses are `pending`/`running`/`done`/`failed`/`skipped`, recording states `uploading`/`processing`/`done`/`failed`. Finalize always runs, so a recording always reaches a terminal state even when a stage failed.

- Artifacts live under `server/storage/recordings/<id>/meta/`: `transcript.md` + `segments.json` (transcribe), `diarization.json` (diarize), `diarized-transcript.md` (merge_speakers), `summary.md` (summarize). A stage marked `done` with a missing file means the container lost the `storage` mount — re-check `docker compose ps`.
- `diarize` is best-effort: on short/quiet/single-speaker audio it can `failed` while the recording still finishes `done` (degrades to transcript-only). That `failed` diarize row is expected degradation, not a broken pipeline — inspect it via `last_error` in row 7 before acting.
- Temporal UI at http://localhost:8082 shows the workflow for a stuck recording: if the workflow itself is `Completed` but stages look wrong, the bug is in the DB rows, not the pipeline; if the workflow is `Running`/`ContinuedAsNew`-less and idle, the worker isn't consuming the queue (row 4).

## Diagnostic table

| # | Observable symptom | Root cause | Confirm | Fix |
|---|---|---|---|---|
| 1 | Browser/webview client fails with `TypeError: Failed to fetch` while `curl` to the same URL works | CORS preflight was rejected: `FastAPI.add_middleware` PREPENDS, so the `bearer_auth` http middleware in `server/api/app/main.py` wrapped `CORSMiddleware` and 401'd the credential-less `OPTIONS` preflight before CORS could answer | `curl -si -X OPTIONS http://localhost:8090/recordings -H 'Origin: http://localhost:5173' -H 'Access-Control-Request-Method: GET' -H 'Access-Control-Request-Headers: authorization'` → expect `200` with an `access-control-allow-origin` header; `GET /recordings` with no/bad token must still be `401` | **Already fixed** — `bearer_auth` in `main.py` skips the token check when `request.method == "OPTIONS"`, locked by `test_cors_preflight_bypasses_auth` in `server/api/tests/test_auth.py`. Do not re-apply; if it regresses, the preflight curl returns `401` — restore the `OPTIONS` exemption and run `cd server/api && uv run pytest` |
| 2 | `api` container exits at startup: `config path /etc/transcripter/config.yaml is a directory` (or `not found`) | `server/config.yaml` was never created, so the compose bind-mount `./config.yaml:/etc/transcripter/config.yaml:ro` materialized a directory on the host; `_check_startup()` in `server/api/app/main.py` exits with that explicit message | `ls -la server/config.yaml` (a directory means the bad mount); `docker compose logs api` | `cp -n config.example.yaml config.yaml` (from `server/`), then `docker compose up -d api` |
| 3 | `docker compose` refuses to start, citing `TRANSCRIPTER_TOKEN` (`set TRANSCRIPTER_TOKEN in .env`) | `server/.env` is missing; compose interpolates `${TRANSCRIPTER_TOKEN:?set TRANSCRIPTER_TOKEN in .env}` in the api service | `ls server/.env` | `printf 'TRANSCRIPTER_TOKEN=dev-local-token\n' > .env` (from `server/`), then `docker compose up -d` |
| 4 | E2E smoke or client shows stages stuck `pending` | Worker still downloading the faster-whisper `small` model on first start, or not connected to Temporal yet — it is NOT ready until it logs `INFO:transcripter.worker:worker started on queue transcripter-pipeline` | `docker compose logs -f worker` (wait for the queue line); inspect workflow state in Temporal UI http://localhost:8082 | Wait for the queue line (model lands in the `models` volume, so it is one-time); if the worker container is `restarting`, check its logs for the Temporal address (`TEMPORAL_ADDRESS=temporal:7233`) |
| 5 | Diarization is slow/flaky, or its healthcheck keeps failing on Apple Silicon | `lintoai/linto-diarization-pyannote:2.3.0` is published `linux/amd64`-only; on ARM it runs emulated with a `start_period: 120s` healthcheck — it works, just slow | `docker compose ps` (diarization `health: starting` for up to 2 min) and the line `The requested image's platform (linux/amd64) does not match the detected host platform (linux/arm64/v8)` in compose output | Nothing to fix — wait out the 120s start period. `worker` depends on diarization being healthy before starting, so #4 and #5 often appear together |
| 6 | Shell script dies on macOS with `stat: illegal option -- c` / `sha256sum: command not found` | Script assumed GNU coreutils, which macOS/BSD lack (`stat -c%s`, `sha256sum`) | `echo 'x' > /tmp/t && stat -c%s /tmp/t` fails on the host | Use `wc -c < file` for size and `shasum -a 256` (or `sha256sum` if present) for digests. `server/scripts/e2e_smoke.sh` was already made portable this session (`fsize()`/`sha256()` helpers, host `ffmpeg` with container fallback) — copy that pattern into new scripts, don't reintroduce GNU-only calls |
| 7 | A stage is `failed` | Stage activity raised; the error is recorded per-stage | `curl -s -H "authorization: Bearer dev-local-token" http://localhost:8090/recordings/<id> \| jq '.stages[]'` — the `last_error` field of the failed stage, plus `docker compose logs worker` | Fix the cause, then re-run the stage: `curl -s -X POST -H "authorization: Bearer dev-local-token" -H 'content-type: application/json' -d '{"stage":"diarize"}' http://localhost:8090/recordings/<id>/regenerate` (stage must be one of `transcribe`/`diarize`/`merge_speakers`/`summarize`; 409 if recording still uploading/processing) |
| 8 | Transcript is empty or garbage on the synthetic smoke audio | Expected: the e2e smoke generates sine tones, not speech — the pipeline is proven, ML quality is not (`**None [00:00:02 - 00:00:15]:** ...`) | `docker compose logs worker` shows transcribe `done` with no error; artifacts exist under `server/storage/recordings/<id>/meta/` | Nothing — judge transcription quality only with a real speech recording |
| 9 | `port is already allocated` / `bind: address already in use` on 8090/8082/8070/5173 | A previous stack or dev server is still holding the port (8090 api, 8082 temporal-ui, 8070 diarization, 5173 vite dev via `pnpm tauri dev`) | `lsof -nP -iTCP:8090 -sTCP:LISTEN` (repeat for the other ports) | `docker compose down` (from `server/`) for 8090/8082/8070; quit the `pnpm tauri dev` process for 5173. Do NOT start a second stack on the same ports |

## Gotchas

- **`skipped` is success, not failure.** Terminal stages end `done,done,done,skipped`: `summarize` skips until a model is configured in `server/config.yaml` (`enabled` + `model`), and `merge_speakers` self-skips when there is no usable diarization (short/quiet audio) so the stage never sits pending forever.
- **401 is the expected auth behavior.** Everything except `PUBLIC_PATHS` (`/health`, `/docs`, `/openapi.json`) requires `authorization: Bearer <token>`. A 401 from the client means wrong/missing token in `server/.env` vs the client's stored settings (localStorage key `transcripter.apiConfig`), not a server bug.
- **The preflight exemption is intentional and locked.** `OPTIONS` bypassing `bearer_auth` is the fix in row 1 — never "harden" the middleware by removing it; the real request is still authed. `test_cors_preflight_bypasses_auth` fails loudly if it regresses.
- **Containers cannot see host `/tmp`.** E2E work goes under `server/.e2e-work` deliberately; any host file a container must read must live under the project path.
- **Client upload is http-only.** The uploader rejects `https` (LAN MVP); `https://` in the client's API base URL fails at the client, not the server.
- **First-run timing is not a hang.** `docker compose up -d` gates on diarization's 120s healthcheck `start_period` (~144s first run), and the worker preloads the faster-whisper `small` model before logging `worker started on queue transcripter-pipeline`. Stages that look stuck pending are usually just row 4/5 in the table.
- **`config.yaml` missing vs directory are the same mistake.** The bind-mount creates a directory when the host file is absent, so the api exit message reads `is a directory` even when you simply never ran `cp -n config.example.yaml config.yaml`.
- **Settings → "Test connection" splits the failure for you.** It hits `/health` (public), then authed `/recordings`: `health 4xx/5xx` means the server or base URL is wrong, `unauthorized: wrong token` means the stored token doesn't match `server/.env`, `recordings <status>` means the authed endpoint itself failed. It is the fastest way to localize row 1 vs a plain auth misconfiguration.
- **The e2e smoke uses its own token fallback.** `scripts/e2e_smoke.sh` line 9: `TOKEN="${TRANSCRIPTER_TOKEN:-test-token-e2e}"` — run it without exporting `TRANSCRIPTER_TOKEN` and health (step 1) passes while every authed step 401s. Run it as `TRANSCRIPTER_TOKEN=<value-from-server/.env> bash scripts/e2e_smoke.sh`. Its EXIT trap `rm -rf`s `server/.e2e-work` on any exit, so intermediate files don't survive a failure — the server-side artifacts under `server/storage/recordings/<id>/` do.
- **The worker has no compose healthcheck.** `docker compose ps` never shows the worker "healthy" — `running` is the best status it has. Readiness is the `worker started on queue transcripter-pipeline` log line (row 4), not ps status; `restarting` means it is crash-looping — read `docker compose logs worker`.
- **The `temporal` service exposes no host port.** Only the UI (8082) is reachable from the host; there is no gRPC port mapping. To inspect workflows beyond the UI, use `docker compose exec` against the `temporal` service, not a host-side Temporal CLI pointed at `localhost`.
- **Tauri `invoke` commands don't work in a plain browser.** The dev UI at http://localhost:5173 serves API-only pages; recording/upload (via `client/src/lib/tauri.ts`) only work inside the app window — a `Failed to fetch`-ish failure there is a client-side wiring issue, not row 1.
