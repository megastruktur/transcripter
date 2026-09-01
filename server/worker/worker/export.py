"""Export finished recordings into the vault: note folder + audio move.

Each done recording gets one folder in the vault root, nested by capture
date: ``{vault}/YYYY/MM/{YYYY-MM-DD_HH-MM} {title} {id8}/`` containing the
meta artifacts 1:1 (``transcript.md``, ``diarized-transcript.md``,
``summary.md``), each with its own frontmatter, plus a hidden
``.transcripter/`` subdir holding the recording's ``audio.flac`` (moved
out of /storage after the pipeline) and ``manifest.json`` (the
self-contained recording description — future import base). The folder
name is fully deterministic (id8 always appended): rename moves the
folder in place, regenerate rewrites the artifact files under the same
name — an Obsidian user edit or a stale NFS stat can't fork duplicates
or race two workers (see .ship-it/plans/vault-folder-export-2026-08-27.md).
Pre-vault layouts (flat root-level folders, ``{ts} {title} {id8}.md``
notes) are found by scan and migrated on the next export/backfill.

Wave A (knowledge-graph profiles): the summary artifact is renamed to
``profile.summarize.output_artifact`` in the note folder when a profile
matches the recording's type. Meta stays canonical (``meta/summary.md``).
Mirror-delete whitelist = static 3 + every known profile's output_artifact.
A profile REMOVED from disk drops out of the whitelist: its renamed note
(e.g. ``session-log.md``) is then classified as user-authored content and
deliberately left in place — orphan cleanup is manual (impl plan §1).
Re-match happens on every run (D11): profile edits between runs take
effect immediately.
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from dataclasses import field as _dataclass_field
from datetime import UTC, datetime
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

# Meta artifacts exported 1:1 into the folder. The mirror-delete whitelist
# extends this set with every profile's output_artifact (see _whitelist)
# so a profile removed from disk between regenerates does not leave its
# renamed note behind in the vault.
ARTIFACTS = ("transcript.md", "diarized-transcript.md", "summary.md")

# Default Obsidian-facing tag — every exported note carries it so the vault
# has at least one tag for filtering. Recording-level knowledge-graph tags
# are appended via _frontmatter_tags.
_DEFAULT_FRONTMATTER_TAG = "transcripter/call"

TZ_ENV = "TRANSCRIPTER_TZ"
DEFAULT_ZONE = "UTC"


class ExportError(Exception):
    """Raised on misconfiguration or unexpected runtime failures."""


def _iso(dt: datetime, zone: ZoneInfo) -> str:
    return dt.astimezone(zone).isoformat(timespec="seconds")


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


def _frontmatter_tags(rec_tags: list[str]) -> list[str]:
    """Obsidian frontmatter ``tags:`` list: default + dedup of recording tags.

    De-duplicated while preserving order. A missing recording.tags list
    collapses to just the default tag (legacy recordings pre-tag column).
    """
    out: list[str] = [_DEFAULT_FRONTMATTER_TAG]
    for t in rec_tags:
        if t and t not in out:
            out.append(t)
    return out


@dataclass(slots=True)
class Rec:
    """Recording-level metadata the exporter needs.

    `tags` renders Obsidian frontmatter ``tags:`` (user grouping).
    `type` (Phase 0) re-matches the knowledge-graph profile at export
    time (D11): routing is by recording.type; NULL → no profile.
    """

    id: str
    title: str
    created_at: datetime
    duration_sec: float | None
    state: str = ""
    tags: list[str] = _dataclass_field(default_factory=list)
    type: str | None = None
    # Import backdate (Phase 0): groups the vault folder (YYYY/MM) and the
    # folder-name timestamp. None → created_at (capture time).
    recorded_at: datetime | None = None
    # sha256 of the uploaded FLAC (catalog value) — gates the storage→vault
    # audio move: the copy is verified before the original is unlinked.
    sha256: str | None = None


def rec_timestamp(rec: Rec) -> datetime:
    """The date a vault folder is named/grouped by: recorded_at (import
    backdate) when set, else created_at (capture time)."""
    return rec.recorded_at or rec.created_at


def folder_path(root: Path, rec: Rec, zone: ZoneInfo) -> Path:
    ts = rec_timestamp(rec).astimezone(zone)
    return root / f"{ts:%Y}" / f"{ts:%m}" / folder_name(rec.title, rec.id, rec_timestamp(rec), zone)


def _year_month_dirs(root: Path) -> list[Path]:
    """root + every ``YYYY/MM`` dir under it (the nested vault layout).
    Unreadable dirs are skipped, never fatal — a scan must not die on an
    NFS hiccup."""
    dirs = [root]
    try:
        for year_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if year_dir.name.isdigit() and len(year_dir.name) == 4:
                dirs.append(year_dir)
                dirs.extend(m for m in year_dir.iterdir() if m.is_dir())
    except OSError as e:
        log.warning("export scan: unreadable vault root %s: %s", root, e)
    return dirs


def scan_recording_folders(root: Path, rec: Rec) -> list[Path]:
    """Every app-scheme folder for this recording under the vault, in any
    supported layout: nested ``YYYY/MM/`` (current) and root-level flat
    (pre-vault legacy). Sorted for determinism.

    The pattern pins only the id8 suffix, so old-title folders under a
    different YYYY/MM (recorded_at edit) are found too."""
    pattern = _folder_pattern(rec)
    found: list[Path] = []
    for parent in _year_month_dirs(root):
        try:
            found.extend(e for e in parent.iterdir() if e.is_dir() and pattern.match(e.name))
        except OSError as e:
            log.warning("export scan: unreadable dir %s: %s", parent, e)
    return sorted(found)


def build_artifact(
    path: Path,
    rec: Rec,
    zone: ZoneInfo,
    *,
    profile_id: str | None = None,
    artifact_name: str | None = None,
) -> str:
    """One exported artifact file: frontmatter + raw artifact body.

    ``artifact_name`` is the filename under which this body lives in the note
    folder (e.g. ``session-log.md`` when a profile renamed the summary). It
    flows into the frontmatter so Obsidian's Properties panel can show it.
    ``profile_id`` is added to the summary-note frontmatter ONLY when a
    profile matched (per the wave-A contract: callers that don't know whether
    the artifact is the summary MUST NOT pass it).

    Frontmatter is serialized with yaml.safe_dump — a title containing
    `: " [ {` must never produce broken YAML (the deterministic naming means
    we never parse it back, but Obsidian's Properties panel would still break).
    """
    fm: dict[str, object] = {
        "recording_id": rec.id,
        "title": rec.title,
        "created": _iso(rec.created_at, zone),
        "date": rec_timestamp(rec).astimezone(zone).date().isoformat(),
        "tags": _frontmatter_tags(rec.tags),
    }
    if rec.recorded_at is not None:
        fm["recorded_at"] = _iso(rec.recorded_at, zone)
    if rec.duration_sec is not None:
        fm["duration_sec"] = rec.duration_sec
    if artifact_name is not None:
        fm["artifact"] = artifact_name
    if profile_id is not None:
        fm["profile"] = profile_id

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
        # Tags are stored as a python list[str]; coerce defensively so a
        # None value (legacy row pre-tag column) never breaks the exporter.
        tags = list(rec.tags) if rec.tags else []
        return Rec(
            rec.id,
            rec.title or "",
            rec.created_at,
            rec.duration_sec,
            rec.state.value,
            tags=tags,
            type=rec.type,
            recorded_at=rec.recorded_at,
            sha256=rec.sha256,
        )


# Hidden per-recording subdir inside the exported folder: Obsidian skips
# dot-dirs, the audio FLAC + manifest live here after the storage→vault move.
HIDDEN_DIR = ".transcripter"
AUDIO_NAME = "audio.flac"
MANIFEST_NAME = "manifest.json"


def vault_audio_path(folder: Path) -> Path:
    """``<note-folder>/.transcripter/audio.flac`` — the vault-side audio
    location (worker regen + API streaming read it when storage is empty)."""
    return folder / HIDDEN_DIR / AUDIO_NAME

def vault_meta_dir(folder: Path) -> Path:
    """``<note-folder>/.transcripter/meta`` — the vault-side mirror of the
    pipeline's ``/storage/recordings/{id}/meta``. The export stage moves
    every meta artifact here once the recording is done; storage becomes
    disposable scratch (regenerate rehydrates from this mirror first)."""
    return folder / HIDDEN_DIR / "meta"


def resolve_meta_dir(c: object, rec_id: str) -> Path:
    """Where a recording's meta artifacts live RIGHT NOW: storage when the
    pipeline (or a rehydrate) put them there, else the vault mirror after
    the export move. Storage wins whenever it holds a transcript — partial
    trees (mid-pipeline) always resolve to storage. Vault scan only in
    vault mode (in storage mode path IS the storage-derived transcripts
    dir; scanning it would be a tautology)."""
    storage = Path(c.recordings_root) / rec_id / "meta"  # type: ignore[attr-defined]
    if (storage / "transcript.md").is_file() or (storage / "diarized-transcript.md").is_file():
        return storage
    vault_cfg = c.vault  # type: ignore[attr-defined]
    if getattr(vault_cfg, "mode", "storage") == "vault":
        scan_rec = Rec(rec_id, "", datetime.now(UTC), None)
        for folder in scan_recording_folders(vault_cfg.path, scan_rec):  # type: ignore[attr-defined]
            cand = vault_meta_dir(folder)
            if (cand / "transcript.md").is_file() or (cand / "diarized-transcript.md").is_file():
                return cand
    return storage


def move_meta_to_vault(meta: Path, folder: Path) -> int:
    """Copy-verify-unlink every file under ``meta`` into
    ``folder/.transcripter/meta/`` (same move semantics as the audio:
    nothing is removed from storage until the vault copy is verified by
    size). Idempotent per file — a retry skips already-moved pairs.
    Returns the number of files moved this run."""
    moved = 0
    mirror = vault_meta_dir(folder)
    for src in sorted(meta.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(meta)
        dst = mirror / rel
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            src.unlink()  # idempotent completion of a previous partial move
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            shutil.copyfile(src, tmp)
            if tmp.stat().st_size != src.stat().st_size:
                raise ExportError(
                    f"meta copy {rel} failed size verify; storage copy kept"
                )
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
        src.unlink()
        moved += 1
    # Prune the emptied storage tree (dirs only — every file is gone or
    # was already gone; a non-empty survivor keeps its dirs).
    for d in sorted(meta.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                break  # non-empty (partial move) — keep for the retry
    try:
        meta.rmdir()
    except OSError:
        pass
    return moved


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def move_audio_to_vault(src: Path, folder: Path, rec: Rec) -> Path | None:
    """Copy-verify-unlink the recording's FLAC into ``folder/.transcripter/``.

    The storage copy is the only copy until the vault copy is VERIFIED
    (sha256 against the catalog hash; size-only when the row has no hash —
    legacy rows). Returns the vault path on success/already-there, None when
    the storage copy is absent (already moved — idempotent). OSError (vault
    down mid-copy) propagates: run() turns it into a loud export error so
    backfill retries; nothing was unlinked."""
    dst = vault_audio_path(folder)
    if dst.is_file():
        if src.is_file():
            # Both sides present (partial previous move / user copied a
            # file in). Trust the vault copy only if it matches the
            # catalog hash; otherwise keep storage as the source of truth.
            if rec.sha256 and _sha256_of(dst) != rec.sha256:
                log.warning(
                    "export: vault audio for %s fails sha256; keeping storage copy",
                    rec.id,
                )
                return None
            src.unlink(missing_ok=True)
        return dst
    if not src.is_file():
        return None  # already moved (idempotent) or never uploaded
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{AUDIO_NAME}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(src, tmp)
        got = _sha256_of(tmp)
        if rec.sha256 and got != rec.sha256:
            raise ExportError(
                f"vault audio copy for {rec.id} failed sha256 verify "
                f"(catalog={rec.sha256[:12]}…, copy={got[:12]}…); storage copy kept"
            )
        if rec.sha256 is None and tmp.stat().st_size != src.stat().st_size:
            raise ExportError(
                f"vault audio copy for {rec.id} failed size verify; storage copy kept"
            )
        os.replace(tmp, dst)  # atomic on POSIX: no torn audio in the vault
    finally:
        tmp.unlink(missing_ok=True)
    src.unlink()
    log.info("export: moved audio for %s to vault (%d bytes)", rec.id, dst.stat().st_size)
    return dst


def write_manifest(folder: Path, rec: Rec, zone: ZoneInfo) -> Path:
    """The self-contained recording description beside the audio — the
    future import base (id/sha/dates/title/tags/type), refreshed on every
    export so title/tag edits surface here too."""
    doc: dict[str, object] = {
        "id": rec.id,
        "sha256": rec.sha256,
        "title": rec.title,
        "created_at": _iso(rec.created_at, zone),
        "recorded_at": _iso(rec.recorded_at, zone) if rec.recorded_at else None,
        "duration_sec": rec.duration_sec,
        "tags": rec.tags,
        "type": rec.type,
        "audio": AUDIO_NAME,
    }
    path = folder / HIDDEN_DIR / MANIFEST_NAME
    write_note_atomic(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return path


def check_sentinel(root: Path, sentinel: str) -> None:
    """Boot-race guard: a bind mount over an empty mountpoint is writable but
    wrong. With vault.sentinel set (e.g. ".transcripter"), refuse to export
    unless it exists under the root."""
    if sentinel and not (root / sentinel).exists():
        raise ExportError(
            f"vault sentinel {sentinel!r} missing under {root} — "
            "export dir looks wrong (empty mountpoint?); refusing to write"
        )


def _folder_pattern(rec: Rec) -> re.Pattern[str]:
    """Match app-scheme folders for this recording: `{ts} {anything} {id8}`.

    Timestamp is derived at call time (TZ/recorded_at may change between
    exports), so the pattern pins only the id8 suffix — the same property the
    old flat-note sweep relied on."""
    return re.compile(
        rf"^\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}} .+ {re.escape(rec.id[:8].lower())}$"
    )


def _whitelist(profiles_dir: Path | str | None) -> frozenset[str]:
    """Effective mirror-delete + app-file whitelist for an export run.

    Static base (transcript/diarized/summary) plus every known profile's
    output_artifact. ``profiles_dir=None`` (legacy callers, tests that don't
    care about profiles) collapses to the static three.
    """
    if profiles_dir is None:
        return frozenset(ARTIFACTS)
    from .profiles import artifacts_for_export

    return artifacts_for_export(profiles_dir)


def _is_app_file(name: str, whitelist: frozenset[str]) -> bool:
    """Names the exporter itself can create inside a recording folder:
    artifacts, their `.{name}.lock` fences and `.{name}.{uuid8}.tmp` staging,
    plus the hidden `.transcripter/` subdir (audio + manifest).

    Takes the run's resolved whitelist (``_whitelist``) — callers compute it
    ONCE per export run; resolving per call would re-read and re-validate
    every profile yaml for every folder child.
    """
    if name == HIDDEN_DIR or name in whitelist:
        return True
    return any(
        name == f".{a}.lock" or (name.startswith(f".{a}.") and name.endswith(".tmp"))
        for a in whitelist
    )


def _safe_iterdir(path: Path) -> list[Path]:
    """iterdir that an NFS hiccup can't kill — unreadable dir reads as
    empty (its contents then classify as app files; the sweep's rmtree
    still catches real trouble)."""
    try:
        return list(path.iterdir())
    except OSError as e:
        log.warning("export scan: unreadable dir %s: %s", path, e)
        return []


def export_recording(
    root: Path,
    meta: Path,
    rec: Rec,
    zone: ZoneInfo,
    sentinel: str = "",
    profiles_dir: Path | str | None = None,
    audio_src: Path | None = None,
) -> Path | None:
    """Export one recording's folder; None when there is nothing to export.

    - Move: an existing folder for this recording under a previous title or
      legacy layout (root-level flat) is moved to the current nested
      deterministic name (os.rename, same filesystem) — Obsidian edits and
      user files survive. FileNotFoundError from a concurrent export having
      already moved it is a no-op; other OSError propagates (next export /
      backfill recovers).
    - Regenerate: artifact files are rewritten atomically in place.
    - Mirror: known artifact names absent from meta are unlinked from the
      folder (e.g. diarize disabled → diarized-transcript.md must not go
      stale in the vault); unknown/user files are never touched.
    - Profile (Phase 0): the summary artifact is renamed to
      ``profile.summarize.output_artifact`` in the note folder when a
      profile matches the recording's TYPE (routing by recording.type;
      tags are user grouping now). Meta stays canonical
      (``meta/summary.md``). Whitelist = static 3 + every known profile's
      output_artifact; a removed profile's note falls out of the whitelist
      and is deliberately kept as user content (manual cleanup). Re-match
      happens here too (D11): profile edits between runs take effect on
      the next export.
    - Audio move: with ``audio_src`` set (the storage FLAC), the file is
      copy-verify-unlinked into ``folder/.transcripter/audio.flac`` and the
      manifest refreshed — AFTER the notes, so a failure leaves a fully
      valid note folder plus the original audio in storage (the next
      export/backfill retries the move).
    """
    check_sentinel(root, sentinel)
    if not (meta / "transcript.md").is_file() and not (meta / "diarized-transcript.md").is_file():
        return None

    # A fresh export dir may not exist yet (no vault → ./storage/transcripts,
    # first export ever). The move-scan below must not crash on it —
    # write_note_atomic creates parents for the files.
    root.mkdir(parents=True, exist_ok=True)
    whitelist = _whitelist(profiles_dir)
    target = _rename_to_target(root, rec, zone, whitelist)
    if target is None:
        target = folder_path(root, rec, zone)

    # Re-match profile per D11 (the meta canonical file is summary.md; the
    # note-folder filename for the summary becomes profile.output_artifact
    profile = None
    summary_target = "summary.md"
    if profiles_dir is not None:
        from .profiles import match_profile_by_type

        profile = match_profile_by_type(rec.type, profiles_dir)
        if profile is not None:
            summary_target = profile.summarize.output_artifact

    # Static pair: meta name → target name (same for transcript/diarized).
    # Summary row is dynamic (profile renaming).
    plan: list[tuple[str, str, str | None]] = [
        ("transcript.md", "transcript.md", None),
        ("diarized-transcript.md", "diarized-transcript.md", None),
        (
            "summary.md",
            summary_target,
            profile.id if profile is not None else None,
        ),
    ]

    written = False
    for src_name, target_name, profile_id in plan:
        src = meta / src_name
        if src.is_file():
            write_note_atomic(
                target / target_name,
                build_artifact(
                    src,
                    rec,
                    zone,
                    profile_id=profile_id,
                    artifact_name=target_name,
                ),
            )
            written = True
        else:
            # Mirror: a regenerate that dropped the artifact (diarize disabled,
            # summarize skipped) must not leave it stale in the vault.
            stale = target / target_name
            if stale.exists():
                stale.unlink()
                # The lockfile stays: unlinking it while a concurrent writer
                # holds flock on that inode lets two writers lock different
                # inodes (the permanence invariant write_note_atomic relies on).
    # Mirror-delete any other names in the whitelist that the current run did
    # not write — e.g. a previous run's profile.output_artifact after the
    # profile was removed from disk (the renamed note would otherwise linger
    # forever in the vault).
    written_names = {target_name for _, target_name, _ in plan}
    for name in whitelist - written_names:
        stale = target / name
        if stale.exists():
            stale.unlink()
            # Lockfile stays (same permanence invariant as above).
    if not written:
        return None
    if audio_src is not None:
        move_audio_to_vault(audio_src, target, rec)
        write_manifest(target, rec, zone)
    return target


def rename_folder(root: Path, rec: Rec, zone: ZoneInfo, sentinel: str = "") -> Path | None:
    """Rename-only: move the recording's folder to its current-title name
    WITHOUT rewriting any files inside (Obsidian edits are sacred here —
    frontmatter title inside files goes stale until the next regenerate).

    Returns the folder path, or None when nothing was ever exported."""
    check_sentinel(root, sentinel)
    if not root.is_dir():
        return None
    return _rename_to_target(root, rec, zone, _whitelist(None))


def _rename_to_target(
    root: Path, rec: Rec, zone: ZoneInfo, whitelist: frozenset[str]
) -> Path | None:
    """Move an existing folder for this recording (any title, nested or
    legacy flat) to the current deterministic nested name.

    Returns the target path when the folder exists (moved or already in
    place), None when no folder for this recording exists at all. With
    multiple matches (double-rename race leftover) prefers the folder holding
    non-app (user-authored) files — edits are the only content that cannot be
    regenerated from meta; the app-only leftovers are swept after export."""
    target = folder_path(root, rec, zone)
    if target.is_dir():
        return target
    matches = scan_recording_folders(root, rec)
    matches.sort(
        key=lambda e: not any(not _is_app_file(c.name, whitelist) for c in _safe_iterdir(e))
    )
    for entry in matches[:1]:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(entry, target)
            log.info("export: moved folder %s -> %s", entry, target)
        except FileNotFoundError:
            # Concurrent export already moved it — same deterministic
            # target, nothing left to do.
            pass
    return target if target.is_dir() else None


def sweep_stale_notes(
    root: Path, rec: Rec, keep: Path, whitelist: frozenset[str]
) -> None:
    """Delete stale app-scheme vault entries for this recording, keeping `keep`:

    - legacy flat notes (pre-folder scheme) `* {id8}.md` + lockfiles — the
      migration path; scanned at the vault root AND inside YYYY/MM (notes
      were always root-level, but the scan is cheap and total);
    - old-title/old-layout folders orphaned by a double-rename race —
      removed recursively, but ONLY when they contain no non-app files: a
      folder with anything user-authored (a file without an app
      artifact/lock/tmp name) is left alone and logged, never destroyed.

    Scoped to the app's own naming scheme (`YYYY-MM-DD_HH-MM ` prefix AND
    ` {id8}` suffix) — user-authored notes/folders are never matched.
    Best-effort: an entry that can't be removed (NFS hiccup, permissions)
    is skipped, it never fails a successful export."""
    flat = re.compile(
        rf"^\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}} .+ {re.escape(rec.id[:8].lower())}\.md$"
    )
    for parent in _year_month_dirs(root):
        for entry in _safe_iterdir(parent):
            if entry == keep or not entry.is_file() or not flat.match(entry.name):
                continue
            try:
                entry.unlink()
                # Lockfiles (.{name}.lock) are permanent per write_note_atomic
                # and never match the sweep regex themselves.
                entry.with_name(f".{entry.name}.lock").unlink(missing_ok=True)
                log.info("export: migrated legacy flat note %s", entry.name)
            except OSError as e:
                log.warning("export sweep: could not remove legacy note %s: %s", entry, e)
    for entry in scan_recording_folders(root, rec):
        if entry == keep:
            continue
        if any(not _is_app_file(child.name, whitelist) for child in _safe_iterdir(entry)):
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


# --- Dashboard MOC -----------------------------------------------------------

DASHBOARD_NAME = "Dashboard.md"


def write_dashboard(root: Path, recs: list[Rec], zone: ZoneInfo) -> Path | None:
    """(Re)generate ``<vault>/Dashboard.md`` — the vault's map of content:
    reverse-chronological months with wikilinks to every exported recording,
    plus per-tag sections. Best-effort standalone overview; the per-recording
    folders stay the source of truth."""
    if not recs:
        return None
    by_month: dict[str, list[Rec]] = {}
    by_tag: dict[str, list[Rec]] = {}
    for rec in recs:
        ts = rec_timestamp(rec).astimezone(zone)
        by_month.setdefault(f"{ts:%Y/%m}", []).append(rec)
        for tag in rec.tags or ["untagged"]:
            by_tag.setdefault(tag, []).append(rec)

    def _link(rec: Rec) -> str:
        name = folder_name(rec.title, rec.id, rec_timestamp(rec), zone)
        return f"- [[{name}/{name}|{rec.title or 'call'}]]"

    lines = [
        "---",
        "tags: [transcripter/dashboard]",
        "---",
        "",
        "# Transcripter Dashboard",
        "",
        "> Auto-generated by transcripter; user edits are overwritten on the",
        "> next export. Browse by month below or via tags.",
        "",
    ]
    for month in sorted(by_month, reverse=True):
        lines.append(f"## {month}")
        lines.extend(_link(r) for r in sorted(by_month[month], key=rec_timestamp, reverse=True))
        lines.append("")
    lines.append("## Tags")
    for tag in sorted(by_tag):
        lines.append(f"### #{tag}")
        lines.extend(_link(r) for r in sorted(by_tag[tag], key=rec_timestamp, reverse=True))
        lines.append("")
    path = root / DASHBOARD_NAME
    write_note_atomic(path, "\n".join(lines))
    return path


def run(rec_id: str, rename_only: bool = False) -> Path | None:
    """Load config + recording, no-op unless done, export.

    rename_only=True is the PATCH-rename path: the folder is moved to the
    new-title name and NOTHING inside it is rewritten (Obsidian edits are
    sacred); full exports (pipeline finally / regenerate / backfill) always
    run with rename_only=False and refresh the artifact files.

    Shared by the export_once CLI (subprocess) and unit tests. The CLI runs
    in a SUBPROCESS spawned (and SIGKILL-abandoned on timeout) by the
    export_transcript activity / backfill — a dead NAS mount parks the child
    in D-state, which no in-process exception handling can survive.

    Returns the folder path (or None on no-op).
    """
    from .config import load_config
    from .db import init_engine

    cfg = load_config()
    init_engine(cfg.database.url)
    zone = configured_zone()
    rec = load_recording(rec_id)
    if rec.state != "done":
        return None
    profiles_dir = getattr(cfg, "profiles", None)
    profiles_dir = profiles_dir.path if profiles_dir is not None else None
    if rename_only:
        return rename_folder(cfg.vault.path, rec, zone, cfg.vault.sentinel)
    meta = resolve_meta_dir(cfg, rec_id)
    audio = cfg.recordings_root / rec_id / "audio.flac"
    path = export_recording(
        cfg.vault.path,
        meta,
        rec,
        zone,
        cfg.vault.sentinel,
        profiles_dir=profiles_dir,
        audio_src=audio,
    )
    if path is not None:
        # Vault mode: storage is scratch — carry the meta tree into the
        # vault mirror and drop what remains of the storage dir. Only when
        # the meta actually lived in storage (a re-export after a
        # rehydrate reads the vault mirror directly; moving it "into
        # itself" would unlink the vault copy as already-moved).
        storage_meta = cfg.recordings_root / rec_id / "meta"
        if (
            getattr(cfg.vault, "mode", "storage") == "vault"
            and meta == storage_meta
            and storage_meta.is_dir()
        ):
            moved = move_meta_to_vault(storage_meta, path)
            if moved:
                log.info("export: moved %d meta files for %s to vault", moved, rec_id)
        # With the meta tree gone there is nothing app-owned left in the
        # storage dir (audio moved earlier); drop the dir itself — storage
        # is scratch in vault mode. rmdir: refuses a non-empty dir (a
        # stranded file keeps its dir; backfill retries).
        rec_dir = cfg.recordings_root / rec_id
        try:
            rec_dir.rmdir()
        except OSError:
            pass
        sweep_stale_notes(cfg.vault.path, rec, path, _whitelist(profiles_dir))
        _refresh_dashboard(cfg, zone)
    return path


def _refresh_dashboard(cfg: object, zone: ZoneInfo) -> None:
    """Dashboard refresh after a successful export; best-effort — a vault
    hiccup here must not fail the export (the next export retries)."""
    from .db import Recording, RecordingState, session

    try:
        with session() as s:
            rows = (
                s.query(Recording)
                .filter(Recording.state == RecordingState.done)
                .order_by(Recording.created_at)
                .all()
            )
            recs = [
                Rec(
                    r.id,
                    r.title or "",
                    r.created_at,
                    r.duration_sec,
                    r.state.value,
                    tags=list(r.tags) if r.tags else [],
                    type=r.type,
                    recorded_at=r.recorded_at,
                    sha256=r.sha256,
                )
                for r in rows
            ]
        write_dashboard(cfg.vault.path, recs, zone)  # type: ignore[attr-defined]
    except Exception:
        log.exception("export: dashboard refresh failed (skipped)")
