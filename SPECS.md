## Prerequisites
- The app MUST be be MacOS + Windows clients.
- The app MUST be a client-server app
- The server MUST be a dockerized container for easier hosting
- The Server MUST have settings to set the transcribe and summarize model. Both could be hosted locally, but ability to set the API key.

## Locked decisions (2026-08-21, MVP shipped)

- Post-processing (не real-time); диаризация в MVP; LAN + bearer token.
- Клиент: Tauri v2 + SvelteKit SPA (adapter-static, fallback index.html), без React.
- Сервер: FastAPI + Temporal (auto-setup + postgres + ui) + LinTO linto-diarization-pyannote (CPU).
- Аудио: FLAC везде (транспорт и хранение); client encoder — flacenc (pure Rust).
- Доставка: spool на клиенте → resumable offset-PUT → finalize sha256 → spool чистится после ack.
- Regenerate: POST /recordings/{id}/regenerate {stage} — downstream прогоняется всегда.
- Summarize: выключен, пока не задана модель (OpenAI-compatible, base_url+key_env).
- Pre-flight на каждый старт записи (пермишены + RMS-проба) — урок референса (пустая первая запись).
- Порты: api 8090, temporal-ui 8082, diarization 8070 (конфликты хоста).
