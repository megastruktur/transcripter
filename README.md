# Transcripter Maximus

Личный клиент-серверный рекордер звонков: запись (mic + system audio) на
Win/mac клиенте → доставка на сервер → транскрипция → диаризация → саммари,
с per-stage regenerate. LAN, single-user, bearer-token auth.

## Архитектура

```
client/  Tauri v2 + SvelteKit SPA (Rust core: cpal capture, flacenc, spool, resumable uploader)
server/  Docker Compose: api (FastAPI) + worker (Temporal) + postgres +
         temporal auto-setup + temporal-ui + LinTO diarization
```

Пайплайн: `transcribe` (faster-whisper локально или OpenAI-API) → `diarize`
(LinTO pyannote CPU) → `merge_speakers` (IoU word↔segment) → `summarize`
(OpenAI-compatible, отключён без модели). Durable execution — Temporal;
каждая стадия ретраится ×2, фейл ≠ блокер остальных.

## Запуск сервера

```bash
cd server
cp config.example.yaml config.yaml      # при необходимости поправить модель/пути
echo 'TRANSCRIPTER_TOKEN=<секрет>' > .env
docker compose up -d
```

- API: `http://localhost:8090` (health: `GET /health`)
- Temporal UI: `http://localhost:8082` (namespace default, queue transcripter-pipeline)
- Диаризация: `http://localhost:8070` (LinTO, HTTP API)
- Хранилище: `server/storage/recordings/<uuid>/{audio.flac, meta/*}` —
  путь к NAS задаётся bind-mount'ом в compose (`./storage` → ваш маунт).

## Запуск клиента

Требуется Node 22 + pnpm, Rust (для GUI-таргетов — Win/mac с webkit-пакетами):

```bash
cd client
pnpm install
pnpm tauri dev     # окно приложения
```

Настройки в приложении: Settings → Server URL (`http://<server>:8090`) + token.

## API (bearer auth)

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/recordings` | создать запись (uuid, стадии) |
| PUT | `/recordings/{id}/audio?offset=N` | чанк аплоада (≤16MB), resume с overlap |
| POST | `/recordings/{id}/finalize` | sha256-проверка → запуск workflow |
| GET | `/recordings[/{id}]` | список/детали со стадиями |
| POST | `/recordings/{id}/regenerate` | перегенерация стадии `{"stage": "..."}` |
| GET | `/recordings/{id}/artifacts/{stage}[?file=segments.json]` | артефакты |
| GET | `/recordings/{id}/audio` | скачать flac |
| GET | `/settings` | эффективный конфиг (секреты замаскированы) |

## E2E

```bash
cd server && bash scripts/e2e_smoke.sh
```

Генерирует синтетический 2-голосной FLAC, заливает с обрывом и resume,
проверяет бит-идентичность, ждёт пайплайн, проверяет артефакты.

## Разработка на locked-down хосте (без sudo)

Репо-специфика: cargo-линкер `zig cc` (`~/.local/bin/zigcc`), pkg-config через
docker (`~/.local/bin/pkg-config`), системные .so в `client/src-tauri/.link-libs/`
(gitignored). `cargo test --lib` — с
`RUSTFLAGS="-C linker=$HOME/.local/bin/zigcc -L $PWD/.link-libs" LD_LIBRARY_PATH=$PWD/.link-libs`.
GUI-сборки — на таргетах Win/mac (webkitgtk на dev-хосте отсутствует).

## Тесты

- server/api: `uv run pytest` (29: auth, upload/resume, regenerate, artifacts)
- server/worker: `uv run pytest` (4: merge IoU/nearest/single)
- client: `cargo test --lib` (7: spool, flac encode, sha256)
- линт: `uvx ruff check .`, `uvx pyright` (оба пакета); client `cargo clippy -- -D warnings`
