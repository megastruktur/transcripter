"""Export finished transcripts as a folder of per-artifact notes.

Each done recording gets one folder in the transcripts dir:
`{YYYY-MM-DD_HH-MM} {title} {id8}/` containing the meta artifacts 1:1
(`transcript.md`, `diarized-transcript.md`, `summary.md`), each with its own
frontmatter. The folder name is fully deterministic (id8 always appended):
rename renames the folder in place (`os.rename`), regenerate rewrites the
artifact files under the same name — an Obsidian user edit or a stale NFS
stat can't fork duplicates or race two workers (see
.ship-it/plans/vault-folder-export-2026-08-27.md).
"""

import fcntl
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger("transcripter.export")

# Windows-reserved plus Obsidian-significant: # (heading ref in wikilinks),
# [] and ^ (wikilink/block-ref syntax — they break [[...]] parsing).
_ILLEGAL = re.compile(r'[/\\:*?"<>|#\[\]^]')
_CTRL = re.compile(r"[\x00-\x1f]")
# 255-byte NAME_MAX with margin for ".{uuid8}.tmp" and ".lock".
_MAX_NAME_BYTES = 240

# Meta artifacts exported 1:1 into the folder (whitelist for mirror-delete:
# anything else in the folder is user content and is never touched).
ARTIFACTS = ("transcript.md", "diarized-transcript.md", "summary.md")

TZ_ENV = "TRANSCRIPTER_TZ"
DEFAULT_ZONE = "UTC"


class ExportError(Exception):
    """Raised on misconfiguration or unexpected runtime failures."""


def configured_zone() -> ZoneInfo:
    """Timezone for folder names/frontmatter. Env TRANSCRIPTER_TZ, default UTC.

    The worker reads env at start (existing convention), so a TZ change means
    a worker restart — same as every other knob.
    """
    name = os.environ.get(TZ_ENV, "").strip() or DEFAULT_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ExportError(f"invalid {TZ_ENV}={name!r}: {e}") from e


def folder_name(title: str, recording_id: str, created_at: datetime, zone: ZoneInfo) -> str:
    """Deterministic folder name: `{YYYY-MM-DD_HH-MM} {title|call} {id8}`.

    - id8 is ALWAYS in the name: two same-minute same-title calls can never
      collide, so no existence probe / read-back is ever needed.
    - Capped by UTF-8 bytes on a char boundary, always preserving ` {id8}`.
    """
    ts = created_at.astimezone(zone).strftime("%Y-%m-%d_%H-%M")
    id8 = recording_id[:8].lower()
    suffix = f" {id8}"

    t = _CTRL.sub(" ", title)
    t = _ILLEGAL.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if not t:
        t = "call"

    room = _MAX_NAME_BYTES - (len(ts.encode()) + 1 + len(suffix.encode()))
    t_bytes = t.encode()
    while t_bytes and len(t_bytes) > room:
        t_bytes = t_bytes[:-1]
    t = t_bytes.decode(errors="ignore").rstrip() or "call"
    return f"{ts} {t}{suffix}"


def _iso(dt: datetime, zone: ZoneInfo) -> str:
    return dt.astimezone(zone).isoformat(timespec="seconds")


@dataclass(slots=True)
class Rec:
    id: str
    title: str
    created_at: datetime
    duration_sec: float | None
    state: str = ""


def folder_path(root: Path, rec: Rec, zone: ZoneInfo) -> Path:
    return root / folder_name(rec.title, rec.id, rec.created_at, zone)


def build_artifact(path: Path, rec: Rec, zone: ZoneInfo) -> str:
    """One exported artifact file: frontmatter + raw artifact body.

    Frontmatter is serialized with yaml.safe_dump — a title containing
    `: " [ {` must never produce broken YAML (the deterministic naming means
    we never parse it back, but Obsidian's Properties panel would still break).
    """
    fm: dict[str, object] = {
        "recording_id": rec.id,
        "title": rec.title,
        "created": _iso(rec.created_at, zone),
        "date": rec.created_at.astimezone(zone).date().isoformat(),
        "tags": ["transcripter/call"],
    }
    if rec.duration_sec is not None:
        fm["duration_sec"] = rec.duration_sec

    body = path.read_text(encoding="utf-8").rstrip()
    return "\n".join(
        ["---", yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip(), "---", "", body, ""]
    )


def write_note_atomic(path: Path, content: str) -> None:
    """Unique-tmp + os.replace + flock: concurrent exports of the same
    recording (activity + backfill, or two regenerates) can never tear the
    note or interleave partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    lock = path.with_name(f".{path.name}.lock")
    try:
        tmp.write_text(content, encoding="utf-8")
        with open(lock, "a+b") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                os.replace(tmp, path)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        tmp.unlink(missing_ok=True)


def load_recording(rec_id: str) -> Rec:
    from .db import Recording, session

    with session() as s:
        rec = s.query(Recording).filter(Recording.id == rec_id).one()
        return Rec(rec.id, rec.title or "", rec.created_at, rec.duration_sec, rec.state.value)


def check_sentinel(root: Path, sentinel: str) -> None:
    """Boot-race guard: a bind mount over an empty mountpoint is writable but
    wrong. With transcripts.sentinel set (e.g. ".transcripter"), refuse to export
    unless it exists under the root."""
    if sentinel and not (root / sentinel).exists():
        raise ExportError(
            f"transcripts sentinel {sentinel!r} missing under {root} — "
            "export dir looks wrong (empty mountpoint?); refusing to write"
        )


def _folder_pattern(rec: Rec) -> re.Pattern[str]:
    """Match app-scheme folders for this recording: `{ts} {anything} {id8}`.

    Timestamp is derived from created_at at call time (TZ may change between
    exports), so the pattern pins only the id8 suffix — the same property the
    old flat-note sweep relied on."""
    return re.compile(
        rf"^\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}} .+ {re.escape(rec.id[:8].lower())}$"
    )


def export_recording(
    root: Path,
    meta: Path,
    rec: Rec,
    zone: ZoneInfo,
    sentinel: str = "",
) -> Path | None:
    """Export one recording's folder; None when there is nothing to export.

    - Rename: an existing folder for this recording under a previous title is
      renamed in place (os.rename) — Obsidian edits and user files survive.
      FileNotFoundError from a concurrent export having already renamed it is
      a no-op; other OSError propagates (next export / backfill recovers).
    - Regenerate: artifact files are rewritten atomically in place.
    - Mirror: known artifact names absent from meta are unlinked from the
      folder (e.g. diarize disabled → diarized-transcript.md must not go
      stale in the vault); unknown/user files are never touched.
    """
    check_sentinel(root, sentinel)
    if not (meta / "transcript.md").is_file() and not (meta / "diarized-transcript.md").is_file():
        return None

    # A fresh export dir may not exist yet (TRANSCRIPTS_DIR unset →
    # ./storage/transcripts, first export ever). The rename-scan below must
    # not crash on it — write_note_atomic creates parents for the files.
    root.mkdir(parents=True, exist_ok=True)
    target = folder_path(root, rec, zone)
    if not target.is_dir():
        pattern = _folder_pattern(rec)
        matches = [
            entry
            for entry in list(root.iterdir())
            if entry.is_dir() and pattern.match(entry.name)
        ]
        # Multiple matches = a double-rename race leftover. Prefer the folder
        # holding non-app (user-authored) files: edits are the only content
        # that cannot be regenerated from meta; the app-only leftovers are
        # swept after the export.
        matches.sort(key=lambda e: not any(not _is_app_file(c.name) for c in e.iterdir()))
        for entry in matches[:1]:
            try:
                os.rename(entry, target)
                log.info("export: renamed folder %s -> %s", entry.name, target.name)
            except FileNotFoundError:
                # Concurrent export already renamed it — same deterministic
                # target, nothing left to do.
                pass

    written = False
    for name in ARTIFACTS:
        src = meta / name
        if src.is_file():
            write_note_atomic(target / name, build_artifact(src, rec, zone))
            written = True
        else:
            # Mirror: a regenerate that dropped the artifact (diarize disabled,
            # summarize skipped) must not leave it stale in the vault.
            stale = target / name
            if stale.exists():
                stale.unlink()
                # The lockfile stays: unlinking it while a concurrent writer
                # holds flock on that inode lets two writers lock different
                # inodes (the permanence invariant write_note_atomic relies on).
    return target if written else None


def sweep_stale_notes(root: Path, rec: Rec, keep: Path) -> None:
    """Delete stale app-scheme vault entries for this recording, keeping `keep`:

    - legacy flat notes (pre-folder scheme) `* {id8}.md` + lockfiles — the
      migration path;
    - old-title folders orphaned by a double-rename race (export A renamed
      title1→title2 while export B created title3) — removed recursively,
      but ONLY when they contain no non-app files: a folder with anything
      user-authored (a file without an app artifact/lock/tmp name) is left
      alone and logged, never destroyed.

    Scoped to the app's own naming scheme (`YYYY-MM-DD_HH-MM ` prefix AND
    ` {id8}` suffix) — user-authored notes/folders are never matched.
    Best-effort: an entry that can't be removed (NFS hiccup, permissions)
    is skipped, it never fails a successful export."""
    flat = re.compile(
        rf"^\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}} .+ {re.escape(rec.id[:8].lower())}\.md$"
    )
    for entry in list(root.iterdir()):
        if entry == keep:
            continue
        if entry.is_file() and flat.match(entry.name):
            try:
                entry.unlink()
                # Lockfiles (.{name}.lock) are permanent per write_note_atomic
                # and never match the sweep regex themselves.
                entry.with_name(f".{entry.name}.lock").unlink(missing_ok=True)
                log.info("export: migrated legacy flat note %s", entry.name)
            except OSError as e:
                log.warning("export sweep: could not remove legacy note %s: %s", entry, e)
        elif entry.is_dir() and _folder_pattern(rec).match(entry.name):
            if any(
                not _is_app_file(child.name)
                for child in entry.iterdir()
            ):
                log.warning(
                    "export sweep: stale folder %s contains non-app files; leaving for manual cleanup",
                    entry,
                )
                continue
            try:
                shutil.rmtree(entry)
                log.info("export: swept orphaned old-title folder %s", entry.name)
            except OSError as e:
                log.warning("export sweep: could not remove stale folder %s: %s", entry, e)


def _is_app_file(name: str) -> bool:
    """Names the exporter itself can create inside a recording folder:
    artifacts, their `.{name}.lock` fences and `.{name}.{uuid8}.tmp` staging."""
    if name in ARTIFACTS:
        return True
    return any(
        name == f".{a}.lock" or (name.startswith(f".{a}.") and name.endswith(".tmp"))
        for a in ARTIFACTS
    )

def run(rec_id: str) -> Path | None:
    """Load config + recording, no-op unless done, export.

    Shared by the export_once CLI (subprocess) and unit tests. The CLI runs
    in a SUBPROCESS spawned (and SIGKILL-abandoned on timeout) by the
    export_transcript activity / backfill — a dead NAS mount parks the child
    in D-state, which no in-process exception handling can survive.

    Returns the exported folder path (or None on no-op).
    """
    from .config import load_config
    from .db import init_engine

    cfg = load_config()
    init_engine(cfg.database.url)
    zone = configured_zone()
    rec = load_recording(rec_id)
    if rec.state != "done":
        return None
    meta = cfg.recordings_root / rec_id / "meta"
    path = export_recording(cfg.transcripts.path, meta, rec, zone, cfg.transcripts.sentinel)
    if path is not None:
        sweep_stale_notes(cfg.transcripts.path, rec, path)
    return path
