# Transcripter — product specification

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
- Audio: FLAC everywhere (transport and storage); client encoder is flacenc
  (pure Rust).
- Delivery: client-side spool → resumable offset-PUT upload → SHA-256
  finalize → spool cleaned after ack.
- Regenerate: `POST /recordings/{id}/regenerate {stage}` — downstream stages
  always re-run.
- Summarize: disabled until a model is configured (OpenAI-compatible,
  base_url + key_env).
- Pre-flight check on every recording start (permissions + RMS probe) —
  lesson from the reference project (silent empty first recording).
- Ports: api 8090, temporal-ui 8082, diarization 8070 (host port conflicts).
