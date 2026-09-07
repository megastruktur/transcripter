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
  finalize → spool cleaned after ack. An upload abandoned before finalize
  (client crash/kill) is marked `failed` by the api reaper after
  `upload_ttl_hours` (default 24) without row activity — `uploading` never
  wedges forever; `failed` rows are deletable from the client.
- Regenerate: `POST /recordings/{id}/regenerate {stage}` — downstream stages
  always re-run. The API resets the target stage and every downstream one to
  `pending` (errors cleared) in the same commit that backfills stage rows,
  BEFORE the workflow starts — the client's stage icons show the honest
  pipeline, never stale `done` for stages about to be recomputed. The PATCH
  auto-regen path (tags/type change on a done recording) applies the same
  reset.
- Archive list: `GET /recordings` is paginated and filtered server-side —
  `?limit=&offset=&q=&state=` returns `{items, total, limit, offset}`; the
  client library pages at 20 rows and sends its search box / state filter
  through `q` / `state`.
- Summarize: enabled via the platform LiteLLM proxy (OpenAI-compatible) —
  local `qwen3.8-27b-q4_k_m` at `http://192.168.3.23:4000/v1`, key from the
  `LITELLM_API_KEY` env var. The stage still self-skips when unconfigured.
- Vault export (2026-08-31): when `VAULT_DIR` is set in `.env` (replaces
  `TRANSCRIPTS_DIR`, still accepted as a deprecated alias), every finished
  recording exports as a self-contained folder
  `{vault}/YYYY/MM/{YYYY-MM-DD_HH-MM} {title} {id8}/` (grouped by
  coalesce(recorded_at, created_at)): meta artifacts 1:1 + hidden
  `.transcripter/` with `audio.flac` (MOVED out of /storage after the
  pipeline: copy → sha256-verify vs catalog → atomic rename → unlink
  storage) and `manifest.json` (id/sha256/dates/title/tags/type — future
  import base). `Dashboard.md` (months + per-tag MOC, wikilinks)
  regenerates at the vault root after every export. `/recordings/{id}/audio`
  and transcribe/diarize regenerates read storage first, vault second.
  DELETE removes catalog row + storage dir + vault folder (id8-scan:
  nested + legacy flat). Pre-vault layouts migrate on next export/backfill
  (id8 folder scan; flat `* {id8}.md` notes deleted). No vault →
  `./storage/transcripts` flat layout, audio stays in /storage. Export
  subprocess timeout 120 s (audio move budget); rename moves the folder
  without rewriting files (Obsidian edits survive).
- Pre-flight opens and probes every selected source before recording. A selected
  system source that cannot start blocks recording; microphone-only capture is
  available only when System audio is explicitly Off.
- Ports: api 8090, temporal-ui 8082, diarization 8070 (host port conflicts).
- Tags & profiles (2026-08-27): recordings carry normalized tags (`TEXT[]` on
  Postgres, trim+lowercase+dedupe) set at upload init or via
  `PATCH /recordings/{id}`; `?q=` matches title and tags. Yaml profiles in
  `server/profiles/` (bind-mounted at `/etc/transcripter/profiles`, re-scanned
  per stage run — no restart) override the summarize prompt and rename the
  exported summary artifact (`output_artifact`, default `summary.md`;
  `meta/summary.md` stays canonical). Format contract:
  `server/profiles/README.md`. Broken profiles warn+skip; the pipeline is
  never affected. Tag edits apply on the next summarize regenerate.
- Knowledge graph (2026-08-28): optional `enrich` stage after summarize —
  profiles with an `enrich:` section extract {events, entities, relations}
  via the summarize LLM (json_object, in-activity retries) into Neo4j CE
  (compose profile `graph`, no published ports, mem_limit 1.5g). Writes are
  idempotent: DETACH DELETE by origin_recording_id → MERGE by (tag, slug);
  dedup is slug-normalization + a best-effort LLM same-entity question.
  enrich is best-effort (skipped without graph/profile, failed never fails
  the recording). Profile prompts substitute ONLY {title}/{transcript}
  literally — JSON schema braces in prompts are safe.
  `server/scripts/e2e_smoke.sh` GRAPH=1 verifies the write path through a
  deterministic in-container probe (scripts/graph_probe.py) plus a live
  enrich regenerate cycle.
- Tag digests (2026-08-28): `POST /tags/{tag}/digest` (body optional since
  2026-09-04 — a bare POST from the client must 202, not 422; `last_n`
  default 5, `1..50`) → 202 + workflow_id; Temporal TagDigest workflow
  picks the last N done recordings with the tag (Postgres `@>`), pulls
  their subgraph (Neo4j), renders an Obsidian note to
  `<transcripts>/digests/<tag>.md` (tmp+rename, frontmatter with
  recording ids). 409 when the graph layer is off; 400 on
  non-filesystem-safe tags. Language (2026-09-04): the digest is written
  in the dominant STT-detected language of the selection (transcribe
  stage details; explicit "Write the ENTIRE digest in …" directive;
  majority language wins; soft fallback when no language is known).
- Android client PoC (2026-08-28, verdict GO): Tauri v2 Android builds
  user-local (no sudo): Temurin 21 + SDK/NDK 26.1.10909125, repro in
  `client/ANDROID_POC.md`. Desktop-only Rust is cfg-gated; capture on Android
  is JS: getUserMedia → MediaRecorder (webm/opus) → one-shot
  `POST /recordings/direct` (multipart; server transcodes to canonical FLAC
  unless the bytes already are FLAC). Runtime mic check on a physical device
  is pending; backgrounding tears down the MediaStream (foreground service
  is the known future fix).

## Tag registry + vocabularies (2026-09-04)

- Tags are a first-class registry entity (`tag_defs` table): a tag can be
  created on the Tags page BEFORE any recording carries it
  (`POST /tags {name, vocabulary?}`). `GET /tags/{tag}`, `PATCH /tags/{tag}`
  (full-list vocabulary replace; upserts legacy tags), `DELETE /tags/{tag}`
  (registry row only; 409 while recordings carry the tag — tag memory has
  its own purge path).
- Recordings auto-register their tags into the registry on create/direct/
  PATCH (`INSERT ... ON CONFLICT DO NOTHING`): the registry stays a
  superset of tags seen on recordings, and `GET /tags` unions both
  (registered tags with zero recordings list with `count: 0`; every row
  carries `registered` + `vocabulary_count`).
- Per-tag vocabulary (hot words, ≤200 entries × 64 chars, casefold-dedup,
  casing preserved) flows into the pipeline: transcribe injects it as the
  ASR `prompt` (whisper initial_prompt, ~900-char cap, whole-word
  truncation; survives the suspect-chunk decoder reset — it is domain
  bias, not decoder state) on every chunk and stereo channel; summarize
  appends it as a glossary block in the system message (same rail as the
  recap block). Applies on the next transcribe/summarize run — no
  retroactive regeneration.
- Client (merged 2026-09-07): the Tags rail item and `/tags` pages are GONE —
  the Vault is the single tag surface. `/vault` carries the register form and
  one ruled row per tag (sessions, entities, vocabulary count; registry-only
  tags show "registered · no sessions yet"), and `GET /vault` unions
  `tag_defs` rows in as 0-session items with `vocabulary_count`. The tag
  page's first tab is **Definition** (vocabulary editor with immediate-PATCH
  autosave, context textarea, delete-registry-entry danger zone — the retired
  `/tags/[tag]` page verbatim); the page opens on **Digest** by default and
  renders the tab row even when the timeline 404s (registry-only tag).

## Entity dossiers (2026-09-07)

- Every entity carries a dossier — a short generated "who/what is this in
  the story" card (1–3 sentences, output language follows the STT language
  of the recording). Written by a DEDICATED best-effort LLM pass inside
  enrich (+1 call per recording, never fails the stage), AFTER dedup and
  BEFORE the graph write; stored as the `description` property on the
  Neo4j node (dies with purge, rebuilds with rebuild, folds on merge) and
  additively in `meta/events.json` `entities[]`.
- Auto-refresh: every enrich re-describes the session's entities (input
  includes the old text); a depth-1 wave (+1 capped call, 12 neighbors)
  re-describes UNTOUCHED related entities whose dossiers may have gone
  stale. Untouched entities keep their last known state by design.
- Injection loop: `{known_entities}` (enrich) renders the dossier after
  each entity line (160-char cap); summarize gets a dossier block (top-15
  described entities, between the tag context and the glossary); the
  digest prompt shows dossiers on entity lines.
- Manual control: `PATCH /tags/{tag}/entities/{slug}/description` (user
  text; arms `description_edited` — generated passes never stomp it;
  propagates into the tag's events.json copies) and `POST
  /tags/{tag}/entities/{slug}/refresh-description` (full rebuild from
  every mentioning event + neighbors; refuses user-edited dossiers).
  Both go through the `SetEntityDescription` Temporal workflow.
- UI: the Vault tag page's Entities tab groups entities by type (group
  order = group size) and expands a row into the dossier card — text,
  Refresh/Edit actions, and "Mentioned in" links deep-linking to the
  recording player at the event's timecode; the Lattice drawer shows the
  dossier under the entity header.
