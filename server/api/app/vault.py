"""Shared Phase 3 scan helpers: tag timelines + the vault overview.

Both endpoints read the SAME two sources — the Postgres catalog (which
recordings carry a tag, and when) and per-recording ``meta/events.json``
artifacts written by the worker's enrich stage — so the querying lives
here once and is imported by the /tags and /vault routers. No Neo4j
access: timelines and the vault must render with the graph profile off
(the events.json artifact survives a graph outage the same way the
digest note does).

State filter asymmetry is deliberate: the timeline lists DONE recordings
only (a half-uploaded capture has no events yet), while the vault counts
recordings in ANY state — same precedent as GET /tags, where a tag on an
uploading capture is real user intent.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import ServerConfig
from app.db import Recording, RecordingState

_LOG = logging.getLogger("transcripter.api.vault")

# Skip oversized files instead of yaml-parsing garbage/oddball notes.
# (Moved here from routes/tags.py so the digest scan below and the
# get_digest endpoint walk the same files with the same rules.)
_MAX_DIGEST_BYTES = 1024 * 1024

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# Vault entity list cap: a tag page renders a panel, not an inventory;
# past this many distinct names the UI needs search, not a longer list.
_ENTITY_CAP = 200

# App-scheme recording folder pattern: `{ts} {anything} {id8}` — pins the
# id8 suffix only (title/TZ may change). EXACT twin of the worker's
# export._folder_pattern; the layouts it must find (root-level flat legacy,
# nested YYYY/MM current) mirror worker.export.scan_recording_folders.
_FOLDER_RE_CACHE: dict[str, re.Pattern[str]] = {}


def folder_pattern(recording_id: str) -> re.Pattern[str]:
    pat = _FOLDER_RE_CACHE.get(recording_id)
    if pat is None:
        pat = re.compile(
            rf"^\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}} .+ {re.escape(recording_id[:8].lower())}$"
        )
        _FOLDER_RE_CACHE[recording_id] = pat
    return pat


def scan_recording_folders(cfg: ServerConfig, recording_id: str) -> list[Path]:
    """The recording's app-scheme folder(s) in the vault, nested YYYY/MM or
    legacy root-level flat. Best-effort: unreadable dirs are skipped."""
    pattern = folder_pattern(recording_id)
    found: list[Path] = []
    root = cfg.vault.path
    try:
        parents: list[Path] = [root]
        for year_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if year_dir.name.isdigit() and len(year_dir.name) == 4:
                parents.append(year_dir)
                parents.extend(m for m in year_dir.iterdir() if m.is_dir())
        for parent in parents:
            found.extend(e for e in parent.iterdir() if e.is_dir() and pattern.match(e.name))
    except OSError as exc:
        _LOG.warning("vault: scan failed under %s: %s", root, exc)
    return sorted(found)


def vault_audio(cfg: ServerConfig, recording_id: str) -> Path | None:
    """The recording's FLAC in the vault (``<folder>/.transcripter/audio.flac``)
    when present — the fallback after the export stage moved it out of
    storage. None when the vault holds no copy."""
    for folder in scan_recording_folders(cfg, recording_id):
        candidate = folder / ".transcripter" / "audio.flac"
        if candidate.is_file():
            return candidate
    return None

def vault_meta_artifact(cfg: ServerConfig, recording_id: str, rel: str) -> Path | None:
    """A meta artifact (``transcript.md``, ``events.json``, …) from the
    vault mirror (``<folder>/.transcripter/meta/``) when the storage copy
    is gone — the read-side counterpart of the worker's meta move. None
    when the vault holds no copy (never-generated artifact)."""
    for folder in scan_recording_folders(cfg, recording_id):
        candidate = folder / ".transcripter" / "meta" / rel
        if candidate.is_file():
            return candidate
    return None


def delete_recording_folders(cfg: ServerConfig, recording_id: str) -> list[Path]:
    """Remove the recording's exported folder(s) from the vault — notes,
    hidden .transcripter/ (audio + manifest), everything. The DELETE
    endpoint's vault-side counterpart; returns what was removed. Folder
    content is entirely app-owned (deterministic id8 match), so no
    user-file guard applies here — unlike the export sweep."""
    import shutil

    removed: list[Path] = []
    for folder in scan_recording_folders(cfg, recording_id):
        try:
            shutil.rmtree(folder)
            removed.append(folder)
        except OSError as exc:
            _LOG.warning("vault: could not remove %s: %s", folder, exc)
    return removed

def find_digest(cfg: ServerConfig, tag: str) -> Path | None:
    """First ``*.md`` under ``<transcripts>/digests/`` whose frontmatter
    ``tag:`` equals ``tag`` (sorted, first match wins).

    The worker names files by slug, so the API cannot reconstruct the
    filename from the raw tag — every note is checked for frontmatter
    whose ``tag:`` matches the normalized tag. Extracted from
    routes/tags.py get_digest (Phase 1) so the vault's ready/stale/none
    computation reuses one regex, one size guard, one matching order.
    """
    digests = cfg.vault.path / "digests"
    if not digests.is_dir():
        return None
    for md in sorted(digests.glob("*.md")):
        try:
            if md.stat().st_size > _MAX_DIGEST_BYTES:
                continue
            m = _FRONTMATTER_RE.match(md.read_text(encoding="utf-8"))
        except OSError:
            continue  # unreadable/racy file — not this tag's problem
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue  # malformed frontmatter — skip, don't 500
        if isinstance(fm, dict) and fm.get("tag") == tag:
            return md
    return None


def _naive_utc(dt: datetime) -> datetime:
    """Normalize to naive UTC (pass naive through).

    The catalog stores naive-UTC timestamps (plain TIMESTAMP columns),
    but round-trips through some backends and ``datetime.now(UTC)``
    defaults can hand back the aware flavor; comparing mixed flavors
    raises TypeError."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _read_events_json(cfg: ServerConfig, recording_id: str) -> dict | None:
    """Parse ``<recordings>/<id>/meta/events.json``; None when absent or
    garbage. A missing file is NORMAL (only enriched recordings have
    one) so it stays silent; a present-but-unparseable file is logged."""
    path = cfg.recordings_root / recording_id / "meta" / "events.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Vault mode: the meta tree moved into the vault mirror.
        mirror = vault_meta_artifact(cfg, recording_id, "events.json")
        if mirror is None:
            return None
        try:
            raw = mirror.read_text(encoding="utf-8")
        except OSError as exc:
            _LOG.warning("vault: unreadable events.json for %s: %s", recording_id, exc)
            return None
    except OSError as exc:
        _LOG.warning("vault: unreadable events.json for %s: %s", recording_id, exc)
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOG.warning("vault: malformed events.json for %s: %s", recording_id, exc)
        return None
    if not isinstance(doc, dict):
        _LOG.warning("vault: events.json for %s is not an object", recording_id)
        return None
    return doc


def _clean_events(recording_id: str, doc: dict | None) -> list[dict]:
    """The per-session events list in the client contract shape
    (``{ts, kind, summary, mentions}``); non-dict entries are dropped."""
    if doc is None:
        return []
    events = doc.get("events")
    if not isinstance(events, list):
        _LOG.warning("vault: events.json for %s has no events list", recording_id)
        return []
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        mentions = ev.get("mentions")
        out.append(
            {
                "ts": ev.get("ts", ""),
                "kind": ev.get("kind", ""),
                "summary": ev.get("summary", ""),
                "mentions": mentions if isinstance(mentions, list) else [],
            }
        )
    return out


def _entity_count(doc: dict | None) -> int:
    entities = doc.get("entities") if doc else None
    return len(entities) if isinstance(entities, list) else 0


def _tag_recordings(session: Session, tag: str) -> list[Recording]:
    """The tag's DONE recordings, newest first by
    ``coalesce(recorded_at, created_at) DESC`` (id DESC tiebreak).
    Dialect split mirrors worker digest ``_select_recordings``: Postgres
    matches the TEXT[] with @>, SQLite explodes the JSON array with
    json_each (tests)."""
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        tag_filter = Recording.tags.contains([tag])
    else:
        tag_json = tag.replace("\\", "\\\\").replace('"', '\\"')
        tag_filter = text(
            f"EXISTS (SELECT 1 FROM json_each(recordings.tags) "
            f"WHERE value = '{tag_json}')"
        )
    return (
        session.query(Recording)
        .filter(
            Recording.state == RecordingState.done,
            tag_filter,
        )
        .order_by(
            func.coalesce(Recording.recorded_at, Recording.created_at).desc(),
            Recording.id.desc(),
        )
        .all()
    )


def _aggregate_entities(rows: list[tuple[datetime, dict | None]]) -> list[dict]:
    """Aggregate entities across one tag's events.json files.

    A slug counts as present in a file when it appears in that file's
    entities[] OR in any event's mentions; ``sessions`` is the number of
    distinct recordings, ``last_seen`` the newest recording date among
    them. ``rows`` arrives newest-first, so the first label/type seen
    for a slug is the freshest one (a slug mentioned without an entity
    entry falls back to the slug itself / empty type). Sorted last_seen
    DESC then slug ASC, capped at ``_ENTITY_CAP``."""
    sessions_count: dict[str, int] = {}
    last_seen: dict[str, datetime] = {}
    labels: dict[str, tuple[str, str]] = {}
    for date, doc in rows:
        if doc is None:
            continue
        present: set[str] = set()
        entities = doc.get("entities")
        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                slug = ent.get("slug")
                if not isinstance(slug, str) or not slug:
                    continue
                present.add(slug)
                if slug not in labels:  # newest file wins — never overwrite
                    label = ent.get("label")
                    etype = ent.get("type")
                    labels[slug] = (
                        label if isinstance(label, str) else slug,
                        etype if isinstance(etype, str) else "",
                    )
        events = doc.get("events")
        if isinstance(events, list):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                mentions = ev.get("mentions")
                if not isinstance(mentions, list):
                    continue
                present.update(s for s in mentions if isinstance(s, str) and s)
        for slug in present:
            sessions_count[slug] = sessions_count.get(slug, 0) + 1
            if slug not in last_seen or date > last_seen[slug]:
                last_seen[slug] = date
    out = sorted(last_seen, key=lambda slug: slug)
    out.sort(key=lambda slug: last_seen[slug], reverse=True)
    return [
        {
            "slug": slug,
            "label": labels.get(slug, (slug, ""))[0],
            "type": labels.get(slug, (slug, ""))[1],
            "sessions": sessions_count[slug],
            "last_seen": last_seen[slug].isoformat(),
        }
        for slug in out[:_ENTITY_CAP]
    ]


def _digest_state(cfg: ServerConfig, tag: str, newest: datetime) -> str:
    """Vault digest semantics for a tag: ``ready`` (note exists),
    ``stale`` (note mtime older than the tag's newest recording date),
    ``none`` (no note)."""
    md = find_digest(cfg, tag)
    if md is None:
        return "none"
    try:
        mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC).replace(tzinfo=None)
    except OSError as exc:
        _LOG.warning("vault: digest for %s vanished during stat: %s", tag, exc)
        return "none"
    return "ready" if mtime >= newest else "stale"


def scan_timeline(cfg: ServerConfig, session: Session, tag: str) -> dict:
    """The frozen GET /tags/{tag}/timeline payload (normalized ``tag``)."""
    sessions: list[dict] = []
    agg_rows: list[tuple[datetime, dict | None]] = []
    for rec in _tag_recordings(session, tag):
        date = _naive_utc(rec.recorded_at or rec.created_at)
        doc = _read_events_json(cfg, rec.id)
        sessions.append(
            {
                "recording_id": rec.id,
                "title": rec.title,
                "date": date.isoformat(),
                "type": rec.type,
                "duration_sec": rec.duration_sec,
                "events": _clean_events(rec.id, doc),
                "entity_count": _entity_count(doc),
            }
        )
        agg_rows.append((date, doc))
    return {
        "tag": tag,
        "sessions": sessions,
        "entities": _aggregate_entities(agg_rows),
        "digest_generated": find_digest(cfg, tag) is not None,
    }


def scan_vault(cfg: ServerConfig, session: Session) -> list[dict]:
    """The frozen GET /vault items — one entry per distinct free tag.

    Aggregates the whole catalog in one pass (a vault is a personal
    library: hundreds of rows, not millions — grouping in Python keeps a
    single dialect-independent query; the semantics are the SQL one:
    distinct tags + counts + max(coalesce(recorded_at, created_at))).
    Recordings with no tags are skipped (vault = free tags only); a tag
    counts recordings in ANY state. Digest staleness compares the note's
    mtime to the tag's newest recording date. Ordering: last_activity
    DESC, tag ASC."""
    rows = session.execute(
        select(
            Recording.id,
            Recording.tags,
            func.coalesce(Recording.recorded_at, Recording.created_at),
        )
    ).all()
    per_tag: dict[str, dict] = {}
    for rid, tags, raw_date in rows:
        date = _naive_utc(raw_date)
        for tag in tags or []:
            if not tag:
                continue
            entry = per_tag.get(tag)
            if entry is None:
                entry = per_tag[tag] = {"sessions": 0, "last_activity": date, "recs": []}
            entry["sessions"] += 1
            entry["recs"].append((date, _read_events_json(cfg, rid)))
            entry["last_activity"] = max(entry["last_activity"], date)
    items = []
    for tag, entry in per_tag.items():
        # Newest-first so entity labels come from the freshest file.
        entry["recs"].sort(key=lambda row: row[0], reverse=True)
        items.append(
            {
                "tag": tag,
                "sessions": entry["sessions"],
                "entities": len(_aggregate_entities(entry["recs"])),
                "last_activity": entry["last_activity"].isoformat(),
                "digest": _digest_state(cfg, tag, entry["last_activity"]),
            }
        )
    items.sort(key=lambda it: it["tag"])
    items.sort(key=lambda it: it["last_activity"], reverse=True)
    return items
