# 🎙️ Transcripter Maximus

Self-hosted call recorder with server-side ML: record meetings/calls on your
desktop, upload in the background, and get a **transcript with speaker labels
and a summary** — every stage re-runnable on demand.

```
┌──────────────────────────┐          ┌─────────────────────────────────────┐
│ Client (Tauri v2)        │  FLAC    │ Server (Docker Compose)             │
│ ┌──────────────────────┐ │  via     │ ┌────────┐  ┌────────────────────┐  │
│ │ mic + system audio   │─┼──PUT─────┼▶│  API   │─▶│ Temporal worker    │  │
│ │ FLAC encode          │ │ resumable│ │FastAPI │  │ transcribe         │  │
│ │ spool + retry        │ │ chunks   │ │ :8090  │  │ → diarize → merge  │  │
│ └──────────────────────┘ │          │ └────────┘  │ → summarize        │  │
│ SvelteKit UI             │          │             └────────────────────┘  │
                                      │ PostgreSQL · Temporal UI :8082      │
                                      │ NAS-backed recording storage        │
                                      └─────────────────────────────────────┘
```

## Highlights

- **Client is thin, server does the ML.** Capture + FLAC encode + upload on the
  desktop; transcription (faster-whisper), diarization (LinTO/pyannote) and
  summarization (any OpenAI-compatible API) run in Docker on your hardware.
- **Durable pipeline.** Temporal drives the four stages with per-stage retry
  and status; a crash or restart never loses a recording mid-processing.
- **Resumable uploads.** Audio is spooled locally, uploaded as offset-addressed
  chunks, verified by SHA-256 at finalize. A dropped Wi-Fi connection resumes
  where it left off — on the next app run, too.
- **Per-stage regenerate.** Not happy with the transcript or summary? Hit
  regenerate for that single stage; downstream stages re-run automatically.
- **FLAC everywhere.** Lossless transport and storage; PCM is captured
  disk-first, so recording survives even if encoding dies.
- **Pre-flight on every record start.** Permission check + live RMS probe
  before capture begins — no more silent empty first recordings.
- **Single-user by design.** One bearer token, LAN-oriented, zero accounts.

## Pipeline

| Stage        | Engine                                   | Notes                                              |
| ------------ | ---------------------------------------- | -------------------------------------------------- |
| `transcribe` | faster-whisper (local) or OpenAI API     | model configurable, `small` by default             |
| `diarize`    | LinTO `linto-diarization-pyannote` (CPU) | bundled weights, no HF token needed                |
| `merge`      | IoU word↔segment matching                | fuses transcript words with speaker turns          |
| `summarize`  | OpenAI-compatible endpoint               | optional; stage reports `skipped` when no model    |
| `finalize`   | —                                        | always runs (even on stage failure) — unblocks UI |

Artifacts per recording: raw transcript, diarization turns, merged
speaker-attributed transcript, summary — all fetchable over the API and shown
in the client.

## Server setup

Requirements: Docker + Docker Compose plugin, ~4 GB free RAM for models.

```bash
cd server
cp config.example.yaml config.yaml        # optional: tune models/storage
echo 'TRANSCRIPTER_TOKEN=<your-secret>' > .env
docker compose up -d
```

| Service      | URL                        | Purpose                     |
| ------------ | -------------------------- | --------------------------- |
| API          | `http://localhost:8090`    | REST + health at `/health`  |
| Temporal UI  | `http://localhost:8082`    | pipeline observability      |
| Diarization  | internal (`:8070` on host) | LinTO HTTP service          |

Recordings land in `server/storage/recordings/<uuid>/` — point the compose
bind-mount at your NAS/export path to store elsewhere.

### Configuration (`server/config.yaml`)

```yaml
transcribe:
  backend: local          # local (faster-whisper) or api (OpenAI-compatible)
  model: small            # whisper model / API model id
  base_url: ""            # when backend=api
  api_key_env: ""         # ENV VAR NAME holding the key (never the key itself)

summarize:
  enabled: false          # true once model+base_url are set
  model: ""
  base_url: ""
  api_key_env: ""

diarization:
  endpoint: http://diarization:8080
```

Storage path, DB URL, and ports live in the same file / `docker-compose.yml`.

## Client setup

Requirements: Node 22+, pnpm, Rust toolchain, platform webkit deps
(see [tauri prerequisites](https://v2.tauri.app/start/prerequisites/)).

```bash
cd client
pnpm install
pnpm tauri dev     # desktop app window
```

In-app **Settings**: Server URL (`http://<server-host>:8090`) + the token from
your `.env`. That's the whole onboarding.

## API

All endpoints require `Authorization: Bearer <token>` (except `/health`).

| Method | Path                                                 | Purpose                                  |
| ------ | ---------------------------------------------------- | ---------------------------------------- |
| POST   | `/recordings`                                        | register recording (returns uuid)        |
| PUT    | `/recordings/{id}/audio?offset=N`                    | upload chunk (≤16 MB), resumable         |
| POST   | `/recordings/{id}/finalize`                          | SHA-256 check → start pipeline           |
| GET    | `/recordings` / `/recordings/{id}`                   | list / detail with per-stage status      |
| POST   | `/recordings/{id}/regenerate`                        | `{"stage": "transcribe"}` → rerun chain  |
| GET    | `/recordings/{id}/artifacts/{stage}[?file=…]`        | stage artifacts (transcript, summary, …) |
| GET    | `/recordings/{id}/audio`                             | download the FLAC                        |
| GET    | `/settings`                                          | effective config (secrets masked)        |

Quick check:

```bash
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

## Development

- Server API tests: `cd server/api && uv run pytest`
- Worker tests: `cd server/worker && uv run pytest`
- Client tests: `cd client/src-tauri && cargo test`
- Lint: `uvx ruff check .` / `uvx pyright` (server), `cargo clippy -- -D warnings` (client)

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
`src-tauri\target\release\transcripter.exe`.

**macOS** (12+, Xcode Command Line Tools: `xcode-select --install`):

```bash
git clone https://github.com/megastruktur/transcripter && cd transcripter/client
pnpm install
pnpm tauri build            # bundle/macos/*.app + bundle/dmg/*.dmg
```

Note: the `.app`/`.dmg` will be unsigned — right-click → Open on first
launch, or [sign it yourself](https://v2.tauri.app/distribute/sign/macos/).

**First run after install:** Settings → Server URL
`http://<server-LAN-IP>:8090` + your `TRANSCRIPTER_TOKEN`. Windows Firewall
will prompt on first upload — allow it.

**What to test on Windows** (system audio = WASAPI loopback, fully wired):
record a call (mic + system), stop, watch upload → stages → artifacts in
Recordings; regenerate a stage; kill the app mid-upload and restart
(spool resumes); pre-flight denial (deny mic permission in Windows
Settings → Privacy → Microphone, then hit Record).

## Scope & limitations (MVP)

- Single user, bearer token; no TLS termination in the compose stack — put it
  behind a reverse proxy (e.g. Traefik/Caddy) if you expose it beyond LAN.
- System-audio capture works fully on Windows/macOS loopback targets; on other
  platforms the client records mic only (system stream is drained honestly,
  `system_active=false`).
- The Rust uploader speaks plain HTTP (LAN). TLS support tracks a toolchain
  upgrade — see project memory notes.

## License

MIT
