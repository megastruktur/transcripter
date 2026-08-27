# Transcript Folder Export (2026-08-27, supersedes 2026-08-23 flat-note scheme)

Worker-side export of finished recordings as a folder of per-artifact Markdown
notes into a host-configurable directory (Obsidian vault). Plans:
`.ship-it/plans/vault-folder-export-2026-08-27.md` (1-cycle critic,
ACCEPT-WITH-RESERVATIONS) and the superseded
`.ship-it/plans/transcripts-export-2026-08-23.md`.
Related: `mem:transcripter_stack`.

## Scheme

- Folder per done recording: `{YYYY-MM-DD_HH-MM} {title|call} {id8}/` —
  same sanitize + UTF-8 byte-cap-240 rules as the old flat name, minus `.md`.
- Inside: meta artifacts 1:1 — `transcript.md`, `diarized-transcript.md`,
  `summary.md` (only those that exist), each with its own yaml.safe_dump
  frontmatter (recording_id, title, created ISO+offset, date,
  tags [transcripter/call], duration_sec omitted when NULL).
- Rename recording (PATCH) → RENAME-ONLY: `os.rename` of the folder in
  place, files inside NOT rewritten (user edits sacred; frontmatter title
  goes stale until next regenerate). Plumbing: PATCH →
  `start_export(rec_id, rename_only=True)` → ExportRecording workflow input
  `{recording_id, rename_only}` → `export_transcript(args: dict)` activity →
  `export_once <id> --rename-only` → `run(rec_id, rename_only=True)` →
  `rename_folder()`. Full exports (pipeline finally / regenerate / backfill)
  always pass rename_only=False. Rename-scan finds the old folder by regex
  (`{ts} * {id8}`); with multiple matches (double-rename race) it PREFERS the
  folder with non-app files (edits are unregenerable); FileNotFoundError from
  a concurrent rename is a no-op, other OSError propagates.
- Regenerate → artifact files rewritten atomically in place
  (`write_note_atomic`: uuid tmp + os.replace + hidden `.{name}.lock` flock,
  lock NEVER unlinked — unlinking a locked inode lets two writers lock
  different inodes).
- Mirror-delete: known artifact names absent from meta are unlinked from the
  folder (diarize disabled → diarized-transcript.md must not go stale);
  unknown/user files never touched (whitelist `_is_app_file`).
- `sweep_stale_notes`: deletes legacy flat `* {id8}.md` + `.lock` (migration),
  and orphaned app-only old-title folders via shutil.rmtree — but leaves (and
  warns on) any stale folder containing non-app files.
- `export_recording` tolerates a missing export root (mkdir parents; first-
  ever export with TRANSCRIPTS_DIR unset).

## Architecture (unchanged from 2026-08-23)

- Temporal activity `export_transcript` (activities.py), scheduled in
  `ProcessRecording.run` `finally` after `finalize_recording`, with
  `cancellation_type=WAIT_CANCELLATION_COMPLETED`. temporalio 1.31 REMOVED
  `workflow.shield` — do not re-introduce it.
- Activity = async Popen wrapper around `python -m worker.export_once <rec_id>`
  (start_new_session, 20s wait_for, timeout → killpg SIGKILL + ABANDON never
  wait). PID registry with WNOHANG reap sweep, cap 4 live children. Errors
  return as `transcript_note` values, never exceptions.
- `worker/backfill.py` re-exports (= migrates) all done recordings via the
  same wrapper; refuses when `transcripts.sentinel` set but missing.

## Config (unchanged)

- `TranscriptsConfig` in worker config.py: `path` fixed `/transcripts`,
  `sentinel: ""` optional (marker INSIDE the export ROOT, not the recording
  folder). Host dir: `TRANSCRIPTS_DIR` in server/.env → compose bind
  `${TRANSCRIPTS_DIR:-./storage/transcripts}:/transcripts` (worker only).

## Policies

- Regenerate OVERWRITES artifact files (never existence-probe/read-back);
  rename PRESERVES the folder (user content survives).
- DELETE recording leaves the folder (recording_id in frontmatter = cleanup
  hook).
- NAS bind: keep HARD mount (subprocess 20s kill fences the pipeline);
  systemd RequiresMountsFor= drop-in recommended.

## Ops

- E2E smoke step 9b asserts exactly one folder `* {RID:0:8}` at maxdepth 1,
  non-empty `transcript.md` inside, frontmatter recording_id. NOTE: the smoke
  script reads `./storage/transcripts` — with a prod `.env`
  (`TRANSCRIPTS_DIR=/mnt/synology/...`) you must recreate the worker with
  `TRANSCRIPTS_DIR=` (default bind) before running e2e, then `up -d worker`
  again after. This asymmetry predates the folder scheme.
- After worker code edits: `docker compose build worker` REQUIRED (image
  layer cache), then `up -d worker`.
