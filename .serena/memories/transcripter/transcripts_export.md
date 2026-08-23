# Transcript Note Export (2026-08-23)

Worker-side export of finished recordings as consolidated Markdown notes into a
host-configurable directory (Obsidian vault). Plan:
`.ship-it/plans/transcripts-export-2026-08-23.md` (5-round kimi-k3 critique,
ACCEPT-WITH-RESERVATIONS; critique trail in `transcripts-export-critique-kimi-k3.md`).
Related: `mem:transcripter_stack`.

## Architecture

- New Temporal activity `export_transcript` (activities.py), scheduled in
  `ProcessRecording.run` `finally` after `finalize_recording`, with
  `cancellation_type=WAIT_CANCELLATION_COMPLETED`. IMPORTANT: temporalio 1.31
  REMOVED `workflow.shield` and `CancellationScope` — this cancellation_type is
  the documented replacement; do not re-introduce shield.
- Activity = async Popen wrapper around `python -m worker.export_once <rec_id>`
  (start_new_session, 20s asyncio.wait_for, on timeout killpg SIGKILL + ABANDON
  never wait). PID registry with WNOHANG reap sweep (ECHILD ⇒ discard), cap 4
  live children. Errors return as `transcript_note` values, never exceptions.
- `worker/export.py`: pure functions — `note_name` (deterministic
  `{YYYY-MM-DD_HH-MM} {title|call} {id8}.md`; sanitize Windows-reserved +
  `#[]^` + control chars + edge dots; UTF-8 byte cap 240; zone from
  `TRANSCRIPTER_TZ` env, default UTC), `build_note` (yaml.safe_dump frontmatter:
  recording_id, title, created ISO+offset, date, tags [transcripter/call],
  duration_sec omitted when NULL; ## Summary if present; ## Transcript
  diarized > plain), `write_note_atomic` (unique uuid tmp + os.replace + hidden
  `.name.md.lock` flock, never unlinked). `run()` loads config, init_engine,
  no-ops unless state==done.
- `worker/backfill.py`: `cd /app/worker && .venv/bin/python -m worker.backfill`
  re-exports all done recordings via the same activity wrapper; refuses when
  `transcripts.sentinel` set but missing.

## Config

- `TranscriptsConfig` in worker config.py: `path` fixed `/transcripts`,
  `sentinel: ""` optional (e.g. `.obsidian`). No env override of the container
  path by design (divergence footgun). Host dir: `TRANSCRIPTS_DIR` in server/.env
  → compose bind `${TRANSCRIPTS_DIR:-./storage/transcripts}:/transcripts`
  (worker service only). Dir must exist before `up` (docker creates root-owned
  dirs otherwise; this host: storage is root-owned, created transcripts dir via
  `docker run -v ...: alpine mkdir+chown 1000:1000`).

## Policies (critic-forced)

- Machine owns the note: regenerate OVERWRITES (never existence-probe/read-back
  — TOCTOU + Obsidian-edit fragility). User annotations → linked notes.
- DELETE recording leaves the note (recording_id in frontmatter = cleanup hook).
- Future title-edit endpoint MUST hook re-export or drop title from filename.
- Future UI-configurable path = mount-model change (stable parent + subpath),
  not config plumbing.
- NAS bind should be soft-mounted; systemd RequiresMountsFor= drop-in
  recommended.

## Ops

- E2E smoke step 9b asserts the note (exists, frontmatter recording_id,
  exactly one).
- After worker code edits: `docker compose build worker` REQUIRED (image layer
  cache), then `up -d worker`.
- Deploy checklist: no open ProcessRecording workflows + worker not
  restart-looping (finally-command-sequence change replays in-flight).
