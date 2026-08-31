---
name: transcripter-e2e-smoke
description: Run and interpret the transcripter end-to-end smoke test (server/scripts/e2e_smoke.sh) — synthetic audio upload with simulated drop and overlapping resume, server-side SHA-256 byte-identity, finalize, pipeline stage polling, and artifact assertions. Supports the STT=speaches mode that proves the api-backend/word-timestamps path against the bundled Speaches profile. Use when asked to run the e2e smoke, smoke test the full upload→pipeline→artifacts path, verify a recording-pipeline change end to end, or interpret the smoke script's stage output (done,done,done,skipped).
metadata:
  version: "1.1"
---

# transcripter-e2e-smoke

Full-path smoke test: chunked upload with interruption + resume, then the Temporal pipeline producing all recording artifacts. One script, ~18 s on a warm stack.

## Running

Prerequisite: the docker stack is up (api healthy on 8090) and the worker is past its first-start model download. See `transcripter-stack-up` for bring-up in any mode.

```bash
cd server
TRANSCRIPTER_TOKEN=dev-local-token bash scripts/e2e_smoke.sh   # optional arg: seconds to wait, default 600
STT=speaches TRANSCRIPTER_TOKEN=dev-local-token bash scripts/e2e_smoke.sh   # api-backend mode
```

The token env var is required to match your `server/.env` — the script's built-in default is a different token, so an unset `TRANSCRIPTER_TOKEN` fails with 401 at step 3.
Against the **dev stack** (next to Komodo staging — see `transcripter-stack-up`):

```bash
cd server && set -a && . ./.env && set +a
STT=speaches SPEACHES_PROBE_URL=http://192.168.3.23:8010/v1/models \
  TRANSCRIPTER_API=http://localhost:18090 \
  TRANSCRIPTER_STORAGE=$PWD/storage-dev \
  TRANSCRIPTER_DC="docker compose -p transcripter-dev -f docker-compose.yml -f docker-compose.dev.yml" \
  bash scripts/e2e_smoke.sh
```

Env overrides (defaults preserve staging behavior): `TRANSCRIPTER_API` (api
base URL), `TRANSCRIPTER_STORAGE` (server storage dir for artifact asserts),
`TRANSCRIPTER_TRANSCRIPTS` (default `$TRANSCRIPTER_STORAGE/transcripts`),
`TRANSCRIPTER_DC` (compose command for the speaches docker-exec probe).
Step 3c PATCHes the recording `type: ttrpg` (POST /recordings accepts no
type) so the ttrpg-session-log profile matches — without it the
`session-log.md` assert cannot pass. Fresh transcripts dirs need the
`.transcripter` sentinel file or export refuses to write (and the 9b
`grep -c` guard needs its `|| true` — zero matches exit 1 under pipefail).

`STT=speaches` requires: stack up with `--profile stt` (+ `--profile diarization` for the full path), config.yaml routed to `backend: api` / `base_url: http://speaches:8000/v1` / `model: Systran/faster-whisper-small`, and a worker restart (see `transcripter-stack-up` mode C). It uses the committed speech fixture `server/scripts/fixtures/speech-2voices.flac` instead of tones (Speaches' Silero VAD rejects non-speech), waits for the model preload (first run downloads weights), and asserts non-empty word timestamps in `segments.json` — the end-to-end proof that the granularities/words-parsing path works.

## What the 10 numbered steps do

1. **health** — `GET /health` must return `{"status":"ok"}`.
2. **audio** — local mode: generates a 30 s, 16 kHz mono WAV of alternating sine tones (two "voices" by frequency), encodes to FLAC. Speaches mode: copies the committed speech fixture instead (VAD-compatible). ffmpeg/ffprobe prefer the host binary, falling back to the pinned speaches container.
3. **create recording** — `POST /recordings` with `{"title":"e2e-smoke"}`; prints the recording `id`.
4. **upload first half** — `PUT /recordings/{id}/audio?offset=0` with exactly `SIZE/2` bytes, simulating a connection drop after the first half commits.
5. **resume from overlap** — resumes at `offset = HALF - 1024` (1 KiB *before* the committed end) and uploads the rest, proving the server accepts an overlapping offset and lands on `committed == SIZE`.
6. **verify server-side bytes are bit-identical** — recomputes SHA-256 of `server/storage/recordings/<id>/audio.flac` on disk and asserts it equals the client-side hash.
7. **finalize** — `POST /recordings/{id}/finalize` with the SHA-256 and duration (`30` for tones; the probed fixture duration in speaches mode — activity timeouts scale from it); asserts the recording enters `processing`, which kicks off the Temporal workflow.
8. **wait for pipeline stages** — polls `GET /recordings/{id}` every 15 s, printing the four stage statuses joined as e.g. `stages: running,running,pending,skipped`; exits on `PIPELINE FAILED` (any stage `failed`, with `last_error` printed) or `TIMEOUT waiting` once the wait window is exhausted.
9. **artifacts exist** — in speaches mode first asserts non-empty `words` in `segments.json`; `transcript.md`/`segments.json` always required; `diarization.json` required only if the `diarize` stage is `done`, `diarized-transcript.md` only if `merge_speakers` is `done` (gates match the stage that writes each file — a done diarize with zero found speakers legitimately leaves no markdown).
10. **regenerate diarize** — `POST /recordings/{id}/regenerate` with `{"stage":"diarize"}`; accepts rc `200` (idle, re-runs) or `409` (already-processing guard) — both pass.

Final line on success:

```
E2E SMOKE PASSED (recording <uuid>)
```

## Reading the stage line

Step 8 prints statuses for the six pipeline stages in fixed `kind` order: `chunk,transcribe,diarize,merge_speakers,summarize,enrich`.

- `done,done,done,done,done,done` is SUCCESS on the full config (summarize model set, diarization on, graph profile up). `skipped` in any slot is honest state for a disabled stage (e.g. `summarize` without a model, `enrich` without `graph.uri`), not a failure.
- `done,skipped,skipped,skipped` is SUCCESS when `diarization.enabled: false` — skipped is the honest state for a disabled stage, not a failure.
- Any `failed` element → the script already exited with `PIPELINE FAILED` and the stage's `last_error`; hand off to `transcripter-troubleshooting`.
- Non-terminal statuses (e.g. `running`) persisting past the wait window → `TIMEOUT waiting`; check `docker compose logs -f worker` before re-running.

## Inspecting results afterward

The script cleans up its `server/.e2e-work` scratch dir, but the recording row and files persist:

- **On disk:** `server/storage/recordings/<id>/` holds `audio.flac` plus `meta/` with `transcript.md`, `segments.json`, `diarization.json`, `diarized-transcript.md`.
- **Via API** (all with `-H "authorization: Bearer dev-local-token"`):
  - `GET http://localhost:8090/recordings/{id}` — detail incl. state and per-stage status/`last_error`.
  - `GET http://localhost:8090/recordings/{id}/artifacts/{stage}` — artifact content; `{stage}` is one of `transcribe`, `diarize`, `merge_speakers`, `summarize` (note: the diarized transcript belongs to `merge_speakers`, not `diarize`).

## Gotchas

- `summarize = skipped` is **expected**, not a failure: summarize is disabled until a model is configured in `config.yaml`.
- In local mode the synthetic audio is sine tones, so the transcript is empty placeholder text. The smoke proves the **pipeline** works; it says nothing about ML quality — judge quality only on a real speech recording (or the speaches-mode fixture).
- The script assumes the stack is **already up** and the worker is **past its first-start model download** (readiness = the `worker started on queue transcripter-pipeline` log line). If you run it right after `docker compose up -d`, the step-8 poll loop silently burns the entire 600 s wait window before timing out.
- The work dir is `server/.e2e-work`, deliberately under the project path: containers cannot see host `/tmp`, so scratch must live where the ffmpeg container fallback can bind-mount it. A `trap` removes it on exit.
- The script is portable across macOS/BSD hosts (fsize/sha256 helpers, host-ffmpeg preference). Do not "fix" it back to GNU coreutils.
