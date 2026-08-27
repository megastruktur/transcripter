# Transcriptor Maximus — product specification

## Prerequisites
- The app MUST target macOS + Windows clients.
- The app MUST be a client-server app.
- The server MUST be dockerized for easier hosting.
- The server MUST have settings for the transcribe and summarize models. Both
  can be hosted locally, with the ability to use an API key instead.

## Locked decisions (2026-08-21, MVP shipped)

- Post-processing pipeline (not real-time); diarization in the MVP; LAN +
  bearer token.
- Client: Tauri v2 + SvelteKit SPA (adapter-static, fallback index.html),
  no React.
- Server: FastAPI + Temporal (auto-setup + postgres + UI) + LinTO
  `linto-diarization-pyannote` (CPU).
- Audio: one 48 kHz mono FLAC containing the microphone plus selected system
  output. macOS 14.2+ uses a Core Audio process tap; Windows 10 1703+/11 uses
  WASAPI shared-mode loopback. Client encoder is flacenc (pure Rust).
- Delivery: client-side spool → resumable offset-PUT upload → SHA-256
  finalize → spool cleaned after ack.
- Regenerate: `POST /recordings/{id}/regenerate {stage}` — downstream stages
  always re-run.
- Archive list: `GET /recordings` is paginated and filtered server-side —
  `?limit=&offset=&q=&state=` returns `{items, total, limit, offset}`; the
  client library pages at 20 rows and sends its search box / state filter
  through `q` / `state`.
- Summarize: enabled via the platform LiteLLM proxy (OpenAI-compatible) —
  local `qwen3.8-27b-q4_k_m` at `http://192.168.3.23:4000/v1`, key from the
  `LITELLM_API_KEY` env var. The stage still self-skips when unconfigured.
- Transcript export: every finished recording is exported as a folder
  `{YYYY-MM-DD_HH-MM} {title} {id8}/` in the same configurable host directory
  (`TRANSCRIPTS_DIR` in `.env`, bind-mounted at `/transcripts`); inside, the
  meta artifacts 1:1 (`transcript.md`, `diarized-transcript.md`, `summary.md`),
  each with its own frontmatter; recording rename renames the folder in place
  (Obsidian edits survive); regenerate rewrites artifact files; legacy flat
  `* {id8}.md` notes are migrated (deleted) on export; best-effort,
  subprocess-isolated, `worker.backfill` re-exports and migrates all.
- Pre-flight opens and probes every selected source before recording. A selected
  system source that cannot start blocks recording; microphone-only capture is
  available only when System audio is explicitly Off.
- Ports: api 8090, temporal-ui 8082, diarization 8070 (host port conflicts).
