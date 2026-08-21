---
slug: transcripter-mvp
created: 2026-08-21
work_unit: feature
estimated_scope:
  files: ~55
  lines: ~6000
  tests: ~35
---

# Transcripter Maximus — MVP

## TL;DR

Клиент-серверный рекордер звонков: Tauri v2 + SvelteKit клиент (Win/mac) пишет mic+system audio во FLAC и грузит на сервер по resumable-протоколу; сервер (Docker Compose: FastAPI + Temporal + Postgres + LinTO) прогоняет транскрипцию → диаризацию → саммари с per-stage regenerate, артефакты в файловом layout + SQLite/PG каталог. LAN, single-user, bearer token.

## Goal & Definition of Done

**Goal:** Записать звонок на Win/mac клиенте → аудио доставлено на сервер (переживает обрыв сети) → артефакты (transcript.md, diarized transcript, summary.md) видны в UI клиента; каждую стадию можно перегенерировать кнопкой.

**Definition of Done (machine-verifiable):**
- `docker compose up -d` в `server/` поднимает api+worker+temporal+postgres+temporal-ui+diarization, `curl -s localhost:8080/health` → `{"status":"ok"}`, `docker compose ps worker` → running
- `pytest server/tests/` → 0 failed (auth, upload resume, catalog, regenerate contract)
- Upload 50MB файла с обрывом на середине (kill connection) → повтор с offset → файл на сервере бит-в-бит (sha256 совпадает), `pytest -k resume`
- Запуск workflow на тестовом WAV (5 мин, 2 голоса, модель whisper `small` CPU) → за ≤15 мин: `recordings/<id>/meta/{segments.json,diarization.json,summary.md,transcript.md}` существуют, статусы стадий `done`; large-v3 — опция, DoD-тайминг к ней не применяется
- `summarize` без модели в конфиге → стадия `skipped`, workflow завершается успешно
- `POST /recordings/{id}/regenerate {"stage":"transcribe"}` → новый run, артефакт перезаписан, timestamp обновлён
- Клиент (`pnpm tauri dev`): старт записи без mic-пермишена → явная ошибка в UI, запись не начинается; с пермишеном и тишиной >30s → warning «нет сигнала»
- `pnpm check` (svelte-check) и `cargo check` в `client/` → без ошибок

## Architecture

```
Client (Tauri v2: SvelteKit SPA + Rust core)          LAN HTTP + bearer
├─ capture: cpal mic + system (Win: WASAPI loopback via default_output_device;
│  mac: CoreAudio same trick — reference approach), FLAC encode on the fly
├─ spool: {app_data}/spool/<uuid>/{session.json, audio.flac}
├─ uploader: offset-chunked PUT, retry/backoff, delete after server ack
└─ UI: record/stop, list+stage statuses, artifacts view, regenerate, settings
↓ REST
Server (docker compose, server/)
├─ api (FastAPI, uv): auth, catalog, resumable upload, regenerate, artifacts, SSE statuses
├─ postgres: recordings/stages catalog
├─ temporal (auto-setup) + temporal-ui: durable pipeline
├─ worker (FastAPI-adjacent process): Temporal activities
│   transcribe (faster-whisper local | OpenAI-API) → diarize (LinTO HTTP)
│   → merge_speakers (IoU words↔segments) → summarize (OpenAI-compatible, opt-in)
└─ storage volume: $STORAGE_PATH/recordings/<id>/audio.flac + meta/*.json|md
```

Regenerate semantics: `POST /recordings/{id}/regenerate {"stage":...}` → новый Temporal workflow run от указанной стадии; downstream-стадии (merge зависит от transcribe+diarize; summarize зависит от merge) прогоняются заново ВСЕГДА (просто и предсказуемо; hash-сравнение входов — post-MVP refinement).

## Files Touched

| File | Change type | Description |
|------|-------------|-------------|
| `server/docker-compose.yml` | Add | api, worker, postgres, temporal auto-setup, temporal-ui, diarization, volumes |
| `server/api/pyproject.toml` | Add | FastAPI, uvicorn, temporalio, faster-whisper, httpx, pydantic |
| `server/api/app/config.py` | Add | config.yaml + env loading (storage path, models, token) |
| `server/api/app/db.py` | Add | SQLAlchemy/sqlite→PG catalog: recordings, stages |
| `server/api/app/main.py` | Add | FastAPI app, auth middleware, routes |
| `server/api/app/routes/{recordings,upload,settings}.py` | Add | REST endpoints |
| `server/api/app/temporal_client.py` | Add | Temporal client + start/regenerate workflows |
| `server/worker/{pyproject.toml,activities.py,workflows.py,main.py}` | Add | Temporal worker: 4 activities + workflow |
| `server/worker/{audio.py,transcribe.py,diarize.py,merge.py,summarize.py}` | Add | Stage implementations |
| `server/tests/{test_auth,test_resume,test_catalog,test_regenerate}.py` | Add | Contract tests |
| `server/config.example.yaml` | Add | Documented config sample |
| `server/Dockerfile.api` | Add | uv-based image |
| `server/Dockerfile.worker` | Add | worker image (faster-whisper + ffmpeg) |
| `client/package.json` | Add | SvelteKit + adapter-static + @tauri-apps/* |
| `client/svelte.config.js` | Add | adapter-static, fallback index.html |
| `client/src-tauri/tauri.conf.json` | Add | devUrl 5173, frontendDist ../build |
| `client/src-tauri/src/{lib.rs,capture.rs,encode.rs,spool.rs,uploader.rs,permissions.rs}` | Add | Rust core |
| `client/src-tauri/Cargo.toml` | Add | tauri v2, cpal, flac-encoder (pure-Rust; fallback: flac-bound/libflac-sys при проблемах на win/mac), reqwest, tokio |
| `client/src/routes/{+layout.svelte,+page.svelte,settings/+page.svelte}` | Add | UI |
| `client/src/lib/{api.svelte.ts,stores.svelte.ts}` | Add | API client, state |
| `SPECS.md` | Modify | Add locked decisions |
| `README.md` | Add | Stack overview + run instructions |

## Approach

Разделение: клиент — только захват, кодирование, spool, доставка и UI; сервер — весь ML и хранение. Resumable upload сделан примитивно и надёжно: `POST /recordings` создаёт запись и upload-session, `PUT /recordings/{id}/audio?offset=N` дописывает чанки (сервер отвечает committed size, персистит его в каталоге), `POST /recordings/{id}/finalize` сверяет sha256 и запускает workflow. Это переживает обрыв соединения, смерть api-контейнера и рестарт сервера без external object-store. Temporal выбран осознанно (user opt-in): activities с heartbeat + retry policies дают durability на многочасовых стадиях, regenerate — просто новый workflow run; альтернатива «SQLite + in-process worker» отвергнута пользователем. Диаризация — готовый CPU-контейнер LinTO (веса вшиты, HF-токен не нужен), склейка слов со спикерами — наш ~50-строчный IoU-мерджер. Захват аудио — чистый cpal по образцу ActaVoces (проверено на Win, рискованнее на mac), отличие: мы стримим на диск через FLAC-энкодер вместо накопления i16 в RAM.

## Key Assumptions

- `[VERIFIED]` SvelteKit SPA = официальный путь Tauri (tauri.app/start/frontend/sveltekit: adapter-static + fallback index.html + devUrl/frontendDist) — прочитано в доках
- `[VERIFIED]` ActaVoces захватывает system audio как input stream на `host.default_output_device()` (src-tauri/src/capture/audio_devices.rs:214-219) — прочитано в исходнике
- `[VERIFIED]` LinTO pyannote: CPU-only образ, веса вшиты, HTTP API — research doc, Dockerfile пруф
- `[REASONABLE]` faster-whisper читает FLAC через встроенный ffmpeg-decoder — стандартное поведение, проверим на фазе T3
- `[REASONABLE]` cpal input stream на output device работает на macOS как у референса («lighter use» в их README) — риск-зона, проверка в T8 на реальном маке
- `[FRAGILE]` Tauri v2 + SvelteKit без проблем собираются под Win/mac в 2026-08 — проверка каркаса в T7; при проблемах fallback на Vite+Svelte (не Kit) без смены UI-кода
- `[FRAGILE]` macOS permission-check API (AVCaptureDevice.authorizationStatus) доступен из Rust через objc2 — если нет, fallback: короткий тест-захват 1 сек + анализ RMS перед стартом реальной записи (работает на обеих ОС)
- `[FRAGILE]` Temporal auto-setup single-container достаточен для single-user LAN (без Elasticsearch) — docs говорят да для dev; при проблемах замена на temporal-cli `docker run` server

## Pre-Mortem

**Scenario 1: macOS системное аудио не захватывается (пустой второй поток).**
Addressed by: захват двух потоков независим; запись не блокируется, если есть mic. Клиент помечает system stream как missing в session.json; UI показывает warning. План Б (post-MVP): ScreenCaptureKit. Также pre-flight проверяет наличие default_output_device до старта.

**Scenario 2: обрыв сети на 40-й минуте 600MB FLAC.**
Addressed by: uploader стартует сразу после стопа записи, чанки по 8MB с offset; после повторного коннекта клиент спрашивает committed size у сервера и продолжает. Spool не чистится до finalize-ack. Тест test_resume имитирует kill connection.

**Scenario 3: Temporal workflow завис (диаризация 2-5× RT на CPU).**
Addressed by: heartbeat + StartToCloseTimeout с запасом (расчёт от длительности аудио), RetryPolicy максимум 2 попытки, дальше стадия `failed` + ручной regenerate. Activity Timeout раз в 10 сек пингует LinTO `/healthcheck`.

**Scenario 4: юзер стартует запись без пермишена на mic → пустой файл (баг референса, случился у юзера).**
Addressed by: pre-flight на каждом старте: permission-check (mac: objc2 AVCaptureDevice; win: попытка открыть device) + 1-сек probe-захват с RMS-порогом; при провале — явная ошибка, запись не стартует. DoD-пункт это фиксирует.

**Scenario 5: на NAS-маунте кончилось место во время upload.**
Addressed by: `os.statvfs` проверка свободного места в upload-роуте (413/507 при <2× размера чанка), частичный файл чистится, клиент показывает ошибку.

**Scenario 6: две версии клиента (win/mac) с разным поведением пермишенов.**
Addressed by: pre-flight абстрагирован в permissions.rs с единым контрактом (enum PermissionState), UI получает одинаковое состояние.
**Scenario 7: cold-start whisper-модели (download/загрузка в RAM) валит первый workflow по таймауту.**
Addressed by: модель скачивается при build образа worker (T1) и preload-ится при старте контейнера (T3); таймауты activity = f(audio_duration, model) с запасом на прогрев.

## Task Breakdown

#### T1. Compose-скелет сервера + scaffold репо
- **Files:** `server/docker-compose.yml`, `server/Dockerfile.api`, `server/Dockerfile.worker`, `server/config.example.yaml`, `.gitignore`, `SPECS.md` (modify)
- **Acceptance:** `docker compose up -d` поднимает api (health ok), worker (running, temporal-воркер зарегистрирован), postgres, temporal (+ui на :8081), diarization (healthcheck ok); named volumes для pg/temporal, bind-mount для storage; whisper-модель скачивается при build образа worker (offline-старт контейнера); config.example.yaml документирует все ключи, auth-токен задаётся ТОЛЬКО env (`TRANSCRIPTER_TOKEN`), не в yaml
- **Depends on:** _none_

#### T2. API: auth, каталог, resumable upload
- **Files:** `server/api/app/{config.py,db.py,main.py}`, `server/api/app/routes/{recordings,upload,settings}.py`
- **Wave:** 2 (после T1 — нужны контейнеры)
- **Depends on:** T1
- **Acceptance:** bearer auth (401 без/с кривым токеном, токен из env); `POST /recordings` → uuid id; `PUT /recordings/{id}/audio?offset=N` (id валидируется regex uuid, offset ∈ [0, committed], max chunk 16MB) пишет чанк и возвращает `{"committed": N}`; finalize сверяет sha256 → 409 при несовпадении; `GET /recordings` отдаёт список со стадиями; `statvfs`-guard работает

#### T3. Temporal worker + transcribe
- **Files:** `server/worker/{main.py,workflows.py,activities.py,transcribe.py,audio.py}`
- **Wave:** 3
- **Depends on:** T1 (temporal), T2 (finalize запускает workflow)
- **Acceptance:** finalize стартует workflow `process_recording`; worker при старте контейнера preload-ит whisper-модель из конфига (warm, cold-start ≤60s не блокирует первый activity — таймауты учитывают прогрев); activity transcribe: local faster-whisper (модель из конфига) | API-режим; пишет `meta/segments.json` (words+timestamps), `transcript.md`; StartToCloseTimeout = f(audio_duration, model); stage status в каталоге обновляется прямым DB-write из worker

#### T4. diarize + merge_speakers
- **Files:** `server/worker/{diarize.py,merge.py}`
- **Wave:** 4 (параллельно с T5 — разные файлы)
- **Depends on:** T3 (workflow-структура, segments.json как вход merge)
- **Acceptance:** diarize зовёт LinTO `/diarization`, пишет `meta/diarization.json` (segments+speakers); merge клеит word↔speaker по max-IoU, пишет `diarized-transcript.md`; unit-тест merge на синтетических таймингах (overlap, gap, одиночный спикер)

#### T5. summarize activity
- **Files:** `server/worker/summarize.py`
- **Wave:** 4
- **Depends on:** T3
- **Acceptance:** без модели в конфиге → `skipped` (workflow ok); с моделью → OpenAI-compatible call (base_url+key из конфига), пишет `meta/summary.md`; таймаут+1 retry

#### T6. Regenerate + SSE статусы
- **Files:** `server/api/app/routes/recordings.py` (extend), `server/api/app/sse.py`
- **Wave:** 5
- **Depends on:** T3–T5 (все стадии существуют)
- **Acceptance:** `POST /recordings/{id}/regenerate {"stage":...}` → новый run с указанной стадии; повторный запуск того же workflow-id с run-id; SSE `/recordings/{id}/events` шлёт смены статусов; клиентский poll без SSE тоже работает (GET stages)

#### T7. Клиент: scaffold Tauri+SvelteKit
- **Files:** `client/*` (package.json, svelte.config.js, tauri.conf.json, src-tauri скелет, пустые роуты)
- **Wave:** 1 (не зависит от сервера!)
- **Depends on:** _none_
- **Acceptance:** `pnpm tauri dev` открывает окно на linux-хосте разработки; `pnpm check` + `cargo check` зелёные; пустые страницы: Recorder, Recordings, Settings

#### T8. Захват + spool + pre-flight
- **Files:** `client/src-tauri/src/{capture.rs,encode.rs,spool.rs,permissions.rs}`
- **Wave:** 2 (стартует сразу после T7, параллельно всему серверному треку)
- **Depends on:** T7
- **Acceptance:** запись mic (linux dev-хост) пишёт FLAC в spool; pre-flight: нет пермишена → PermissionState::Denied + сообщение; тишина (RMS ниже порога 30 сек) → warning-событие в UI; стоп → session.json с duration/channels/hash-in-progress

#### T9. Uploader
- **Files:** `client/src-tauri/src/uploader.rs`, `client/src/lib/api.svelte.ts`
- **Wave:** 6
- **Depends on:** T7 (scaffold), T2 (контракт upload API — читать OpenAPI/код)
- **Acceptance:** после стопа uploader стартует сам; retry с backoff; после обрыва (тест: остановить api-контейнер) возобновляется с committed offset; после finalize-ack spool-файл удаляется

#### T10. UI клиента
- **Files:** `client/src/routes/*`, `client/src/lib/stores.svelte.ts`
- **Wave:** 7
- **Depends on:** T7–T9
- **Acceptance:** Recorder: start/stop, таймер, устройства, статусы pre-flight; Recordings: список+стадии (poll/SSE), просмотр transcript/diarized/summary, кнопка Regenerate на стадию; Settings: server URL, token, тест-коннект

#### T11. E2E smoke
- **Files:** `server/scripts/e2e_smoke.sh`
- **Wave:** 8
- **Depends on:** T2–T6
- **Acceptance:** скрипт: генерирует тестовый WAV (2 голоса — склейка TTS/шумовых дорожек), заливает через curl с обрывом, ждёт стадии, проверяет артефакты; exit 0 = DoD-пункты upload+pipeline выполнены

#### T12. Доки + memory + бэкапы
- **Files:** `README.md`, `SPECS.md`, `MEMORY.md`, Serena memory
- **Wave:** 8
- **Depends on:** все
- **Acceptance:** README описывает запуск и конфиг; SPECS дополнен решениями; MEMORY.md KEY DECISIONS подтверждены; Serena memory `transcripter` создана (правила репо: README + memory при infra-изменениях)

## Parallel Execution Plan

**Within-phase parallelization:**
- Wave 1: T1 (server compose) ∥ T7 (client scaffold) — разные директории, ноль общих файлов
- Wave 4: T4 (diarize+merge) ∥ T5 (summarize) — stage-файлы разные; изменения в shared `activities.py`/`workflows.py` выполняет один автор (server-track владелец) ПОСЛЕ завершения обоих
- Wave 6: T8 (capture) ∥ T9 (uploader) — capture.rs/spool.rs vs uploader.rs/api.svelte.ts
- Wave 8: T11 ∥ T12 (после зелёных T2–T6)

**Hard sequencing constraints:**
- T1 → T2: api-роуты деплоятся в контейнер из compose
- T2 → T3: finalize-роут запускает workflow (нужен контракт каталога)
- T3 → T4, T5: activities подключаются к существующему workflow-скелету; merge ест segments.json из T3
- T3–T5 → T6: regenerate/SSE покрывает все стадии
- T2 → T9: uploader реализует upload-контракт T2
- T7 → T8, T9, T10: клиентский код пишется в scaffold
- T8+T9 → T10: UI садится на готовые команды и события
- T2–T6 → T11: e2e гоняет полный путь

**Critical-path estimate:**
Sequential: T1 3h → T2 4h → T3 5h → T4 4h → T5 2h → T6 3h → T7 3h → T8 6h → T9 4h → T10 6h → T11 2h → T12 1h ≈ **43h**.
Parallel (2 субагента, server-track и client-track): server-track T1→T2→T3→(T4∥T5)→T6 ≈ 19h; client-track T7→(T8∥T9)→T10 ≈ 13h, стартует сразу; T11+T12 в конце ≈ 3h. Total ≈ **22h. ~49% speedup.**

**Subagent delegation:**
- Tranche A: T1+T7 — @task (scaffolding, механика)
- Tranche B: T2–T6 server-track — @task; контракт DB/upload фиксируется в T2 до старта T3
- Tranche C: T8, T9 — @task параллельно; T10 — @designer (UI/UX)
- Tranche D: T11, T12 — @task

**What NOT to parallelize:**
- T3 (workflow-скелет) внутри server-track: единый контракт стадий, правки в одном файле workflows.py — сериально внутри трека
- T10 стартует только после стабилизации команд T8/T9 (интерфейс invoke-команд)
- Regenerate-семантика (T6) — один автор, чтобы не расползлись определения статусов

## Verification Strategy

| Gate | Command |
|------|---------|
| Server lint | `cd server/api && uv run ruff check .` |
| Server types | `uv run pyright` (worker+api; ruff+pyright в dev-deps обоих pyproject) |
| Server tests | `uv run pytest server/tests -q` |
| Client types | `cd client && pnpm check` |
| Client rust | `cd client/src-tauri && cargo check && cargo clippy -- -D warnings` (scaffold-код чистый, без allow-заглушек) |
| E2E | `bash server/scripts/e2e_smoke.sh` (нужен docker compose up) |
| Compose | `docker compose config -q` |

## Rollback Plan

**Mid-flight:** задачи = отдельные коммиты; откат до последнего зелёного (тесты+check) коммита ветки. Client и server — независимые треки, откат одного не трогает другой. Docker: `docker compose down -v` чистит postgres/temporal (каталог записей на host-маунте не трогаем).
**Post-merge:** revert merge-commit; артефакты в filesystem-layout обратно совместимы (по папке на запись, имена файлов стабильны); БД-каталог пересобираем из метаданных на диске (planned: `scripts/rebuild_catalog.py` — MVP fallback, ручной запуск).

## Non-goals

- Real-time транскрипция/стриминг
- Multi-user, роли, web-UI сервера (только Temporal UI как админ-панель)
- TLS/reverse-proxy (LAN; Traefik — post-MVP)
- Speaker identification по голосу (только метки Speaker 1/2…, переименование спикеров — post-MVP)
- Авто-удаление старых записей, retention-политики
- Windows-подпись сборок, автoupdater
- Linux-клиент (dev-хост linux достаточен для разработки; таргеты — win/mac)

## Open Questions

- Нужен ли SSE в MVP или хватает poll каждые 3-5 сек (решение в T6 — poll дефолт, SSE если дёшево)
- Граница чанка upload: 8MB дефолт, тюнить по LAN-реальности
- Как называть записи без ручного ввода: timestamp-имя папки (дефолт) vs заголовок из саммари (post-MVP)

## References

- SPECS.md; MEMORY.md (KEY DECISIONS #1–#13)
- Research: ~/.hermes/kanban/attachments/t_cae49ae2/diarization-research.md
- Reference: /home/megastruktur/projects/vendor/actavoces (capture: src-tauri/src/capture/*)
- Tauri×SvelteKit: https://tauri.app/start/frontend/sveltekit
