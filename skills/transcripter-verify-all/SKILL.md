---
name: transcripter-verify-all
description: Front door for the transcripter repo — the ordered, end-to-end sequence to build the whole project and prove it works: preflight the toolchain, bring up the Docker server stack, wait for worker readiness, run unit suites + lint, run the e2e smoke, build the Tauri client, launch the app and verify the UI. Routes each phase to a sibling transcripter-* skill instead of restating detail. Use when asked to "build the project", "set up transcripter", "spin up the stack and run it", "does it work end to end", "verify everything", or "full smoke test".
metadata:
  version: "1.0"
---

# transcripter-verify-all

Ordered, end-to-end verification for transcripter (Tauri v2 + SvelteKit client,
FastAPI + Temporal server in Docker). This is a routing skill: each phase names
the sibling skill that owns the detail, plus a one-line pass criterion. Run the
phases in the numbered order.

## Pipeline

0. **Preflight the toolchain.** Required: docker + compose plugin, node, pnpm,
   rust (cargo), uv. Host `ffmpeg` + `jq` are needed by the e2e smoke script.
   Single check, from the repo root:

   ```bash
   docker --version && docker compose version && node --version && pnpm --version && cargo --version && uv --version && ffmpeg -version | head -1 && jq --version
   ```

   Pass: every line prints a version. Fix missing pieces before phase 1.

1. **Server stack up** → `transcripter-stack-up`.
   Pass: six services healthy/up (api, worker, postgres, temporal,
   temporal-ui, diarization) and `curl -s http://localhost:8090/health`
   returns `{"status":"ok"}`.

2. **Wait for worker readiness.** The worker downloads the faster-whisper
   `small` model into the `models` volume on first start before it can serve
   work. Watch `docker compose logs -f worker` (from `server/`).
   Pass: `worker started on queue transcripter-pipeline` appears in the log.

3. **Unit suites + lint** → `transcripter-test-suite`.
   Pass: all three suites report **0 failed** (server/api pytest,
   server/worker pytest, `cargo test` in `client/src-tauri`) and lint clean.
   Do not assert exact test counts — they grow with the codebase.

4. **Pipeline proof** → `transcripter-e2e-smoke`.
   Pass: `E2E SMOKE PASSED` with terminal stages `done,done,done,skipped`
   (transcribe, diarize, merge done; summarize skipped by config — that is
   success, see Gotchas).

5. **Client build** → `transcripter-client-build`.
   Pass: SvelteKit `pnpm build` clean and `cargo build` in
   `client/src-tauri` exits 0.

6. **App launch + UI verification** → `transcripter-client-run`.
   Pass: the `target/debug/transcripter` window process is present,
   Settings → Test connection reports ok, and the Recordings table lists the
   smoke recording from phase 4.

7. **Anything red** → `transcripter-troubleshooting`. Bring the failing phase
   number and its exact error; that skill maps known failure modes.

## Observed timings (macOS arm64, Apple M4 Pro)

Budget the wait; none of these are hangs:

| Phase | Time |
| ----- | ---- |
| `docker compose build` (phase 1) | ~37s |
| `docker compose up -d`, first run | ~145s — gated on diarization's 120s healthcheck `start_period` |
| First `cargo build` (phase 5) | ~34s, compiles ~419 crates |
| `e2e_smoke.sh` (phase 4), warm stack | ~18s |

## Quick full verify (server-side gates)

Stack already up and worker ready? Chain the remaining server-side gates in one
paste, from `server/`:

```bash
curl -sf http://localhost:8090/health >/dev/null && \
docker compose logs worker 2>/dev/null | grep -q "worker started on queue transcripter-pipeline" && \
(cd api && uv run pytest -q) && \
(cd worker && uv run pytest -q) && \
TRANSCRIPTER_TOKEN=dev-local-token bash scripts/e2e_smoke.sh
```

Exit 0 with `E2E SMOKE PASSED` at the end = server side green.

## Teardown

`cd server && docker compose down`. Do NOT add `--volumes`: the `models`
volume holds the faster-whisper model download, and dropping it forces a
re-download plus a slow worker start on the next bring-up. `pgdata` (Postgres)
can stay too; the e2e smoke recreates its recording each run.

## What this proves — and what it does not

- **Proves:** full wiring (API ↔ Temporal ↔ worker ↔ Postgres ↔ diarization),
  resumable chunked upload with SHA-256 verification, and all four pipeline
  stages end-to-end against a real upload.
- **Does NOT prove ML transcript quality:** the smoke audio is synthetic sine
  tones, so the transcript is legitimately empty text. The pipeline is proven;
  model quality is not. Judge quality with a real speech recording.
- **Does NOT exercise real capture:** mic / system-audio recording and upload
  from the app go through Tauri `invoke`, which works only in the app window
  (the plain browser at `:5173` cannot drive them). Full capture verification
  needs a human recording made in the app window.

## Gotchas

- **Run the phases in order.** The e2e smoke (phase 4) needs the stack AND a
  ready worker, or it just burns its wait window (default 600s) polling stages
  that will never move and fails late. Phases 1 + 2 are its precondition.
- **Never start a second stack or second app.** A second `docker compose up`
  collides on ports 8090 / 8082 / 8070, and a second `pnpm tauri dev` collides
  on vite's `strictPort` 5173. Check `docker compose ps` /
  `pgrep -fl "target/debug/transcripter"` first; reuse the running instance.
- **`summarize` = `skipped` is success.** The stage is disabled until a model
  is configured in `server/config.yaml`; the smoke asserts `skipped`, not
  `done`. Do not "fix" it by enabling summarization mid-verify.
- **Apple Silicon: expect the amd64 emulation warning.**
  `lintoai/linto-diarization-pyannote:2.3.0` is linux/amd64 only, so compose
  logs `The requested image's platform (linux/amd64) does not match the
  detected host platform (linux/arm64/v8)`. It works — just slower, and the
  cause of the 120s `start_period` in phase 1's wait.
