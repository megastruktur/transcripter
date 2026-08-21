# MEMORY

## KEY DECISIONS
<!-- Auto-loaded every session. Soft cap: 20 active rows; flag when exceeded. -->

| # | Date       | Decision                                                              | Rationale                                                            | Status   |
|---|------------|-----------------------------------------------------------------------|----------------------------------------------------------------------|----------|
| 1 | 2026-08-21 | Post-processing MVP, not real-time streaming transcription            | Real-time ×3 complexity; user confirmed                              | ✓ Active |
| 2 | 2026-08-21 | Diarization in MVP via `lintoai/linto-diarization-pyannote` container | Only supported CPU-only docker w/ HTTP API; weights bundled, no HF token; DER 11.0 VoxConverse | ✓ Active |
| 3 | 2026-08-21 | Client: Tauri v2 + SvelteKit SPA (adapter-static), no React           | User rejects React; SvelteKit SPA is documented Tauri path           | ✓ Active |
| 4 | 2026-08-21 | Durable pipeline: Temporal self-hosted (auto-setup + Postgres + UI)   | User explicitly opted in despite simpler SQLite alternative           | ✓ Active |
| 5 | 2026-08-21 | FLAC as transport + storage audio format                              | Lossless for ML, ~2-3× smaller than WAV; faster-whisper reads via ffmpeg | ✓ Active |
| 6 | 2026-08-21 | Resumable upload: client spool → chunked offset PUT → ack → cleanup   | Survives network drops; audio never stored long-term on client        | ✓ Active |
| 7 | 2026-08-21 | LAN-first; single-user bearer-token auth; no TLS inside MVP           | User: «давай сначала LAN»                                             | ✓ Active |
| 8 | 2026-08-21 | Server storage path from config.yaml (NAS mount on server host)       | User: audio lives on server, configurable path                       | ✓ Active |
| 9 | 2026-08-21 | Summarize disabled unless model configured; OpenAI-compatible (LiteLLM) | User requirement; fits existing LiteLLM proxy                        | ✓ Active |
| 10 | 2026-08-21 | Per-stage regenerate (transcribe/diarize/summarize) via API button    | User requirement                                                      | ✓ Active |
| 11 | 2026-08-21 | Capture permission + non-silence pre-flight check on EVERY record start | User bug: first reference recording was empty (permission not granted) | ✓ Active |
| 12 | 2026-08-21 | macOS system audio via cpal default_output_device (reference approach) | Proven by ActaVoces; ScreenCaptureKit only as later fallback         | ✓ Active |
| 13 | 2026-08-21 | Reference repo at /home/megastruktur/projects/vendor/actavoces (read-only) | Capture architecture studied; not a code dependency                  | ✓ Active |

## Running State

- 2026-08-21: Brainstorm + stack research complete (SvelteKit↔Tauri confirmed via official docs; cpal capture verified in reference source). Plan saved to `.ship-it/plans/transcripter-mvp-2026-08-21.md`, adversarial review loop started. Diarization research: `~/.hermes/kanban/attachments/t_cae49ae2/diarization-research.md`.

## Running State — 2026-08-21 MVP complete

- All 12 tasks shipped; e2e green end-to-end: upload w/ drop+resume → sha256 bit-identity → transcribe/diarize/merge done, summarize skipped (no model) → state=done, 4 artifacts.
- Host quirks: ports 8080/8081 busy (traefik/llama.cpp) → api:8090, ui:8082; LinTO wants SERVICE_MODE=http, listens :80; host shell itself runs in a container (docker can't see /tmp; /usr/lib64 nearly empty on host).
- Toolchain: zigcc linker (glibc 2.38 target), dockerized pkg-config, client .link-libs so-copies (gitignored).
- Roborev review loop ran per commit; all findings addressed in follow-up commits.
- Config backup: backups/config.yaml.20260821.
