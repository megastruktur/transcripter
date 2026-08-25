# HANDOFF: серверный чанкинг аудио (новая стадия workflow)

> Для новой сессии. Дата составления: 2026-08-25. Статус: **план утверждён устно, реализация НЕ начата**. Следующий шаг — шаг 1 из «Порядка работ» ниже.

## Контекст: что произошло

1. Запись `d19c828a-33ae-40b9-a858-612ee3d0e09f` (89.7 мин, 138 МБ FLAC) падала на транскрипции: хардкод `timeout=600` в `ApiTranscriber` + автоматический retry ×2 запускал две конкурирующие large-v3 задачи на одном CPU voice-стеке → обе таймаутили (~60 машинных минут впустую).
2. **Уже сделано и закоммичено** (`105787d fix(worker): scale stage budgets with audio length; no transcribe retry`):
   - бюджеты клиента и Temporal масштабируются: `300 с + 40 с/мин аудио`, fallback `_DEFAULT_MINUTES=150` (2.5 ч) при неизвестной длительности;
   - HTTP-бюджет на 30 с НИЖЕ Temporal-бюджета (httpx Exception → stage failed, не зависший running);
   - transcribe без авто-ретраев (`maximum_attempts=1`), regenerate только вручную из UI;
   - diarize таймаут тоже масштабирован (был фикс-3600);
   - блокирующие вызовы через `asyncio.to_thread` + heartbeat 60 с;
   - гейты пройдены: ruff, 49/49 pytest, pyright; запись потом успешно доехала end-to-end.
3. **Новая проблема (этот план её и лечит)**: после 01:01:00 транскрипция зациклилась — до конца записи 369 повторов одной фразы. Диагноз: классический **whisper repetition loop** серверной стороны (faster-whisper `condition_on_previous_text=True`): контекст предыдущих 30-с окон отравляет генерацию; начиная с ~61-й минуты каждое окно даёт один и тот же текст (compression ratio 8.755725 стабильный до сотых в логах Speaches, предупреждения с 12:16 UTC до конца прогона). Доказательство, что это НЕ transcripter: `meta/segments.json` (сырой ответ Speaches) уже содержит повторы; LinTO-диаризация покрыла весь файл до 5373 с; аудио исправно.
4. В запущенной версии Speaches 0.8.3 ручек `condition_on_previous_text` / `compression_ratio_threshold` / `no_repeat_ngram_size` **нет** (проверено в `/routers/stt.py:199` внутри контейнера `transcription-speaches`; per-request есть только `language/prompt/hotwords/temperature/vad_filter/timestamp_granularities/stream`). Пользователь обновляет версию Speaches САМ — не трогать платформенный voice-стек.

**Решение пользователя: серверный чанкинг — новая первая стадия workflow ДО транскрипции и диаризации.** Обоснование: цикл живёт в контексте одного запроса; нарезка на чанки обнуляет контекст → один плохой чанк портит ≤10 мин, а не 2.5 ч. Независимо от версии Speaches.

## Архитектура

```
upload finalize → chunk (НОВАЯ стадия) → transcribe → diarize → merge_speakers → summarize
```

- `order` в `server/worker/worker/workflows.py`: `["chunk", "transcribe", "diarize", "merge_speakers", "summarize"]`.
- `STAGE_KINDS` дополняется `"chunk"` в ОБОИХ: `server/worker/worker/db.py:43` и `server/api/app/db.py:43`. Колонка — Postgres Enum `stage_kind` (`mapped_column(Enum(*STAGE_KINDS, name="stage_kind"))`) → потребуется `ALTER TYPE stage_kind ADD VALUE IF NOT EXISTS 'chunk'` (отдельная миграция; проверить, как API заводит stage-строки — `server/api/app/routes/recordings.py:121`, `for kind in STAGE_KINDS`).
- Regenerate принимает стадию по имени (`server/api/app/routes/regenerate.py:42`) — `"chunk"` станет валидным значением автоматически через STAGE_KINDS.

## Изменения по файлам

### 1. `server/worker/worker/config.py` — ChunkConfig

```python
class ChunkConfig(BaseModel):
    enabled: bool = False       # OFF по умолчанию: короткие записи не режем
    target_min: float = 10.0    # целевая длина чанка
    overlap_sec: float = 2.0    # перехлёст для стыков
```

В `server/config.yaml` dev-стека: `chunk: {enabled: true}`. В `config.example.yaml` — закомментированный блок.

### 2. Новый `server/worker/worker/chunk.py`

- `plan_chunks(duration_sec, target_min, overlap_sec) -> list[tuple[float, float]]` — чистая функция; ровные чанки, короткий хвост, overlap без дыр.
- Нарезка через ffmpeg (ffmpeg/ffprobe ЕСТЬ в worker-образе — `apt-get install ffmpeg` в `server/Dockerfile.worker`; на хосте тоже есть). FLAC→FLAC: `-f segment` + `-c:a flac` (перекодирование: `-c copy` с segment-muxer по точному времени не работает; декод 2.5 ч — секунды).
- Обёртка по паттерну `export_transcript`: subprocess `start_new_session=True`, при timeout — `os.killpg(SIGKILL)` и ABANDON (никогда не ждать зомби в D-state). См. `worker/activities.py` `_EXPORT_TIMEOUT_SEC` блок.
- Выход: `meta/chunks/chunk_000.flac ...` + `meta/chunks/chunks.json` (манифест: start/end/файл/status/suspect per chunk).

### 3. `server/worker/worker/activities.py`

- `@activity.defn chunk(rec_id)` — запускает нарезку, пишет манифест; идемпотентно при regenerate (пересоздаёт).
- `transcribe`: при наличии `chunks.json` — цикл по чанкам, **последовательно** (НИКАКОГО параллелизма: один CPU voice-стек, параллелизм = повторение контеншн-инцидента). Каждый чанк — отдельный POST со своим ретраем ×2 (backoff 5 с) ВНУТРИ активности. Таймстемпы сегментов и слов сдвигаются `+chunk_start` перед конкатенацией в единый `segments.json` (формат существующий — merge/summarize не меняются). При regenerate по манифесту дозаполняются только НЕ-done чанки (готовые не перезаливаются).
- `diarize`: при наличии чанков — то же: последовательные POST на LinTO по чанкам, конкатенация `diarization.json` со смещением. Спикер-лейблы per-chunk у LinTO (spk_0 в чанке 1 ≠ spk_0 в чанке 2) — осознанно оставляем: merge_speakers атрибутирует слова по overlap, ему всё равно; пользователь предупреждён.
- Бюджет чанка по HTTP: `300 + 40 × chunk_minutes` (от ДЛИНЫ ЧАНКА, не записи) − 30 с разрыва с Temporal, как уже сделано.
- Detection repetition в чанке: если >50 % сегментов чанка — идентичный текст → чанк помечается `suspect` в манифесте; при regenerate suspect-чанки пересжимаются с `initial_prompt=""` (и полем под `condition_on_previous_text=false`, когда пользователь обновит Speaches).

### 4. `server/worker/worker/workflows.py`

- `order` пополнить; блок `chunk` перед transcribe: `start_to_close_timeout = int(duration_sec×2 + 300)` (ffmpeg быстрый), `retry_policy=_retry()` (×2, дешёвый).
- `start_stage` в regenerate: `"chunk"` уже валиден через `assert start in order`.

### 5. БД-миграция

- `ALTER TYPE stage_kind ADD VALUE IF NOT EXISTS 'chunk'` — найти как у API устроены миграции (проверить `server/api/app/main.py` startup / alembic). Старые записи со строками без chunk — читать нормально; pipeline новых записей создаст строку `chunk` через `for kind in STAGE_KINDS` в recordings.py:121.

### 6. Тесты (канон репо: честные контракты)

- `server/worker/tests/test_chunk.py`: `plan_chunks` (ровные чанки; хвост 2 мин при 92-мин записи; overlap не даёт дыр/отрицательных длин); формат манифеста.
- transcribe по чанкам: смещение таймстемпов (чанк старт 600 с, сегмент 5.2 с → 605.2 с); retry-per-chunk (мок: чанк 2 падает раз → успех); fallback без манифеста → старое поведение (existing tests test_transcribe_api.py продолжают проходить).
- diarize по чанкам: конкатенация со смещением, suspect-маркировка >50 % повторов.
- e2e: `server/scripts/e2e_smoke.sh` с `chunk.enabled=true` → `done,done,done,skipped` на синтетике.

### 7. Доки/память

- `docs/backend-architecture.md` — порядок стадий + бюджеты + таблица отказоустойчивости ниже.
- README.md — раздел про чанкинг.
- Serena memory `transcripter_stack` (правило репо: инфра-изменения → README + Serena memory).

## Таблица отказоустойчивости (согласовано с пользователем)

| Сбой | Поведение |
|---|---|
| ffmpeg упал/timeout | Стадия chunk failed + retry ×2; pipeline дальше не идёт — явно в UI |
| Чанк не транскрибировался после 2 попыток | Внутриактивный retry ×2; при исчерпании — stage failed c `last_error = "chunk N of M: ..."`; regenerate дозаполняет ТОЛЬКО не-done чанки по манифесту |
| Speaches лёг на середине | HTTP-таймаут от длины чанка (не записи) → retry |
| Repetition-цикл в чанке | >50 % идентичных сегментов → `suspect` в манифесте; regenerate пересжимает suspect c `initial_prompt=""` (+hook под новый Speaches) |
| Диск | чанки живут в `meta/chunks/` (та же квота, что артефакты); чистятся после `merge_speakers` (retention until_merged) |

## Осознанные отказа

- БЕЗ VAD-нарезки по тишине (сложно, overlap 2 с достаточно для стыков).
- БЕЗ параллельной транскрипции чанков.
- БЕЗ изменений клиентского `ApiTranscriber` под чанкинг (стадия сама всё делает; ApiTranscriber остаётся «один файл → один POST» примитивом).
- БЕЗ правок платформенного voice-стека / версии Speaches (пользователь сам).
- Спикер-лейблы per-chunk не склеиваются глобально (принято, merge_speakers не пострадает).

## Порядок работ

1. `worker/chunk.py` + `test_chunk.py` (чистая нарезка, без docker).
2. Активити `chunk` + Enum/DB-миграция (api+worker) + workflow-порядок.
3. `transcribe`/`diarize` по чанкам + смещения + retry-per-chunk + тесты.
4. Suspect-detection + cleanup chunks после merge.
5. e2e-smoke с чанкингом + доки + Serena memory.

Гейты перед отчётом (канон репо): `cd server/worker && uv run ruff check worker tests && uv run pytest -q && uv run pyright worker` → `cd server && docker compose build worker && docker compose up -d worker` → e2e-smoke.

## Текущее состояние стека (на момент handoff)

- Dev-стек вверху: api/postgres/temporal/temporal-ui/worker Up, worker на очереди `transcripter-pipeline`.
- Платформенный voice-стек: `transcription-speaches` (0.8.3-cpu, :8010, API_KEY в `platform/transcription/.env`), `transcription-linto` (:8081), gigastt, transcriber (не используется transcripter'ом).
- Запись `d19c828a…` — state done, но транскрипт после 01:01 мусорный (repetition loop); её стоит **перегенерировать после внедрения чанкинга** как первый боевой тест: `curl -X POST -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"stage":"chunk"}' http://localhost:8090/recordings/d19c828a-33ae-40b9-a858-612ee3d0e09f/regenerate`.
- Токен: `server/.env` → `TRANSCRIPTER_TOKEN` (сейчас `test-token-e2e`).
- Известная проблема среды: субагенты в этом harness периодически зависают (правило: 2 проверки без роста транскрипта → cancel и делать самому; max ожидание 8–10 мин).
