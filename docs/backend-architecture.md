# Backend architecture — external STT + diarization

Architecture of the server stack when transcription and diarization are served
by **third-party/external services** instead of the bundled ML containers
(compose profiles `stt` / `diarization` not started). This is a supported
configuration: `transcribe.backend=api` + an external `diarization.endpoint`
(see «ML deployment matrix» in the root README).

```mermaid
flowchart TB
    subgraph CLIENT["🖥 Tauri-клиент (macOS / Windows)"]
        C["Recorder: capture → FLAC → spool"]
    end

    subgraph STACK["docker compose `transcripter` (без ML-контейнеров)"]
        direction TB
        API["<b>api</b> · FastAPI :8090<br/>bearer TRANSCRIPTER_TOKEN<br/>POST /recordings → offset-PUT → finalize(sha256)"]
        PG[("postgres :5432<br/>recordings + stage-строки")]
        T["temporal :7233<br/>(auto-setup)"]
        UI["temporal-ui :8082"]
        W["<b>worker</b> · Temporal Worker<br/>queue: transcripter-pipeline"]
        ST[("/storage/recordings/&lt;id&gt;/<br/>audio.flac + meta/ артефакты")]
        TR[("/transcripts<br/>bind → host-каталог (Obsidian)")]
    end

    subgraph VOICE["🔇 Внешний voice-стек (другой хост / отдельный compose)"]
        STT["STT · OpenAI-compatible<br/>POST {base_url}/audio/transcriptions<br/>verbose_json + word-timestamps<br/>(Speaches / Groq / OpenAI)"]
        DIAR["Diarization · LinTO<br/>POST {endpoint}/diarization<br/>multipart → segments+spk_id"]
    end

    LLM["LLM summarize (optional)<br/>OpenAI-compatible, напр. LiteLLM"]

    C -- "1. resumable upload + finalize" --> API
    API -- "2. метаданные, стадии" --> PG
    API -- "3. start ProcessRecording" --> T
    T -- "4. задачи" --> W
    UI -.-> T
    W -- "5. stage-статусы" --> PG
    W -- "6. чтение audio.flac" --> ST

    subgraph PIPE["ProcessRecording: transcribe → diarize → merge_speakers → summarize → finalize → export"]
        direction LR
        p1[transcribe] --> p2[diarize] --> p3[merge_speakers] --> p4[summarize]
    end
    W ~~~ PIPE

    W -- "7. audio.flac" --> STT
    W -- "8. audio.flac" --> DIAR
    W -- "9. transcript (optional)" --> LLM
    W -- "10. consolidated .md (subprocess)" --> TR
```

## Поток данных

1. **Загрузка.** Клиент грузит FLAC только до API (resumable offset-PUT чанков
   ≤16 МБ + finalize с SHA-256). Внешние STT/diarization-сервисы никогда не
   видят клиента — worker сам отправляет им файл с диска (`/storage`).
2. **Запуск пайплайна.** API после `finalize` стартует workflow
   `ProcessRecording` в Temporal и дальше только отдаёт статусы стадий из
   Postgres.
3. **Стадии.** Worker выполняет активности по порядку:
   `transcribe → diarize → merge_speakers → summarize`, затем всегда
   `finalize_recording` (ставит терминальный статус записи) и best-effort
   `export_transcript` (консолидированная заметка .md в `/transcripts`).
4. **Внешние вызовы.** `transcribe` при `backend=api` делает
   `POST {base_url}/audio/transcriptions` (multipart, `verbose_json`,
   `timestamp_granularities[]=word` — word-timestamps обязательны, по ним
   работает merge). `diarize` шлёт файл на `POST {endpoint}/diarization`
   (LinTO-формат `seg_begin/seg_end/spk_id` транслируется в
   `start/end/speaker` на этой границе).
5. **Regenerate.** `POST /recordings/{id}/regenerate {stage}` перезапускает
   workflow с выбранной стадии; все нижестоящие стадии выполняются заново.

## Конфигурация внешнего режима

`server/config.yaml` + `.env`:

```yaml
transcribe:
  backend: api
  model: Systran/faster-whisper-small   # полный HF-id; пустые имена 404
  base_url: http://<stt-host>:8000/v1   # суффикс /v1 ОБЯЗАТЕЛЕН
  api_key_env: SPEACHES_API_KEY         # ИМЯ env-переменной, не сам ключ

diarization:
  enabled: true
  endpoint: http://<diar-host>:80       # или env DIARIZATION_ENDPOINT
```

- Env перекрывает yaml: `DIARIZATION_ENDPOINT` (endpoint), `SPEACHES_API_KEY`
  (значение ключа), `TRANSCRIPTER_TOKEN`, `TRANSCRIPTS_DIR`.
- Worker читает конфиг один раз при старте → после изменений
  `docker compose restart worker`; смена env из `.env` требует
  `docker compose up -d worker`.
- При `backend=api` worker не прелоадит whisper → volume `models` не нужен,
  контейнер лёгкий, CPU-only.

## Отличия от bundled-режима

- Профили `stt` / `diarization` не поднимаются; порты 8070 (LinTO) и 8000
  (Speaches) в этом стеке не публикуются.
- ML-сервисы живут где угодно: другой хост в LAN, отдельный compose-стек,
  облачный API. Требования к STT — OpenAI-совместимость + word-timestamps;
  к diarization — HTTP-API LinTO.
- Оба стека на одном docker-хосте могут делить внешнюю сеть `voice`
  (`docker network create voice`, раскомментировать блок `networks:` в
  `docker-compose.yml`) и адресоваться по DNS-именам сервисов.

## Отказоустойчивость (workflows.py)

- **transcribe** — retry ×2, `StartToCloseTimeout` растёт от длительности
  аудио (`timeout_for`: base + 30·мин + холодный старт 120 с).
- **diarize** — best-effort: retry ×4 c интервалом 30–60 с (поглощает
  холодный старт внешнего сервиса); при исчерпании ретраев запись остаётся
  годной — transcript-only, стадия помечается `failed` + `last_error` для UI.
- **export** — изолированный subprocess (20 с kill-and-abandon, ≤4 живых
  дочерних процесса), всегда best-effort; восстановление — `worker.backfill`.
- Сторонний сервис, лежащий дольше ретраев, валит только свою стадию, а не
  весь стек. `finalize_recording` ставит терминальный статус записи даже при
  упавшей стадии.
