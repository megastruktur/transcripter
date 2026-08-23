---
name: transcripter-e2e-smoke
description: Run and interpret the transcripter end-to-end smoke test (server/scripts/e2e_smoke.sh) — synthetic audio upload with simulated drop and overlapping resume, server-side SHA-256 byte-identity, finalize, pipeline stage polling, and artifact assertions. Use when asked to run the e2e smoke, smoke test the full upload→pipeline→artifacts path, verify a recording-pipeline change end to end, or interpret the smoke script's stage output (done,done,done,skipped).
metadata:
  version: "1.0"
---

# transcripter-e2e-smoke

Full-path smoke test: chunked upload with interruption + resume, then the Temporal pipeline producing all recording artifacts. One script, ~18 s on a warm stack.

## Running

Prerequisite: the docker stack is up (api healthy on 8090) and the worker is past its first-start model download. See `transcripter-stack-up` for bring-up.

```bash
cd server
TRANSCRIPTER_TOKEN=dev-local-token bash scripts/e2e_smoke.sh   # optional arg: seconds to wait, default 600
```

The token env var is required to match your `server/.env` — the script's built-in default is a different token, so an unset `TRANSCRIPTER_TOKEN` fails with 401 at step 3.

## What the 10 numbered steps do

1. **health** — `GET /health` must return `{"status":"ok"}`.
2. **generate test audio** — writes a 30 s, 16 kHz mono WAV of sine tones alternating 220/440 Hz every 1 s (two "voices" by frequency, with 0.2 s speech-like gaps), then encodes to FLAC. Prefers host `ffmpeg`, falls back to a throwaway `gcc:14` container. Records the FLAC's SHA-256 and byte size.
3. **create recording** — `POST /recordings` with `{"title":"e2e-smoke"}`; prints the recording `id`.
4. **upload first half** — `PUT /recordings/{id}/audio?offset=0` with exactly `SIZE/2` bytes, simulating a connection drop after the first half commits.
5. **resume from overlap** — resumes at `offset = HALF - 1024` (1 KiB *before* the committed end) and uploads the rest, proving the server accepts an overlapping offset and lands on `committed == SIZE`.
6. **verify server-side bytes are bit-identical** — recomputes SHA-256 of `server/storage/recordings/<id>/audio.flac` on disk and asserts it equals the client-side hash.
7. **finalize** — `POST /recordings/{id}/finalize` with `{"sha256": ..., "duration_sec": 30}`; asserts the recording enters `processing`, which kicks off the Temporal workflow.
8. **wait for pipeline stages** — polls `GET /recordings/{id}` every 15 s, printing the four stage statuses joined as e.g. `stages: running,running,pending,skipped`; exits on `PIPELINE FAILED` (any stage `failed`, with `last_error` printed) or `TIMEOUT waiting` once the wait window is exhausted.
9. **artifacts exist** — asserts `transcript.md`, `segments.json`, `diarization.json`, `diarized-transcript.md` all exist and are non-empty under `server/storage/recordings/<id>/meta/`.
10. **regenerate diarize** — `POST /recordings/{id}/regenerate` with `{"stage":"diarize"}`; accepts rc `200` (idle, re-runs) or `409` (already-processing guard) — both pass.

Final line on success:

```
E2E SMOKE PASSED (recording <uuid>)
```

## Reading the stage line

Step 8 prints statuses for the four pipeline stages in fixed `kind` order: `transcribe,diarize,merge_speakers,summarize`.

- `done,done,done,skipped` is the expected SUCCESS on the default config — `summarize` is disabled until a model is configured in `config.yaml`, so it lands `skipped`, not `done`.
- Any `failed` element → the script already exited with `PIPELINE FAILED` and the stage's `last_error`; hand off to `transcripter-troubleshooting`.
- Non-terminal statuses (e.g. `running`) persisting past the wait window → `TIMEOUT waiting`; check `docker compose logs -f worker` before re-running.

## Inspecting results afterward

The script cleans up its `server/.e2e-work` scratch dir, but the recording row and files persist:

- **On disk:** `server/storage/recordings/<id>/` holds `audio.flac` plus `meta/` with `transcript.md`, `segments.json`, `diarization.json`, `diarized-transcript.md`.
- **Via API** (all with `-H "authorization: Bearer dev-local-token"`):
  - `GET http://localhost:8090/recordings/{id}` — detail incl. state and per-stage status/`last_error`.
  - `GET http://localhost:8090/recordings/{id}/artifacts/{stage}` — artifact content; `{stage}` is one of `transcribe`, `diarize`, `merge_speakers`, `summarize` (note: the diarized transcript belongs to `merge_speakers`, not `diarize`).

## Gotchas

- `summarize = skipped` is **expected**, not a failure: summarize is disabled until a model is configured in `config.yaml`. The smoke test is green with `done,done,done,skipped`.
- The synthetic audio is sine tones, so the transcript is empty placeholder text like `**None [00:00:02 - 00:00:15]:** ... ...`. The smoke proves the **pipeline** works; it says nothing about ML quality — judge quality only on a real speech recording.
- The script assumes the stack is **already up** and the worker is **past its first-start model download** (faster-whisper `small` lands in the `models` volume; readiness = the `worker started on queue transcripter-pipeline` log line). If you run it right after `docker compose up -d`, the step-8 poll loop silently burns the entire 600 s wait window before timing out.
- The work dir is `server/.e2e-work`, deliberately under the project path: containers cannot see host `/tmp`, so scratch must live where the ffmpeg container fallback can bind-mount it. A `trap` removes it on exit.
- The script was made portable this session: `fsize()` (`wc -c`) and `sha256()` (`sha256sum` else `shasum -a 256`) helpers, and host `ffmpeg` preferred over spawning a container, because GNU `stat -c%s` and `sha256sum` do not exist on macOS/BSD. It works on both now — do not "fix" it back to GNU coreutils.
