---
name: transcripter-client-run
description: Launch the Transcripter Tauri desktop app in dev mode and verify the UI actually works — window presence, first-run onboarding (server URL + token, connection test), and the headless browser path for API-only pages. Use when asked to run, launch, or start the client/app, smoke-test the desktop UI, verify the window renders, connect the client to the local transcription server, or drive Settings or the recordings page from a browser tab.
metadata:
  version: "1.0"
---

# transcripter-client-run

Launches the Tauri client in dev mode and proves the UI works (window up, onboarding green).
Prerequisite: the server stack is up (`transcripter-stack-up`) — `curl -s http://localhost:8090/health` → `{"status":"ok"}`.
Release builds and `pnpm tauri build` belong to `transcripter-client-build`.

## Launch (from `client/`)

```bash
pnpm tauri dev
```

Runs `pnpm dev` (Vite on port 5173, `strictPort`) then `cargo run`.
Ready when the log prints:

    Running `target/debug/transcripter`

The first run compiles the whole Rust tree (~400 crates); later runs are fast.

## Verify the window exists

```bash
pgrep -fl "target/debug/transcripter"
osascript -e 'tell application "System Events" to get name of every process whose background only is false'
```

The second command lists `transcripter` among the foreground processes. Then prove the UI
renders:

```bash
screencapture -x /tmp/shot.png
```

Inspect with a vision tool. On the Recorder page you should see:

- Left nav rail: `Record` · `Library` · `Settings`; context bar name `Recorder`
- Title `Record audio`, a `Recording name` input (placeholder `e.g. Product sync — August 22`)
- Red `Start recording` button, subtext `Checks devices before capture`
- Device panel: `Microphone` and `System audio` dropdowns + `Check selected devices`
- Footer: `Server connected` / `No pending uploads` once Settings is linked (below)

## First-run onboarding

1. Nav → `Settings` (page title `Server connection`).
2. `Server address` → `http://localhost:8090`; `Bearer token` → the `TRANSCRIPTER_TOKEN` value from `server/.env` (`dev-local-token` in this setup).
3. Click `Test and save connection`. Success shows `Connection established` / `Health and authorization verified.`; a wrong token shows `unauthorized: wrong token`.
4. Settings persist in `localStorage` under key `transcripter.apiConfig` (`client/src/lib/api.svelte.ts`); chosen devices live under `transcripter.microphone` / `transcripter.system-output`.

## Headless path (drive the UI from a browser tab)

While `pnpm tauri dev` runs, the SAME UI is served at `http://localhost:5173` in a normal
browser — no app window needed for API-only pages. This is how Settings → `Test and save
connection` and the recordings page were driven headlessly this session. The recordings page
(page title `The archive`) lists recording cards with title + state (`uploading`/`processing`/
`done`/`failed`) plus one stage chip each — `Transcript` `Diarize` `Speakers` `Summary` — with
re-run buttons once a recording is `done`/`failed`; artifact buttons are `TXT`/`DIA`/`SUM`/`JSON`.
It polls `GET /recordings` every 3s and shows `No matching captures` when empty.

## Gotchas

- **Tauri commands never work in the plain browser.** Anything calling `invoke(...)` from
  `client/src/lib/tauri.ts` — `cmd_pre_flight`, `cmd_start_recording`, `cmd_stop_recording`,
  `cmd_upload_now`, `cmd_recording_frames`, `cmd_retry_pending`, `cmd_list_audio_devices` —
  works ONLY inside the app window. So record/stop and the device panel cannot be smoke-tested
  from a `http://localhost:5173` browser tab; use the app window for those, or the API-level
  flow (`transcripter-e2e-smoke`).
- **HTTPS is rejected.** The uploader is http-only (LAN MVP); an `https://` server address
  fails in Settings with `HTTPS is unsupported by the uploader in this build. Use an HTTP
  address on your trusted LAN.`
- **Port 5173 is `strictPort`** (`client/vite.config.ts`): a second `pnpm tauri dev` fails on
  the port conflict instead of shifting to 5174 — stop the first dev run before restarting.
- **Start recording runs a pre-flight** (`cmd_pre_flight`: mic permission + RMS signal probe).
  macOS prompts for microphone permission on first run; a denied mic surfaces as a
  `Pre-flight failed` notice and no recording starts. A silent mic warns (`no mic signal
  detected (check input device or mute)`). `System audio` defaults to OFF (records mic only);
  a selected-but-unavailable system output blocks start with a `Signal warning`, while a
  silent one warns and records.
- **Process check:** the dev binary is `target/debug/transcripter` (see `pgrep` pattern above);
  stop `pnpm tauri dev` from its terminal to free port 5173.
