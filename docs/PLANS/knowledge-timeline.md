# План: память и таймлайн (TTRPG + рабочие созвоны): типы, свободные теги, граф

Статус: план утверждён 2026-08-28, пересмотрен там же после ревизии. Ключевые
решения (модель тип/тег, английские системные имена, BERT-хостинг, digest
триггер, enrich_all) — в секции «Решения». Порядок фаз = порядок исполнения.

Цель: у записи есть **тип** (системный пресет — маршрутизация конвейера) и
**свободные теги** (группировка, язык любой). Память/таймлайн живут в
пространстве тега; следующая сессия получает рекап предыдущей.

---

## Контекст (что уже есть, фиксация фактов)

- `enrich` — best-effort стадия после `summarize` (`worker/workflows.py:80`):
  профиль с секцией `enrich:` → LLM (json_object, 3 попытки) →
  `{events: [{ts, kind, summary}], entities: [{slug, label, type}],
  relations: [{from_slug, to_slug, type}]}` → Neo4j.
- Идемпотентность: `DETACH DELETE` по `origin_recording_id` → `MERGE (tag, slug)`.
- Дедуп: slug-нормализация (Unicode) + LLM «тот же объект? Y/N» (30s бюджет,
  ошибка → «да»). Повторяемость сущности = `recording_ids` на узле.
- `ts` событий — смещение внутри записи, НЕ абсолютное время. Дат/названий
  сессий в графе нет — только UUID (`origin_recording_id`).
- Digest (волна C): `POST /tags/{tag}/digest {last_n}` → `<transcripts>/digests/<tag>.md`.
  LLM получает голые UUID вместо дат/заголовков → межсессионная хронология
  невозможна. В клиенте digest не виден.
- MENTIONS строится регэкспом word-boundary по лейблу — русская морфология
  («Галахад»/«Галахада») теряет рёбра.
- Профили матчятся по пересечению тегов (`match_profile`, worker/profiles.py).
  Теги записи сейчас выполняют ОБЕ роли: маршрутизация и группировка — это и
  меняем.
- `ARTIFACTS["enrich"] = []` (api/routes/regenerate.py) — файловых артефактов
  у стадии нет; events.json надо будет вайрить сюда.
- Удаление записи НЕ чистит граф: `delete_recording` (routes/recordings.py)
  удаляет строку Postgres + rmtree каталога; Neo4j-узлы остаются навсегда →
  дедуп «видит призраков» удалённых сессий.
- Digest-API регекс тега `^[a-z0-9][a-z0-9._-]{0,63}$` (близнец в
  worker/digest.py `_SAFE_TAG_RE`) отвергает пробелы и кириллицу — свободные
  теги в этой схеме невозможны в принципе.
- Живые теги в проде (2026-08-28): `pathfinder` (14), `e2e` (12), `mobile` (1)
  — все ASCII; миграция тривиальна.
- Живой Neo4j: 5.26.30 (5.26-community, LTS, pinned). `CREATE VECTOR INDEX`
  проверен живой пробой create/list/drop 2026-08-28 — работает из коробки,
  конфигов не требует (GA с 5.13). HNSW доступен для фазы 2.5.

---

## Решения (зафиксировано 2026-08-28)

1. **Модель тип/тег (главная).** Тип — системный, английский, из пресетов
   (профили), маршрутизирует конвейер. Теги — свободные, любой язык, чистая
   группировка; память/таймлайн/дайджест — в пространстве тега. Неймспейсы
   графа = ВСЕ теги записи (копии; теги `[doctronic, personal]` → запись
   видна в обоих, дедуп в каждом свой). Нет тегов → `untagged`. Тип НЕ
   является неймспейсом (иначе все нетегированные созвоны свалились бы в
   одну кучу).
2. **Системные имена — только английские** (решение пользователя): типы, id
   профилей, kinds/types сущностей, дефолты, имена артефактов. Контент
   (промпты, саммари, лейблы сущностей, СВОБОДНЫЕ теги) — любой язык.
3. **Digest-триггер: авто + вручную.** После done-записи (Temporal-сигнал) c
   dedupe-окном на неймспейс (не чаще раза в N часов) + кнопка в Vault.
4. **enrich_all: да, по умолчанию on** (после фазы 2). Нетипизированные и
   нетегированные записи — во встроенный неймспейс `untagged`.
5. **Хостинг BERT: фаза 2.5, ONNX int8 в worker** (bge-m3). Отдельный
   контейнер не заводим, пока worker не упрётся в RAM.
6. **Артефакты-first для чтения.** Per-recording view — из `meta/events.json`;
   per-tag timeline — из артефактов + Postgres (агрегация по API), Neo4j не
   дёргается на каждый запрос. Neo4j-чтение из api — только когда станет тесно.
7. **Импорт файлов: one-shot `/recordings/direct` + бэкидейт.** Форматы
   flac/wav/mp3 (всё, что жуёт ffmpeg); пикер — нативный file input в
   webview; `recorded_at` опционален (NULL → coalesce с created_at).
   Многочасовой WAV идёт одной multipart-загрузкой — как мобильные
   capture; лимитов размера в API нет (диск-гвард 507 до записи на диск).
8. **UI-контракт не ломаем: Import — это та же «порция» контента.** Import
   не отдельный экран, а кнопка на Recorder (рядом со Start) + форма меты;
   мобильный импорт — тот же компонент через drawer.
9. **Digest — по каждому тегу независимо (мульти-тег семантика).** Запись
   с тегами `[doctronic, personal]` попадает в digest И doctronic, И
   personal. SQL не меняется: `tags.contains([tag])` уже фильтрует «тег
   в массиве» независимо от позиции; `_fetch_graph_slice` фильтрует по
   одному `$tag` и вызывается на каждый namespace копий отдельно. Сортировка
   `_select_recordings` → `coalesce(recorded_at, created_at)` — фаза 1.

Открытые (решать при исполнении): recap-источник (digest vs ретрив);
размещение digest-заметок в Obsidian-структуре; живость нативного
`<input type=file>` в Android WebView (проверить на устройстве;
fallback — tauri-plugin-dialog).

---

## Фаза 0 — фундамент: тип/тег split (1–1.5 дня)

Всё остальное стоит на этом. Порядок внутри: схема → профили → worker → API →
клиент.

### 0.1 Схема БД

- `recordings.type: TEXT NULL` — новый столбец (idempotent ALTER по образцу
  миграций STAGE_KINDS). Значение — slug типа: `meeting`, `ttrpg`, …
- `recordings.recorded_at: DATETIME NULL` — «когда реально звучала запись»
  (импорт старых файлов: бэкидейт). NULL = не указано; отображение клиентом
  через `coalesce(recorded_at, created_at)`; сортировки/таймлайны — по нему же.
- Бэкфилл существующих: type = матч профиля по текущим тегам (pathfinder →
  `ttrpg`, meeting/совещание → `meeting`, прочее → NULL). 27 строк, one-off.
- `tags` остаются TEXT[] — теперь только пользовательская группировка.

### 0.2 Профили

- В yaml профиля `type: meeting` ЗАМЕНЯЕТ `tags:` (чистый cutover, личный
  репозиторий; `tags:` в файле → warn+skip профиля, как невалидный).
  `min_host_version` обоих профилей → следующий хост (0.10.0).
- `meeting-notes.yaml`: `type: meeting` + **enrich-секция** (бывший блокер
  фазы 0 старого плана): типы `person|project|team|decision|action_item|
  deadline|system`, kind событий `decision|action|deadline|discussion|risk|
  info`, relations `owner_of|works_on|depends_on|decides|assigned_to`.
- `pathfinder-party-log.yaml`: `type: ttrpg`, enrich остаётся как есть.
- `match_profile(tags)` → `match_profile_by_type(rec_type)`; запись без типа
  → дефолтное поведение (встроенный summarize-промпт; после enrich_all —
  fallback-enrich).

### 0.3 Worker

- `enrich`/`summarize` активности: профиль по `recording.type`.
- Неймспейсы графа (`graph_tags`): все свободные теги записи (копии
  сущностей в каждый; теги `[doctronic, personal]` → обе группы). Нет
  тегов → `["untagged"]`.
- **Regenerate при правках меты** (сегодня PATCH тегов дергает только
  `start_export` — enrich НЕ перезапускается; чиним): смена тегов или
  типа у done-записи → fire-and-forget `regenerate_stage(id, "enrich")`
  (+ summarize при смене типа) + полный export. `DETACH DELETE` по
  `origin_recording_id` чистит ВСЕ старые неймспейсы атомарно, затем
  пишутся копии в новые — рассинхрона нет.
- Значения `tag` в Neo4j теперь могут содержать пробелы/кириллицу — это
  property-значения (параметризованные), не лейблы; безопасно.

### 0.4 API

- `POST /recordings/direct` + путь chunked: поле `type` (опционально; валидация
  slug `^[a-z0-9][a-z0-9-]{0,31}$`, неизвестный тип — храним как есть,
  конвейер просто не сматчит профиль; 400 только на мусор). **Импорт-поля**:
  `recorded_at` (ISO-8601, опционально; бэкидейт «созвон был вчера») и
  `title` уже есть. `duration_sec` от клиента НЕ нужен — worker пробивает
  длину сам (`probe_duration`).
- `PATCH /recordings/{id}`: поля `type` и `recorded_at` добавляются к
  title/tags; смена `type` у done-записи → regenerate summarize+enrich
  (см. 0.3), смена тегов → regenerate enrich, смена `recorded_at` →
  только `start_export` (frontmatter). Body: минимум одно поле, как сегодня.
- `GET /profiles`: поле `type` в ответе (клиент строит селектор типов).
- `GET /tags`: distinct-теги со счётчиками (unnest+group by) — источник
  для свободных тегов (сейчас подсказки = теги профилей, это убираем).
- **Digest tag regex — точный паттерн**: `^[\w][ \w.-]{0,63}$` c
  `re.UNICODE` (`\w` = буквы/цифры/подчёркивание любой раскладки; первый
  символ — не пробел; внутри — пробел/дефис/точка; ≤64). Точный близнец
  в worker/digest.py — менять СИНХРОННО. Имя файла = Unicode-aware
  slugify из enrich (уже держит кириллицу через `\w`); коллизия слагов →
  суффикс `-2`/hash (паттерн `_disambiguate` из export.py). Frontmatter
  digest: поле `tag:` = display-тег, как ввёл пользователь.
- `409` на digest без графа, `400` на пустой тег — как сейчас.

### 0.5 Клиент

- **Recorder**: селектор ТИПА (из GET /profiles: display_name; «None» =
  дефолт) + TagChips свободных тегов с подсказками из GET /tags (недавние
  теги). Перед стартом — хинт сматченного профиля (display_name + значок
  памяти). Мобильный путь (uploadDirect) — то же поле `type`.
- **Импорт файлов** (новая поверхность): кнопка «Import audio» рядом со
  Start — `<input type="file" accept=".flac,.wav,.mp3,audio/*">`; нативный
  пикер работает в Tauri v2 webview на всех платформах (Android — system
  chooser; если облом — fallback `tauri-plugin-dialog`). Форма меты:
  название / дата+время (`datetime-local`, предзаполнено сейчас) / тип /
  теги → `uploadDirect(file, {title, recordedAt, type, tags})`. Сервер
  транскодирует и запускает стандартный конвейер. Один файл за раз; прогон
  — indeterminate spin; ошибки — inline.
- **Размер импорта**: жёсткого лимита в API НЕТ (проверено живым
  контейнером: uvicorn 0.52.4 h11 без body-cap; Starlette multipart
  spool'ит file-part на диск после 1MB RAM). Клиентский soft-гвард:
  при >500 MB — inline-подсказка «перекодируй в FLAC/MP3, иначе долго»
  (ffmpeg-таймаут 600s — реальный потолок транскода, не память);
  БЛОКА нет — грузится всё.
- **Detail**: бейдж типа у заголовка; PATCH тегов уже есть, добавить тип
  (+ редактирование `recorded_at`, то же поле).

### 0.6 Верификация фазы 0 — ВЫПОЛНЕНО (2026-08-29, коммит 7eee78e)

- uv pytest: api 138 passed, worker 234 passed + 4 skipped; ruff/pyright
  clean; pnpm check 0/0, pnpm build ok.
- Live (prod-стек): миграция + бэкфилл 14 pathfinder → ttrpg; upload с
  `type=meeting`, тегами `e2e phase 0` + `проба кириллица` и бэкидейтом →
  конвейер done; PATCH `type=meeting` на реальной 82-мин записи → enrich
  66 сущностей / 34 связи профилем meeting-notes в ОБА неймспейса
  (`daily blob`, `проба`); digest: слаг-файлы `e2e-phase-0.md` +
  `проба-кириллица.md`, frontmatter `tag:` verbatim, мусорный тег 400.
- Найдено попутно: записи, созданные до появления enrich, не имеют
  enrich-стадии — regenerate её создаёт не во всех путях (backfill
  вручную через regenerate enrich; кандидат в Phase 1 GC-обход).

---

## Фаза 1 — граф как самоценный таймлайн-стор (полдня)

1. **`recording_date` + `recording_title` на Event-узлах** — `write_to_graph`
   читает из Postgres (паттерн digest `_select_recordings`).
2. **Digest-промпт с датами и названиями**: `title (YYYY-MM-DD) [kind @ ts]
   summary` вместо UUID-строк (`DigestRow` уже несёт title/created_at).
3. **`meta/events.json` артефакт**: enrich дополнительно пишет результат
   извлечения файлом; `ARTIFACTS["enrich"] = ["meta/events.json"]`.
   Regenerate enrich → файл перезаписывается. База для таба Events и
   per-tag timeline без Neo4j-чтения.
4. **Graph GC sweep**: периодическая активность (Temporal schedule):
   `origin_recording_id` в графе MINUS id в Postgres → DETACH DELETE.
   Самоисцеление прошлых «призраков», без связки delete→worker.
   **Initial sweep**: первый запуск активности чистит уже накопленных
   призраков (delete без чистки работал с самого появления enrich) — те же
   MINUS + DETACH DELETE, просто первый прогон не пустой. Отдельного
   инструмента не нужно: расписание само схлопывает backlog.
5. **Digest UI (сдвиг сужен 2026-08-29)**: per-tag digest кнопки +
   просмотр на СТРАНИЦЕ ЗАПИСИ (Detail, recess + Markdown.svelte,
   регенерация с поллингом) + `GET /tags/{tag}/digest` (чтение заметки
   по frontmatter-матчу). Полноценный «Vault»-экран с манифестом тегов —
   фаза 3 (там же Timeline/Entities табы).
6. **`untagged`-digest gap**: `_select_recordings('untagged')` с
   `tags.contains(['untagged'])` не матчит пустые `tags = []`. Спец-ветка:
   `untagged` → `array_length(tags,1) IS NULL` (pg) / `json_array_length
   (tags) = 0` (sqlite). Иначе digest по untagged навсегда пуст.

Статус (2026-08-29): пункты 1–6 реализованы; гейты — api 149 passed /
worker 244 passed, ruff+pyright clean, pnpm check 0/0 + build. GC
включается `graph.gc_interval_sec` (по умолчанию 0 = off; в прод
выставить, напр. 3600). Live-проверка — ниже по ходу работ.

## Фаза 2 — активная память (1–2 дня)

1. **`{known_entities}` в extraction-промпт**: топ-N сущностей неймспейса
   (`ExistingEntityLookup`) опциональным плейсхолдером — «известны,
   переиспользуй slug». Консистентность + почти исчезают Y/N-вызовы.
   Профиль: `enrich.known_entities: true|N`.
2. **Model-declared mentions**: опциональное `entities: [slug...]` в
   event-объекте wire shape; регэксп — фолбэк. Обратно совместимо.
3. **enrich_all=true default** + встроенный fallback-промпт; неймспейс по
   правилу фазы 0.3.
4. **Авто-digest** (решение 3): Temporal-сигнал после done + dedupe-окно на
   неймспейс + кнопка.

Статус (2026-08-29, коммит b2e617f): пункты 1–4 реализованы и проверены
живьём. Отклонение от «Temporal-сигнал»: авто-digest запускается инлайн в
enrich-активности после успешной записи графа (окно свежести по mtime
digest-файла; без сигнальной инфраструктуры, с сохранением порядка
«граф → digest»). Попутно починен баг write_digest: регенерация теперь
перезаписывает существующий файл тега in-place (frontmatter-матч), а не
плодит -2/-3 копии. Гейты: worker 288 passed + 4 skipped, ruff/pyright
clean.

## Фаза 2.5 — BERT-префильтр дедупа (1 день)

bge-m3 ONNX int8 в worker: cosine ≥ τ_high → авто-мердж; τ_low..τ_high →
LLM Y/N (только серая зона); < τ_low → новая сущность. Векторы — `embedding`
property + HNSW vector index (проверен на живом 5.26.30). Ловит морфологию,
(pathfinder-сущности уже есть).

Статус (2026-08-29, коммит 33eb562): реализовано и проверено живьём —
bge-m3 int8 ONNX в /models volume, HNSW-индекс embedding_bge_m3
(1024d cosine) ONLINE, трёхзонный гейт в resolve_slugs, эмбеддинги
пишутся на CREATE узла. Калибровка на живых данных: ru↔en 0.93,
варианты регистра 0.85–0.95, морфология 0.6–0.8 (серая зона → LLM —
консервативно верно), разные сущности ≤0.55. Дефолты 0.90/0.75;
tau_high, возможно, снизить до 0.85 после наблюдений. Гейты: worker
322 passed + 4 skipped.

## Фаза 3 — рекап и UI таймлайна (2–3 дня)

1. **Recap**: перед summarize подмешивать digest неймспейса / last-seen
   события сущностей («на прошлой сессии…»). Гейт `summarize.recap: true`.
   Бюджет промпта — LiteLLM 2400s потолок.
2. **`GET /tags/{tag}/timeline`**: события по `recording_date` desc + сессии
   + сущности. Реализация: агрегация events.json артефактов + Postgres
   (решение 6), НЕ живой Neo4j из api.
3. **Клиент: страница тега** (см. UI).
4. **Эволюция сущностей**: kind=state_change → «сущность сейчас» в digest.

## Фаза 3.5 — семантический поиск (после)

Эмбеддинги сегментов (та же bge-m3) → sqlite-vec рядом с events.json или
векторный индекс Neo4j → «когда мы обсуждали X» по неймспейсу. База для
recap-ретрива.

---

## BERT-модели: все идеи (с приоритетом)

Кандидаты: bge-m3 (100+ языков, dense+sparse+colbert, 8k токенов),
multilingual-e5-large (560M), LaBSE, GigaEmbeddings (SOTA ruMTEB 69.1),
bge-reranker-v2-m3 (cross-encoder).

| # | Идея | Модель | Приоритет |
|---|------|--------|-----------|
| 1 | Дедуп сущностей: эмбеддинг-префильтр | bge-m3 ONNX int8 | **высокий** — фаза 2.5 |
| 2 | Семантический поиск по сессиям неймспейса | та же | **высокий** — фаза 3.5 |
| 3 | Тематическая сегментация (главы длинной сессии) | sentence embeddings + change-point | средний |
| 4 | Кросс-энкодер для серой зоны дедупа | bge-reranker-v2-m3 | средний |
| 5 | Автотегирование/классификация типа записи | ruBERT/XML-R | средний |
| 6 | Детекция action items из сырых сегментов | fine-tuned token cls | низкий |
| 7 | Спикер-рекластеризация | sentence embeddings | низкий (pyannote есть) |

Куда НЕ ставить BERT: ASR (Speaches), базовая диаризация (pyannote/LinTO),
суммаризация (LLM).

---

## UI-план (desktop 440×720 + Android 411×914)

Контракт: DESIGN_GUIDELINES.md — plates/seams/recesses, cyan только
verified/focus, brass для контролов, красный только запись/опасность, один
компонент-три, расхождения только в shell-слое.

### Поверхности

1. **Recorder**: селектор типа (системные пресеты из /profiles; «None» =
   дефолтный конвейер) над свободными тегами. TagChips с подсказками
   недавних свободных тегов (GET /tags). Хинт сматченного профиля.
2. **«Vault» — назначение навигации** (Recorder / Recordings / Vault /
   Settings). Манифест СВОБОДНЫХ тегов: строки через seam — тег, тип-eyebrow
   (meeting/ttrpg/—), число сессий, сущностей, последняя активность, лампа
   digest (готов/устарел/нет). Записи без тегов группируются по типу.
3. **Страница тега**: Timeline / Entities / Digest табы:
   - Timeline: сессии по датам, раскрытие = события (`kind @ ts summary`),
     клик по событию → запись + seek аудио на ts.
   - Entities: label/type/last seen/session count; раскрытие = упоминания.
   - Digest: recess markdown + регенерация.
4. **Запись: таб Events** — из events.json, click-to-seek; entity-чипы.
5. **Recap-индикатор** в Summary табе («Memory applied: 3 prior sessions»).

### Потоки

**A. Рабочий созвон.** Recorder → тип «Meeting» → тег `doctronic` (подсказка
из недавних) → Start → звонок → Stop. Конвейер: протокол + entities
(person/project/decision/action_item) в неймспейсе `doctronic`. Открыл
запись: Events «00:42:13 · decision · релиз переносим» — клик → seek 42:13.
Vault → doctronic → Timeline сессий по датам; Digest — открытые action
items. Следующий созвон doctronic — с рекапом.

**B. TTRPG.** Тип «TTRPG» → тег `dnd dark castle` (новая кампания = новый
тег, её сущности изолированы от `pathfinder with gimli` — неймспейсы не
пересекаются). 4 часа → персонажи/NPC/лут/локации. Vault → dnd dark castle
→ хроника кампании; Digest = летопись в Obsidian. Следующая сессия — рекап
«партия покинула склеп».

**C. Разбор массива.** Vault → тег → межсессионная хронология; сущность →
все упоминания → seek в аудио; rolling digest. (3.5) поиск «когда обсуждали
артефакт X» по кампании.

**D. Импорт.** Пятница, созвон записан «на стороне» (телефонным диктофоном).
Recorder → «Import audio» → пикер (flac/wav/mp3) → форма: название
«Doctronic weekly», дата вчера 14:00, тип Meeting, тег doctronic → Upload.
Сервер: транскод → конвейер → events/digest в неймспейсе doctronic,
дата в таймлайне — вчерашняя. Через полчаса в Vault у doctronic новая
сессия «вчера 14:00» на своём месте в хронологии.

### Мобильное

Те же поверхности через nav drawer; `.shell--android` оверрайды, без форков.
Запись Android — getUserMedia → /recordings/direct (+поля type, recorded_at);
импорт — тот же Import-компонент (нативный picker через `<input type=file>`).

---

## Риски / гочи (из прожитого)

- Промпт-шаблоны — только литеральный `.replace()`, не `str.format()`.
- Новая активность → в `ACTIVITIES` в `worker/main.py` (тест паритета).
- f-string рядом с Cypher: одиночный `}` ловили в `event_query`.
- `tags ARRAY` фильтры — `postgresql.ARRAY + with_variant(JSON)`; sqlite-тесты
  не видят (есть env-gated pg-тест).
- `{known_entities}` при пустом графе — подставлять «(none)».
- Два regex тега (api tags.py / worker digest.py) — менять СИНХРОННО.
- Бюджеты LLM: httpx 2370s / Temporal 2400s конверт.
- Unicode slugify уже в enrich (`\w` держит кириллицу) — переиспользовать,
  не писать второй.

## Порядок работ

1. Фаза 0 (тип/тег split: схема → профили → worker → API → клиент;
   enrich в meeting-notes входит сюда) → e2e GRAPH=1 с типом+тегом с
   пробелом.
2. Фаза 1 (даты/title на события, digest-рендер, events.json + ARTIFACTS,
   graph GC sweep, digest UI).
3. Фаза 2 (known_entities, model-declared mentions, enrich_all, авто-digest).
4. Фаза 2.5 (BERT-дедуп) параллельно с UI Vault/timeline.
5. Фаза 3 (recap, timeline API, эволюция сущностей) + 3.5 (поиск).
