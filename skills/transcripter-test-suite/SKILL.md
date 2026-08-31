---
name: transcripter-test-suite
description: >-
  Run and interpret the transcripter unit test and lint gates: pytest for the
  FastAPI API (server/api) and Temporal worker (server/worker), cargo test for
  the Tauri client Rust code (client/src-tauri: uploader retry classification, FLAC
  encode, spool), plus ruff, pyright, cargo clippy, and pnpm check. Use when
  asked to run the tests, test this codebase, verify a change, check the lint
  or quality gates, or validate a commit before opening a PR. These suites are
  in-process and need no Docker stack; for the end-to-end pipeline smoke test
  use transcripter-e2e-smoke, and for "check everything" use
  transcripter-verify-all.
metadata:
  version: "1.0"
---

Every gate below is in-process: no Docker stack, no running API, no Tauri app
required. The stack is only needed for the e2e smoke test
(`transcripter-e2e-smoke`). Run each gate after touching the component it
covers, and run all of them before claiming a change is done or opening a PR.

## Test gates

### API unit suite — `server/api`

```bash
cd server/api && uv run pytest
```

Expect all green (33 passed at time of writing — treat **0 failed** as the
gate, not a fixed count). Tests live in `server/api/tests/`:

- `conftest.py` — autouse fixture: points `TRANSCRIPTER_CONFIG` at
  `config.example.yaml`, uses a tmp-path sqlite DB and tmp storage dir, and
  mocks Temporal (`temporal_client.start_pipeline` / `regenerate_stage` as
  `AsyncMock`). That is why the suite needs no Temporal, no Postgres.
- `test_auth.py` — bearer-auth middleware contract: `/health` public,
  protected routes 401 without/wrong token, 200 with valid token, auth
  disabled when `TRANSCRIPTER_TOKEN` is unset, fail-fast on missing or
  directory config. Includes
  `test_cors_preflight_bypasses_auth`, which locks in this session's fix:
  `bearer_auth` previously wrapped `CORSMiddleware` (FastAPI `add_middleware`
  prepends) and 401'd CORS preflight, which carries no `Authorization` by
  spec. Preflight must now return 200 with `access-control-allow-origin`.
- `test_upload.py` — upload/resume/finalize HTTP contract via
  `fastapi.testclient.TestClient`: create returns uuid + stages, resume from a
  committed offset, 409 on bad finalize hash / no audio / offset out of range,
  413 on oversized chunk, list/delete, finalize FLAC frame validation.
- `test_regenerate.py` — stage regeneration (400 unknown stage, 409 while
  uploading/processing, 503 when Temporal is down) and artifact/audio serving
  endpoints.

All in-process: `TestClient` drives the ASGI app directly, so a running
container or port is never needed.

### Worker unit suite — `server/worker`

```bash
cd server/worker && uv run pytest
```

Expect all green (6 passed at time of writing; **0 failed** is the gate).
Single file `server/worker/tests/test_merge.py`,
covering pure pipeline logic: `merge()` speaker assignment (max-overlap,
nearest-segment gap fallback, single-speaker case), `write_diarized`, LinTO
field mapping (`seg_begin`/`seg_end`/`spk_id` → `start`/`end`/`speaker`), and
`merge_speakers` skipping (and dropping stale artifacts) when there is no
usable diarization. No Temporal, no model downloads.

### Client Rust suite — `client/src-tauri`

```bash
cd client/src-tauri && cargo test
```

Expect all green (17 passed at time of writing — counts grow as the client
gains coverage, so treat **0 failed** as the gate, not a fixed number). Tests
are inline in the modules:

- `src/uploader.rs` — error classification — permanent
  rejections (400/401/403/409/413/422) are not retried, transient ones
  (408/429/5xx/507) stay retryable.
- `src/encode.rs` — FLAC encoding: write + finish, multi-block streams
  (beyond one `BLOCK_SIZE`), and `file_sha256` against a known vector
  (`b"abc"` → `ba7816bf…`).
- `src/spool.rs` — upload spool: create/read roundtrip, `pending()` excludes
  finalized sessions, `open_root` sees the same sessions as `new` (the
  uploader retry path), and `remove` deletes the dir.

## Lint gates

Run the gate for the component you changed; run all four before a PR.

```bash
cd server/api && uvx ruff check . && uvx pyright      # pyright scope: app/
cd server/worker && uvx ruff check . && uvx pyright   # pyright scope: worker/
cd client/src-tauri && cargo clippy -- -D warnings
cd client && pnpm check   # = svelte-kit sync && svelte-check
```

`pyright` picks up its scope from each `pyproject.toml`'s `[tool.pyright]`
(`include = ["app"]` / `include = ["worker"]`), so plain `uvx pyright` in the
right directory is enough. `uvx ruff`/`uvx pyright` run the pinned tools
without a local install.

## First run

`uv run` creates the project venv on its first invocation in
`server/api` or `server/worker` and downloads dependencies from the
`uv.lock` — the initial run is slow; later runs are instant.

## Gotchas

- **Run pytest from inside the package dir, via `uv run`.** Each of
  `server/api` and `server/worker` has its own `pyproject.toml` + `uv.lock`
  and its own `pythonpath = ["."]` setting. A `pytest` from the repo root
  finds no tests and no project config — it silently collects nothing.
- **`cargo test` must run in `client/src-tauri`, not `client/`.** `client/`
  is the pnpm/SvelteKit package; the Rust crate (`transcripter`) lives in
  `src-tauri/`.
- **On the cachyos dev host `cargo test`/`cargo build` cannot link the full
  crate** (no system `glib-2.0`/`gobject-2.0`/`asound` — tauri+cpal deps, no
  sudo to install). The honest host gates are `cargo check` + `cargo clippy`;
  `cargo test` runs in CI. With `rustls-tls` in reqwest, ring also needs the
  zig wrappers (`~/.local/bin/cc` → `cc-zig`, `ar` → `ar-zig`) — plain `zigcc`
  fails ring's build with "UnknownOperatingSystem".
- **The API suite prints a `StarletteDeprecationWarning` about httpx.**
  Expected noise from `TestClient`'s httpx version; not a failure. Judge by
  the `passed` count, not the warning.
- **These suites do not prove the pipeline.** They are unit tests — the
  upload/resume/finalize contract is exercised against `TestClient` with
  Temporal mocked, and nothing here exercises the real worker, the diarization
  model, or the actual Temporal queue. Only the e2e smoke
  (`transcripter-e2e-smoke`, needs the Docker stack from
  `transcripter-stack-up`) proves end-to-end. Run the unit gates AND the e2e
  smoke before claiming the stack works.

## See also

- `transcripter-e2e-smoke` — full pipeline smoke against the running stack.
- `transcripter-verify-all` — front door that routes to the right checks.
- `transcripter-troubleshooting` — known failure modes when a gate or the
  smoke fails.
