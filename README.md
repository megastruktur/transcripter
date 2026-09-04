# 🎙️ Transcriptor Maximus

<p align="center">
  <img src="docs/images/readme-record.png" alt="Transcriptor Maximus — main window" width="880">
</p>

Self-hosted call recorder with server-side ML: record meetings/calls on your
desktop, upload in the background, and get a **transcript with speaker labels
and a summary** — every stage re-runnable on demand.

```
┌──────────────────────────┐          ┌─────────────────────────────────────┐
│ Client (Tauri v2)        │  FLAC    │ Server (Docker Compose)             │
│ ┌──────────────────────┐ │  via     │ ┌────────┐  ┌────────────────────┐  │
│ │ mic + system audio   │─┼──PUT─────┼▶│  API   │─▶│ Temporal worker    │  │
│ │ FLAC encode          │ │ resumable│ │FastAPI │  │ chunk → transcribe │  │
│ │ spool + retry        │ │ chunks   │ │ :8090  │  │ → diarize → merge  │  │
│ └──────────────────────┘ │          │ └────────┘  │ → summarize/enrich │  │
│ SvelteKit UI             │          │             └────────────────────┘  │
                                      │ PostgreSQL · Temporal UI :8082      │
                                      │ NAS-backed recording storage        │
                                      └─────────────────────────────────────┘
```

## Highlights

- **Client is thin, server does the ML.** Capture + FLAC encode + upload on the
  desktop; transcription (faster-whisper), diarization (DiariZen) and
  summarization (any OpenAI-compatible API) run in Docker on your hardware.
- **Durable pipeline.** Temporal drives every stage with per-stage retry
  and status; a crash or restart never loses a recording mid-processing.
- **Resumable uploads.** Audio is spooled locally, uploaded as offset-addressed
  chunks, verified by SHA-256 at finalize. A dropped Wi-Fi connection resumes
  where it left off — on the next app run, too.
- **Per-stage regenerate.** Not happy with the transcript or summary? Hit
  regenerate for that single stage; downstream stages re-run automatically.
- **FLAC everywhere.** Lossless transport and storage; PCM is captured
  disk-first, so recording survives even if encoding dies.
- **Pre-flight checks.** Permissions and device availability are checked on
  every record start; **Settings** runs a live RMS probe of both capture
  paths — no more silent empty first recordings.
- **Single-user by design.** One bearer token, LAN-oriented, zero accounts.
- **Types, tags & profiles.** Pick a recording type (`ttrpg`, `meeting`…)
  and a yaml profile rewrites the summary prompt and names the exported
  artifact — RPG session logs, structured minutes, your own domain.
  Freehand tags become knowledge namespaces: timeline, entities, digests,
  semantic search, recap. Declarative only; the core pipeline never
  breaks on a bad profile.

## Screenshots

| Library | Recording detail |
| ------- | ---------------- |
| ![Library](docs/images/readme-library.png) | ![Recording detail](docs/images/readme-recording-detail.png) |

## Hosting 100% locally

All ML runs on-box — no cloud account, no external API key, nothing leaves
your LAN. The **default configuration is the fully local one**:

```bash
cd server
cp config.example.yaml config.yaml     # defaults are already local
cp .env.example .env                   # set TRANSCRIPTER_TOKEN (openssl rand -hex 32)
docker compose up -d --profile diarizen
```

All `docker compose` commands in this README run from `server/` — the
relative `./storage` bind and the `config.yaml` mount depend on it.

| Concern       | Local answer                                                        |
| ------------- | ------------------------------------------------------------------- |
| Transcription | faster-whisper `small` (int8 CPU) inside the worker container       |
| Diarization   | bundled DiariZen container (`diarizen` profile) — global VBx       |
|               | clustering, whole-file: consistent speakers across hours          |
| Recordings    | `server/storage/recordings/` (bind mount — repoint at any dir)      |
| Vault         | `VAULT_DIR` when set — Obsidian vault: notes + audio; else          |
|               | `./storage/transcripts` (local-only fallback)                        |
| Metadata      | postgres in the `pgdata` named volume                               |

**One-time downloads, then offline.** The first worker start pulls the
whisper weights (~0.5 GB for `small`) from huggingface.co into the `models`
named volume; container recreates keep them. After that the whole stack
works with no internet access. Never run `docker compose down -v` unless
you mean it — it deletes the whisper and bge-m3 model caches, the
postgres database, the Speaches and DiariZen caches, and the Neo4j graph.

**Resources.** ~4 GB free RAM for the base stack (local whisper). The
DiariZen container needs ~2 GB RSS while diarizing (whole-file inference
at ~0.2× realtime on CPU: a 61-min meeting finishes in ~12 min, a
123-min one in ~24 min) and pulls ~350 MB of weights from huggingface.co
into the `diarizen-hf-cache` volume on first start. Enabling the bundled
Speaches profile with a larger model needs several GB more; check
`docker stats`.

**Optional: local summarizer.** Any OpenAI-compatible chat endpoint on the
same host/LAN works — e.g. Ollama (`ollama pull qwen3:14b`, serves
`:11434`):

```yaml
# server/config.yaml
summarize:
  enabled: true
  model: qwen3:14b
  base_url: http://<ollama-host>:11434/v1   # LAN IP or host.docker.internal —
                                            # NOT localhost (worker is a container)
  api_key_env: ""                            # keyless endpoint: no auth header sent
```

`docker compose restart worker` to apply.

**Verify the install:** `cd server && bash scripts/e2e_smoke.sh` pushes a
synthetic two-speaker recording through upload → pipeline → artifacts;
green output means the whole local stack works.

**Client:** build on your desktop OS (see
[Building the client](#building-the-client-for-windows--macos)), then point
Settings at `http://<server-LAN-IP>:8090` + your token.

## Design language

The client uses a techno-religious industrial design language: compact machine
panels, square geometry, blackened iron surfaces, bone text, arterial red,
aged brass, and diagnostic cyan. Structure and state carry the theme; ornamental
fantasy styling does not. See [`DESIGN_GUIDELINES.md`](./DESIGN_GUIDELINES.md)
for the canonical palette, component rules, motion, accessibility, copy, and
review checklist.

## How it works

Two companion documents explain the full pipeline logic (extraction,
deduplication, grouping, memory):

- [LOGIC_DIAGRAM.md](./LOGIC_DIAGRAM.md) — the detailed technical map: every
  stage, dedup level, namespace rule, and failure invariant, with function
  and file names.
- [LOGIC_DIAGRAM_SIMPLIFIED.md](./LOGIC_DIAGRAM_SIMPLIFIED.md) — the same
  logic in plain language (Russian), for non-technical readers.

## Pipeline

| Stage        | Engine                                   | Notes                                              |
| ------------ | ---------------------------------------- | -------------------------------------------------- |
| `chunk`      | ffmpeg segment cut (worker)              | optional (`chunk.enabled`); slices long audio into |
|              |                                          | ~10-min FLAC chunks + manifest so a whisper        |
|              |                                          | repetition loop poisons ≤1 chunk, not the recording |
| `transcribe` | faster-whisper (local) or OpenAI API     | model configurable, `small` by default             |
| `diarize`    | DiariZen `BUT-FIT/diarizen-wavlm-base-s80-md` | optional (`enabled: false` → stage `skipped`);    |
|              | (CPU, whole-file: `diarizen` profile)    | global VBx clustering over the entire recording    |
|              |                                          | — speakers stay consistent across hours; overlap-  |
|              |                                          | aware local windows; LinTO dialect still accepted  |
| `merge_speakers` | IoU word↔segment matching           | fuses transcript words with speaker turns          |
| `summarize`  | OpenAI-compatible endpoint               | optional; stage reports `skipped` when no model    |
| `enrich`     | LLM extraction → Neo4j + per-tag vector index | optional; graph off → `skipped` (best-effort) |
| `finalize`   | —                                        | always runs (even on stage failure) — unblocks UI |

Artifacts per recording: raw transcript, diarization turns, merged
speaker-attributed transcript, summary, extracted events — all fetchable
over the API and shown in the client. The recording page renders the
transcript, speakers and summary tabs as sanitized Markdown
(allowlist-only tags, no links/images); the events and JSON tabs stay
raw.

### Diarization: DiariZen (whole-file, primary engine)

Diarization runs as a **single request over the whole recording** — never
per-chunk. DiariZen (BUT-FIT, EEND-VC: overlap-aware local windows +
global VBx clustering) earns its keep exactly there: one clustering pass
over the full timeline gives globally consistent speakers across hours.
Per-chunk diarization (LinTO-era and the retired pyannote separation
stage both had it) fragmented speaker identity — a 123-min recording
came back as **36 chunk-local labels** instead of 6 people; an earlier
NVIDIA Sortformer pilot capped hard at 4 simultaneous speakers.
DiariZen's pilot on a 61-min meeting resolved **7 speakers** and on a
123-min recording **6**, at **RTF ≈ 0.2 on CPU** (~1.6 GiB RSS) — and
marks **~16–18 % of meeting time as overlapping speech** (Sortformer
detected ~13 %).

**Rollback.** `diarize.py` sniffs the response dialect: DiariZen speaks
`start`/`end`/`speaker` natively; LinTO's `seg_begin`/`seg_end`/`spk_id`
is still translated at the same boundary. The deprecated LinTO container
(`diarization` profile, host `:8070`) stays rollback-only — point
`DIARIZATION_ENDPOINT` at it (or any external LinTO) and no code path
changes. Pending removal.

### Speech separation — retired

The pyannote **SpeechSeparation** stage (`separate`) is retired and gone
from the pipeline: DiariZen's overlap-aware EEND-VC windows cover the
"who talks over whom" case it was built for, without its CPU cost
(RTF ≈ 0.75) or its 2-h whole-file OOM crashes. Historical notes: the
model (`pyannote/speech-separation-ami-1.0`) is **gated** on HuggingFace
(accept BOTH `speech-separation-ami-1.0` and `separation-ami-1.0` cards;
the pipeline and its weights are separate gated repos) — still relevant
only if you rebuild the old `Dockerfile.separation` image for a
pre-DiariZen worker. `SEPARATION_ENDPOINT` in `.env`/compose is now
inert.

### Chunking (long recordings, CPU voice stacks)

A single multi-hour STT request can collapse into the whisper **repetition
loop** (one phrase repeated to the end of the file — observed on a 90-min
recording after 01:01). With `chunk.enabled: true` the worker first slices
the audio into ~10-minute chunks (2 s overlap, midpoint seam assignment —
no duplicated or lost speech at the cuts) and then transcribes the
chunks **sequentially** (never in parallel: one CPU voice stack).
Chunking stays an **ASR-only** concern — diarization always sees the
whole recording. Effects:

- a poisoned chunk costs ~10 min of transcript instead of hours;
- a failed chunk fails the stage with `chunk N of M` in `last_error`, and
  **regenerate resumes only the missing chunks** (per-chunk status lives in
  `meta/chunks/chunks.json`);
- a chunk whose segments are >50 % one identical phrase is marked `suspect`
  in the manifest; regenerating `transcribe` re-runs only suspect chunks
  with a reset decoder context (empty prompt +
  `condition_on_previous_text=false` where the STT server accepts it);
- stereo dual-tap recordings keep per-channel chunk FLACs
  (`meta/channels/mic|system.flac`) for diarize: whole-file per-channel
  FLACs with `mic:`/`system:` speaker namespacing, re-derived via the
  idempotent `split_channels` when retention has cleaned them.

Off by default (`chunk.enabled: false` → stage reports `skipped`, pipeline
runs whole-file as before).

## Types, tags & profiles

Recordings carry an optional **type** (`ttrpg`, `meeting`…) picked at
record/import time and flat lowercase freehand **tags**; both are edited
on the recording page, and the library search box matches title, id and
tags (`GET /recordings?q=`). Editing the type or tags of a done recording
re-runs the affected stages automatically (type → summarize+enrich, since
profile routing is by type; tags → enrich, since tags are the graph
namespaces).

**Profiles** are yaml files in `server/profiles/` (bind-mounted read-only at
`/etc/transcripter/profiles`, re-scanned on every stage run — no restart
needed). A profile applies when the recording's type equals the profile's
`type`; it overrides the summarize prompt and renames the exported summary
artifact (`output_artifact`, default `summary.md`; the canonical
`meta/summary.md` is unchanged). Two examples ship in the repo
(`ttrpg-session-log` for type `ttrpg`, `meeting-notes` for type
`meeting`); the format contract for writing your own is
`server/profiles/README.md`. A broken profile logs a warning and is
skipped — the pipeline is never affected.

### Knowledge layer (tags as namespaces, `graph` profile)

With the graph layer on, every tag is a knowledge namespace. After
summarize, the `enrich` stage extracts events/entities/relations into
Neo4j — via the matched profile's `enrich:` section, or (with
`graph.enrich_all: true`, the default) a built-in fallback ontology — and
indexes transcript segments into per-tag sqlite-vec files (bge-m3
embeddings: local ONNX int8 in the worker, or any OpenAI-compatible
`/embeddings` endpoint — `graph.embed`; `EMBED_*` env overrides in
`.env`). Best-effort throughout: failures never hurt the recording.
On top of the graph:

- **Vault page** (client nav): one row per tag — sessions, entities,
  last activity, digest state — plus global cross-tag semantic search
  (`GET /search?q=`).
- **Tag page**: session timeline with click-to-seek events, an entity
  list with inline user rename (`PATCH /tags/{tag}/entities/{slug}`;
  user-corrected entities are exempt from auto-dedup), and the digest.
- **Digests**: `POST /tags/{tag}/digest {last_n}` renders a digest note
  of the last N tagged sessions to `<transcripts>/digests/<tag>.md`;
  `GET /tags/{tag}/digest` serves it back. With `graph.auto_digest:
  true` (default) it also auto-refreshes after enrich, at most once per
  `auto_digest_window_sec`.
- **Recap**: `summarize.recap: true` (default) prepends prior context to
  the summarize prompt — the tag's digest note plus KNN-retrieved
  segments from earlier sessions (the "Memory applied" chip in the
  client; knobs `recap_k`, `recap_budget_chars`).

Mobile/one-shot uploads: `POST /recordings/direct` accepts a single
multipart request (audio + title + tags + optional type and
`recorded_at` backdate; flac/wav/mp3 are transcoded server-side) — used
by the Android client and the desktop **Import** page (Android capture
path and build notes: `client/ANDROID_POC.md`).

## ML deployment matrix

ML services are compose **profiles**: the base stack needs none of them.

> **Upgrading from the LinTO setup?** Bundled diarization now lives behind
> the `diarizen` profile (DiariZen engine) — add `--profile diarizen` to
> your usual commands (config default `diarization.enabled: true` keeps
> working). The old `diarization` profile (LinTO) is deprecated,
> rollback-only, and pending removal. Set `diarization.enabled: false` in
> config.yaml to run transcript-only; the worker logs a startup warning
> if diarization is enabled but its endpoint is unreachable.

| Mode                          | Transcribe                     | Diarization                          | How                                                        |
| ----------------------------- | ------------------------------ | ------------------------------------ | ---------------------------------------------------------- |
| Bundled, zero-config (default)| faster-whisper in worker       | DiariZen behind profile              | `docker compose up -d --profile diarizen`                  |
| No ML containers              | faster-whisper in worker       | disabled — stages `skipped`          | `docker compose up -d` + `diarization.enabled: false`      |
| Bundled Speaches              | Speaches behind profile `stt`  | DiariZen behind profile              | `docker compose --profile stt --profile diarizen up -d` + config below |
| External / voice stack        | any OpenAI-compatible STT      | any DiariZen/LinTO endpoint          | point config/env at `http://<host>:<port>` — see below     |
| Knowledge graph (Neo4j)       | any of the above               | any of the above                     | `docker compose --profile graph up -d` + `NEO4J_PASSWORD` in `.env` — see below |

### Diarization via DiariZen (bundled, `diarizen` profile)

```bash
docker compose --profile diarizen up -d
```

The service answers the same HTTP contract every diarization backend in
this repo speaks — `POST /diarization` (multipart audio) →
`{"speakers": [...], "segments": [{start, end, speaker}]}` plus
`GET/HEAD /healthcheck` — so the worker needs no special wiring: the
default config endpoint (`http://diarization:80`, compose-internal DNS)
or `DIARIZATION_ENDPOINT=http://diarizen:80` in `.env` both reach it.
(A worker on the same compose network cannot reach a host-published
`host-IP:8071` — use the internal DNS name.) One CPU inference at a
time; published as host `:8071` (container `:80`).

| Concern   | Value                                                              |
| --------- | ------------------------------------------------------------------ |
| Image     | `transcripter-diarizen:0.1.0` (GHCR: `ghcr.io/megastruktur/transcripter:diarizen-0.1.0`) |
| Model     | `BUT-FIT/diarizen-wavlm-base-s80-md` (pruned, ~350 MB; override via `DIARIZEN_MODEL`) |
| License   | MIT code + **CC-BY-NC-4.0 weights** — personal self-host use       |
| Weights   | download into the `diarizen-hf-cache` volume on first start (~350 MB) |
| Speed     | RTF ≈ 0.2 CPU (61-min → ~12 min, 123-min → ~24 min), ~1.6 GiB RSS  |
| Speakers  | `spk_0..spk_N`, global clustering, no hard cap (pilot: 7 on 61 min, 6 on 123 min) |

See [Diarization: DiariZen](#diarization-diarizen-whole-file-primary-engine)
for the whole-file contract and the LinTO rollback story.

### Routing transcription to Speaches (bundled)

```bash
docker compose --profile stt --profile diarizen up -d
```

```yaml
# server/config.yaml
transcribe:
  backend: api
  model: Systran/faster-whisper-small   # full HF id; plain "small" 404s
  base_url: http://speaches:8000/v1     # the /v1 suffix is REQUIRED
  api_key_env: ""                       # local speaches needs no key
```

Then `docker compose restart worker` (config is read once at startup).
Speaches preloads `Systran/faster-whisper-small` at startup — the first
start downloads weights from huggingface.co into the `speaches-hf-cache`
volume (later starts work offline).

### Knowledge graph (Neo4j, behind `graph` profile)

The `enrich` pipeline stage extracts entities/events from the transcript
and writes them into a Neo4j Community Edition graph so cross-recording
digest queries (by tag) are possible. The stage is **best-effort**: when
the graph layer is off, `enrich` reports `skipped` and the recording
completes normally — the core pipeline is unaffected (same opt-in
pattern as `diarization`).

Extraction runs via the matched profile's `enrich:` section, or — with
`graph.enrich_all: true` (the default) — a built-in fallback ontology
when no profile matched, so untyped/untagged recordings enrich too. The
knowledge-layer surface built on the graph (Vault, timelines, digests,
search, recap) is described in
[Types, tags & profiles](#types-tags--profiles) above.

Enable the bundled Neo4j container with the `graph` profile:

```bash
# 1. Set a strong password in .env (generate: openssl rand -hex 24).
echo 'NEO4J_PASSWORD=<your-password>' >> .env

# 2. Start the neo4j service alongside whatever else you already run.
docker compose --profile graph up -d         # add this profile to existing flags
# e.g. with bundled diarization:
docker compose --profile diarizen --profile graph up -d
```

Then point the worker at the graph in `config.yaml`:

```yaml
# server/config.yaml
graph:
  uri: bolt://neo4j:7687      # compose-internal DNS; never publish bolt
  user: neo4j
  password_env: NEO4J_PASSWORD
  database: neo4j
```

`docker compose restart worker` to apply (config is read once at
startup). The image is `neo4j:5.26-community`
(LTS, internal 5.26.30, pinned per [SECURITY.md](./SECURITY.md) pre-update
checklist — pulled 2026-08-27), `mem_limit: 1.5g`, no published ports
(bolt driver reaches it on the compose network only). Auth is mandatory
(`NEO4J_AUTH=neo4j/<password>`); compose refuses to render the service
without `NEO4J_PASSWORD` set in `.env`. The worker never `depends_on`
neo4j, so toggling the profile doesn't restart the worker.

The local embedding backend reads the bge-m3 ONNX int8 export from
`/models/bge-m3-int8` in the shared `models` volume (the same volume as
the whisper weights; the export is not auto-downloaded — place it there
once). Switching embedding backend/model later is detected via recorded
index metadata: writes rebuild the affected per-tag index, searches
reply 503 with a backfill hint.

**Without the profile**, recordings still complete end-to-end:
`diarize` and `enrich` both report `skipped` if their backend is
unavailable — the recording is `done` and the client shows the existing
transcript + summary tabs as before. To use a different Neo4j
deployment (LAN host, embedded, Neo4j AuraDB free tier), leave the
profile off, set `graph.uri` in config.yaml to its bolt endpoint, and
provide the password via the env var named in `graph.password_env`.

Backups live next to the postgres dump in `server/backups/` — see
[`server/backups/README.md`](./server/backups/README.md) for the
[`scripts/backup.sh`](./server/backups/README.md) invocation and the neo4j-admin restore runbook.

### External voice stack (separate compose / host)

Architecture deep-dive for this mode (components, data flow, failure
semantics): [docs/backend-architecture.md](./docs/backend-architecture.md).

The worker reaches any reachable endpoint; env beats config:

```bash
# .env next to docker-compose.yml
DIARIZATION_ENDPOINT=http://<voice-host>:8071
```

For transcription set `transcribe.base_url` to `http://<host>:8000/v1`.
Same-host separate stacks can instead share a docker network
(`docker network create voice`, uncomment the `voice` block in
`docker-compose.yml`) and use service DNS names directly.

Working example — a LAN-hosted platform speaches (bearer auth, CPU,
accuracy-tuned):

```yaml
# config.yaml
transcribe:
  backend: api
  model: Systran/faster-whisper-large-v3   # or deepdml/faster-whisper-large-v3-turbo-ct2
  base_url: http://<stt-host>:8000/v1
  api_key_env: SPEACHES_API_KEY
```

```bash
# .env next to docker-compose.yml (compose passes it to the worker;
# docker compose up -d worker to apply — restart alone keeps old env)
SPEACHES_API_KEY=<key from 1Password, vault Secrets>
```

Word timestamps (`timestamp_granularities[]=word`) are what the diarization
merge keys off — the endpoint MUST return them (platform speaches does;
a LiteLLM hop in front of it currently drops the parameter, avoid proxying).

Verify with `STT=speaches bash server/scripts/e2e_smoke.sh` — it asserts
non-empty word timestamps end-to-end. Image updates go through
[SECURITY.md](./SECURITY.md) (pinned tags, pre-update checklist).

## Server setup

> **Megaserver prod status (2026-09-04):** Komodo stack `transcripter` runs
> api/worker images `ghcr.io/megastruktur/transcripter:v0.23.1-api|worker` —
> **Python 3.14.7** (first 3.14 release; v0.23.0 and earlier were 3.12).
> Compose pins the released tags in `server/docker-compose.yml`; deploy via
> Komodo DeployStack (git pull + compose up), never host `.env`.

Requirements: Docker + Docker Compose plugin, ~4 GB free RAM for models.

```bash
cd server
cp config.example.yaml config.yaml        # optional: tune models/storage
echo 'TRANSCRIPTER_TOKEN=<your-secret>' > .env
docker compose up -d --profile diarizen
```

| Service      | URL                        | Purpose                     |
| ------------ | -------------------------- | --------------------------- |
| API          | `http://localhost:8090`    | REST + health at `/health`  |
| Temporal UI  | `http://localhost:8082`    | pipeline observability      |
| Speaches     | internal (opt-in profile)  | OpenAI-compatible STT       |
| Diarization  | host `:8071` (container `:80`) | DiariZen HTTP service     |

Recordings land in `server/storage/recordings/<uuid>/` — point the compose
bind-mount at your NAS/export path to store elsewhere.

### Dev stack next to staging (megaserver)

On megaserver the running `transcripter` project IS the Komodo-managed staging
(api :8090; `https://megaserver-1.tail6fa4ba.ts.net:8090` via tailscale serve).
A second, isolated stack for testing:

```bash
mkdir -p storage-dev/transcripts && touch storage-dev/transcripts/.transcripter
docker compose -p transcripter-dev -f docker-compose.yml -f docker-compose.dev.yml --profile graph up -d
# …test… then tear down (doubles RAM):
docker compose -p transcripter-dev down
```

Deviations: api `127.0.0.1:18090`, temporal-ui `127.0.0.1:18082`, a
separate `storage-dev/` bind. The client speaks both `http://` and
`https://` base URLs (reqwest `rustls-tls`).

### Configuration (`server/config.yaml`)

```yaml
transcribe:
  backend: local          # local (faster-whisper) or api (OpenAI-compatible)
  model: small            # whisper model / API model id (HF id for speaches)
  base_url: ""            # when backend=api — MUST include /v1
  api_key_env: ""         # ENV VAR NAME holding the key (never the key itself)

summarize:
  enabled: false          # true once model+base_url are set; stage
                        # reports `skipped` while unconfigured
  model: ""               # any OpenAI-compatible chat model id; SUMMARIZE_MODEL
                        # in .env (compose passthrough) overrides this — see .env.example
  base_url: ""            # MUST include /v1 (dev stack: LiteLLM proxy at
                        # http://192.168.3.23:4000/v1, qwen3.8-27b-q4_k_m)
  api_key_env: ""         # env var NAME (dev stack: LITELLM_API_KEY in .env —
                        # a LiteLLM virtual key scoped to that model)
  recap: true             # (graph on) prepend prior-session context —
                        # digest note + semantic KNN hits — to the prompt

diarization:
  enabled: true           # false → diarize/merge skipped, no container needed
  endpoint: http://diarization:80   # bundled DiariZen (profile `diarizen`);
                        # env DIARIZATION_ENDPOINT wins — point it at any
                        # DiariZen or (rollback) LinTO endpoint
```

Storage path, DB URL, and ports live in the same file / `docker-compose.yml`.
The worker reads config once — `docker compose restart worker` to apply.

`SUMMARIZE_MODEL` in `server/.env` (passed to api+worker by compose) wins over
`summarize.model` in `config.yaml`; unset/empty keeps the yaml value. The API's
`/settings` endpoint reports the effective model.

### Vault export (Obsidian)

When `VAULT_DIR` is set in `.env`, every finished recording exports into the
Obsidian vault as ONE self-contained folder, grouped by capture date:

```
{vault}/2026/08/2026-08-31_14-05 Standup a1b2c3d4/
├── transcript.md            # meta artifacts 1:1, frontmatter each
├── diarized-transcript.md   #   (only those that exist)
├── summary.md               #   (renamed per profile.output_artifact)
└── .transcripter/           # hidden from Obsidian (dot-dir)
    ├── audio.flac           # the recording's FLAC, MOVED out of /storage
    └── manifest.json        # id/sha256/dates/title/tags/type — import base
```

```bash
# .env next to docker-compose.yml — the dir MUST exist before `up`
VAULT_DIR=/mnt/your-nas/vault/Transcripts
```

Key semantics:

- **Audio lives in the vault** after the pipeline: copy → sha256-verify →
  atomic rename → only then is the storage copy unlinked. A failed move
  (NAS down) leaves the storage copy; the next export/backfill retries.
  `/recordings/{id}/audio` and transcribe regenerates read storage first,
  vault second — both work after the move.
- **Folder naming/grouping** uses `recorded_at` (import backdate) or
  `created_at`; `TRANSCRIPTER_TZ` overrides the timezone (default UTC).
- **Dashboard.md** — a regenerated map-of-content (months + per-tag
  sections, wikilinks) at the vault root; overwritten on every export.
- **Regenerate rewrites artifact files in place** (atomic tmp+rename, one
  hidden `.{name}.lock` fence per file), **renaming a recording moves the
  folder in place** — your edits and extra files inside survive both.
  Artifacts that disappear from meta are mirror-deleted; files the exporter
  doesn't own are never touched. `digests/` and `indexes/` stay at the
  vault root.
- **Deleting a recording** removes the catalog row, the storage dir AND the
  vault folder (notes + audio + manifest).
- **Legacy layouts migrate themselves**: pre-vault root-level folders and
  old flat `* {id8}.md` notes are found by id8-scan and folded into the
  nested layout on the next export/backfill of that recording.
- Unset `VAULT_DIR` → no vault: notes go to `./storage/transcripts` (flat,
  legacy layout) and audio stays in `/storage`.
- `TRANSCRIPTS_DIR` still works (deprecated alias, wins nothing over
  `VAULT_DIR`).

Optional boot-race guard in `config.yaml`:

```yaml
vault:
  sentinel: ".transcripter"  # marker file you create INSIDE the vault dir
                             # (touch "$VAULT_DIR/.transcripter"); export
                             # refuses to run unless it exists — catches a
                             # bind over an empty NAS mountpoint
```

- **Export is best-effort.** A dead NAS mount can't hang the pipeline: the
  export runs in a subprocess (120 s kill-and-abandon, max 4 live children);
  failures land in the workflow result (`transcript_note`) in Temporal UI.
- **Recovery:** `docker compose exec worker sh -c 'cd /app/worker &&
  .venv/bin/python -m worker.backfill'` re-exports every `done` recording
  and moves any audio still sitting in storage (idempotent, same subprocess
  isolation, refuses on a missing sentinel).

- **Keep the NAS mount hard** (default): the export subprocess is killed
  after 120 s, so a hung NAS can't stall the pipeline, and atomic
  tmp+rename can't truncate an existing note. Soft mounts trade that for
  faster EIO on a dead server — not worth it for a personal vault.
  Consider a systemd drop-in `RequiresMountsFor=/mnt/your-nas` on the docker
  unit so binds never capture an empty mountpoint.
- Renaming a recording (`PATCH /recordings/{id}`) moves its vault folder
  in place (`os.rename`) and does NOT rewrite the files inside — your edits
  survive (the frontmatter `title` goes stale until the next regenerate) —
  fire-and-forget: the rename stands even if Temporal is down
  (`worker.backfill` is the recovery path).
- Workflow deploy note: the export activity was added to the workflow
  `finally` — deploy when no `ProcessRecording` executions are open and the
  worker isn't restart-looping (in-flight workflows replay against the new
  command sequence).

## Client setup

Requirements: Node 22+, pnpm, Rust toolchain, platform webkit deps
(see [tauri prerequisites](https://v2.tauri.app/start/prerequisites/)).

```bash
cd client
pnpm install
pnpm tauri dev     # desktop app window
```

In-app **Settings**: enter the server URL (`http://<server-host>:8090`) and the
token from your `.env`, then select **Test and save connection** once. Saved
credentials are checked automatically on later launches. Device selection
also lives in **Settings**: pick the microphone and system output there —
select **Off** for system audio when a microphone-only recording is
intended. On **Record**, **Start recording** opens both capture paths; the
microphone is authoritative — if it fails or stalls, the recording stops
with an error. System audio is a bonus source: a tap that fails to open
blocks the start, but one that delivers no frames within 10 s (no audio
flowing, slow aggregate spin-up) degrades the recording to microphone-only
with a live warning in the UI instead of killing it.

The left rail navigates **Record / Import / Library / Vault / Settings**:
**Import** pushes existing flac/wav/mp3 files through the same pipeline
(optional backdate + type), **Vault** is the knowledge-layer front end
(timelines, entities, digests, search).

## API

All endpoints require `Authorization: Bearer <token>` (except `/health`).

| Method | Path                                                 | Purpose                                  |
| ------ | ---------------------------------------------------- | ---------------------------------------- |
| POST   | `/recordings`                                        | register recording (returns uuid)        |
| PUT    | `/recordings/{id}/audio?offset=N`                    | upload chunk (≤16 MB), resumable         |
| POST   | `/recordings/{id}/finalize`                          | SHA-256 check → start pipeline           |
| GET    | `/recordings` / `/recordings/{id}`                   | paginated list (`?limit=&offset=&q=&state=`) / detail      |
| PATCH  | `/recordings/{id}`                                   | edit title/tags/type/date; auto re-run on tags/type |
| DELETE | `/recordings/{id}`                                   | delete recording + stored audio (204)    |
| POST   | `/recordings/{id}/regenerate`                        | `{"stage": "transcribe"}` → rerun chain  |
| GET    | `/recordings/{id}/artifacts/{stage}[?file=…]`        | stage artifacts (transcript, diarization, …) |
| GET    | `/recordings/{id}/summary`                           | latest summary artifact                  |
|GET/HEAD| `/recordings/{id}/audio`                             | download the FLAC (HTTP Range supported) |
| POST   | `/recordings/direct`                                  | one-shot multipart upload (mobile/import) |
| GET    | `/profiles`                                           | profile list for the type selector        |
| GET    | `/tags`                                               | distinct tags with counts                  |
| GET    | `/tags/{tag}/timeline`                                | tag sessions + events + entities           |
|GET/POST| `/tags/{tag}/digest`                                  | serve / render the digest note (202)       |
| GET    | `/tags/{tag}/search?q=&k=`                            | semantic KNN within the tag                |
| PATCH  | `/tags/{tag}/entities/{slug}`                         | user entity rename (re-embed)              |
| GET    | `/vault`                                              | per-tag manifest (Vault page)              |
| GET    | `/search?q=&k=`                                       | global cross-tag semantic search           |
| GET    | `/settings`                                          | effective config (secrets masked)        |

Quick check:

```bash
# returns {"items":[…],"total":N,"limit":50,"offset":0}
curl -s -H "Authorization: Bearer $TRANSCRIPTER_TOKEN" http://localhost:8090/recordings
```

## End-to-end smoke test

```bash
cd server && bash scripts/e2e_smoke.sh
```

Generates a synthetic two-speaker FLAC, uploads it **with a simulated
connection drop and resume**, verifies byte-identity via SHA-256, waits for
the pipeline, and checks all stage artifacts. Green output = the whole stack
works.

`GRAPH=1 bash scripts/e2e_smoke.sh` additionally exercises the enrich
write path (deterministic in-container probe + a live enrich regenerate).

## Development

### Skills for coding agents

Beyond the vendored Temporal skills (below), the repo ships nine
project skills in `skills/` (symlinked from `.claude/skills/`) that walk
an agent through the repeatable procedures: `transcripter-stack-up`
(bring the server up in any ML mode), `transcripter-test-suite` (pytest
/ ruff / pyright / cargo gates), `transcripter-e2e-smoke` (full
upload→pipeline→artifacts smoke), `transcripter-client-build`,
`transcripter-client-run`, `transcripter-android` (APK build + on-device
debug), `transcripter-release-ops` (version bump, tag, release CI),
`transcripter-troubleshooting` (known failure modes), and
`transcripter-verify-all` (the full ordered verification sequence).

Two vendored Temporal skills give agents accurate, up-to-date Temporal
knowledge instead of relying on stale training data:

- `skills/temporal-developer` — Python-SDK subset of
  [skill-temporal-developer](https://github.com/temporalio/skill-temporal-developer)
  @ `b01c632`: determinism, patterns, gotchas, versioning, testing, CLI
  workflow commands. Curated for this repo (core + python references only).
- `skills/temporal-ops` — self-hosted subset of
  [skill-temporal-ops](https://github.com/temporalio/skill-temporal-ops) @
  `c2f7602`: `temporal operator`/data-plane CLI, stuck-workflow and
  worker-health triage, non-determinism remediation. Cloud/`tcld` references
  removed; SKILL.md pins the repo access pattern (`docker compose exec
  temporal temporal --address temporal:7233 …` from `server/`).

Each skill's SKILL.md records the vendored commit; check upstream for updates
before syncing. Fresh official docs are fetchable as Markdown by appending
`.md` to any docs.temporal.io URL (index: `docs.temporal.io/llms.txt`).

### Tests & lint

- Server API tests: `cd server/api && uv run pytest`
- Worker tests: `cd server/worker && uv run pytest`
- Client tests: `cd client/src-tauri && cargo test`
- Lint: `uvx ruff check .` / `uvx pyright` (server), `cargo clippy -- -D warnings` + `pnpm check` (client)

## Building the client for Windows / macOS

Cross-compiling a Tauri app from Linux is not supported — build **on the
target OS**. Prerequisites on both: [Node 22+](https://nodejs.org),
[pnpm](https://pnpm.io), and a [Rust toolchain](https://rustup.rs)
(`rustup default stable`).

**Windows** (Windows 10/11, x64):

```powershell
# one-time: VS Build Tools 2022 (Desktop development with C++)
winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
# clone + build
git clone https://github.com/megastruktur/transcripter && cd transcripter\client
pnpm install
pnpm tauri build            # bundles appear in src-tauri\target\release\bundle\
```

Installers land in `bundle\nsis\*.exe` (recommended) and `bundle\msi\*.msi`.
For a quick test without installing: run
`src-tauri\target\release\transcriptor-maximus.exe`.

**macOS** (12+, Xcode Command Line Tools: `xcode-select --install`):

```bash
git clone https://github.com/megastruktur/transcripter && cd transcripter/client
pnpm install
pnpm tauri build            # bundle/macos/*.app + bundle/dmg/*.dmg
```

Note: the `.app`/`.dmg` is unsigned and not notarized. On macOS Sequoia+ a
downloaded copy reports *"damaged and can't be opened"* with no **Open
Anyway** option — clear the quarantine attribute once after install:
`xattr -cr "/Applications/Transcriptor Maximus.app"`. Permanent fix:
[sign and notarize it yourself](https://v2.tauri.app/distribute/sign/macos/).

**Android** — built and signed by CI on every release; no local build needed.
The `android` job in `.github/workflows/release.yml` produces a universal APK
and attaches it to the GitHub release as
`Transcriptor.Maximus_<version>_universal.apk`. Signing uses the repo secrets
`ANDROID_KEYSTORE_BASE64` / `ANDROID_KEYSTORE_PASSWORD` / `ANDROID_KEY_ALIAS` /
`ANDROID_KEY_PASSWORD`; the keystore backup lives outside git on the megaserver
at `~/projects/backups/transcripter/android-signing/`. The key is self-signed,
so Android shows an "unknown developer" prompt on install — but every release
is signed with the same key, so in-place updates keep working.

**First run after install:** Settings → Server URL
`http://<server-LAN-IP>:8090` + your `TRANSCRIPTER_TOKEN`. Windows Firewall
will prompt on first upload — allow it. macOS 14.2+ prompts separately for
Microphone and System Audio Recording access; grant both when system audio is
enabled.

**Platform capture verification:** on macOS, test both an output-only device and
a duplex USB/headset output. On Windows 10 1703+ or Windows 11, select a render
endpoint and play non-DRM audio while recording. In both cases, speak a separate
microphone marker and confirm both markers exist in the saved FLAC/transcript.

## Scope & limitations (MVP)

- Single user, bearer token; no TLS termination in the compose stack — put it
  behind a reverse proxy (e.g. Traefik/Caddy) if you expose it beyond LAN.
- Audio playback in the browser passes the bearer token as a `?token=` query
  param so `<audio>` elements can authenticate; that URL can appear in
  proxy/server access logs — an accepted single-user LAN tradeoff.
- System audio uses a Core Audio process tap on macOS 14.2+ and WASAPI shared-mode
  loopback on Windows 10 1703+/Windows 11. Linux remains microphone-only.
- Windows may exclude DRM/protected playback from loopback capture. This is an OS
  restriction, not an application fallback.
- The Rust uploader speaks plain HTTP (LAN). TLS support tracks a toolchain
  upgrade — see project memory notes.

## License

MIT
