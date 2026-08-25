"""Export finished transcripts as consolidated notes into the transcripts dir.

The note filename is fully deterministic ({date} {title} {id8}.md): the file
is always overwritten, never probed for existence — a stale NFS stat or an
Obsidian user edit can't fork duplicates or race two workers (see
.ship-it/plans/transcripts-export-2026-08-23.md).
"""

import fcntl
import logging
import os
import re
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

TZ_ENV = "TRANSCRIPTER_TZ"
DEFAULT_ZONE = "UTC"


class ExportError(Exception):
    """Raised on misconfiguration or unexpected runtime failures."""


def configured_zone() -> ZoneInfo:
    """Timezone for note names/frontmatter. Env TRANSCRIPTER_TZ, default UTC.

    The worker reads env at start (existing convention), so a TZ change means
    a worker restart — same as every other knob.
    """
    name = os.environ.get(TZ_ENV, "").strip() or DEFAULT_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ExportError(f"invalid {TZ_ENV}={name!r}: {e}") from e


def note_name(title: str, recording_id: str, created_at: datetime, zone: ZoneInfo) -> str:
    """Deterministic note basename: `{YYYY-MM-DD_HH-MM} {title|call} {id8}.md`.

    - id8 is ALWAYS in the name: two same-minute same-title calls can never
      collide, so no existence probe / read-back is ever needed.
    - Capped by UTF-8 bytes on a char boundary, always preserving ` {id8}.md`.
    """
    ts = created_at.astimezone(zone).strftime("%Y-%m-%d_%H-%M")
    id8 = recording_id[:8].lower()
    suffix = f" {id8}.md"

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


def note_path(root: Path, rec: Rec, zone: ZoneInfo) -> Path:
    return root / note_name(rec.title, rec.id, rec.created_at, zone)


def build_note(meta: Path, rec: Rec, zone: ZoneInfo) -> str:
    """Assemble frontmatter + Summary + Transcript from meta artifacts.

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

    parts = ["---", yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip(), "---", ""]

    summary = meta / "summary.md"
    if summary.is_file():
        parts += ["## Summary", "", summary.read_text(encoding="utf-8").rstrip(), ""]

    src = meta / "diarized-transcript.md"
    if not src.is_file():
        src = meta / "transcript.md"
    body = src.read_text(encoding="utf-8").rstrip() if src.is_file() else ""
    if body:
        parts += ["## Transcript", "", body, ""]
    return "\n".join(parts)


def write_note_atomic(path: Path, content: str) -> None:
    """Unique-tmp + os.replace + flock: concurrent exports of the same
    recording (activity + backfill, or two regenerates) can never tear the
    note or interleave partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hidden lockfile, never unlinked: unlinking a locked file lets two
    # writers hold locks on different inodes simultaneously. Dot-prefix keeps
    # it out of Obsidian (dotfiles are hidden there by default).
    lock = path.with_name(f".{path.name}.lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
            fcntl.flock(lf, fcntl.LOCK_UN)


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


def export_recording(
    root: Path,
    meta: Path,
    rec: Rec,
    zone: ZoneInfo,
    sentinel: str = "",
) -> Path | None:
    """Export one recording's note; None when there is nothing to export."""
    check_sentinel(root, sentinel)
    if not (meta / "transcript.md").is_file() and not (meta / "diarized-transcript.md").is_file():
        return None
    path = note_path(root, rec, zone)
    write_note_atomic(path, build_note(meta, rec, zone))
    return path


def sweep_stale_notes(root: Path, rec: Rec, keep: Path) -> None:
    """Delete older app-scheme notes for this recording (e.g. pre-rename
    titles) and their permanent lockfile siblings, keeping `keep`.

    Scoped to the app's own filename scheme — a `YYYY-MM-DD_HH-MM ` prefix
    AND the ` {id8}.md` suffix — so user-authored notes are never touched.
    Best-effort: an entry that can't be unlinked (NFS hiccup, permissions)
    is skipped, it never fails a successful export."""
    pattern = re.compile(
        rf"^\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}} .+ {re.escape(rec.id[:8].lower())}\.md$"
    )
    for entry in list(root.iterdir()):
        if entry.name == keep.name or not pattern.match(entry.name):
            continue
        try:
            entry.unlink()
            # Lockfiles (.{name}.lock) are permanent per write_note_atomic
            # and never match the sweep regex themselves.
            entry.with_name(f".{entry.name}.lock").unlink(missing_ok=True)
        except OSError as e:
            log.warning("export sweep: could not remove stale note %s: %s", entry, e)


def run(rec_id: str) -> Path | None:
    """Load config + recording, no-op unless done, export.

    Shared by the export_once CLI (subprocess) and unit tests. The CLI runs
    in a SUBPROCESS spawned (and SIGKILL-abandoned on timeout) by the
    export_transcript activity / backfill — a dead NAS mount parks the child
    in D-state, which no in-process exception handling can survive.
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

