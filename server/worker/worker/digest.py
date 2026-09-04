"""Tag digest (wave C): build a per-tag knowledge-graph digest.

Pipeline:

1. Postgres: pull the last N *done* recordings tagged with ``tag`` (already
   normalized to lower-case by the recording API) — the empty selection is a
   non-error "nothing to digest" path that the activity returns as
   ``{written: false, reason: ...}``.
2. Neo4j: collect a compact view of those recordings' nodes and events
   (entities grouped by label/type; events one per origin recording).
3. LLM: a single chat call (same ``cfg.summarize`` endpoint as summarize and
   enrich) — the prompt asks for a markdown digest in the same language as
   the source. ``response_format`` is deliberately not used: the output is
   free-form markdown for an Obsidian note, not structured JSON.
4. Filesystem: ``<transcripts_root>/digests/<safe-tag>.md`` written via
   unique-tmp + ``os.replace`` (the export.py atomic-write pattern). Tag
   sanitization happens BEFORE the LLM call too, so a bad tag fails loudly
   instead of leaving the user wondering why no file appeared.

Frontmatter (YAML) carries the inputs needed to retrace the digest later
in an Obsidian Properties panel: tag, generation timestamp, the recording
ids it covers, and the count.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from neo4j import GraphDatabase
from sqlalchemy import or_, text

from .db import Recording, RecordingState, Stage, session
from .enrich import slugify
from .llm_payload import system_first_messages

log = logging.getLogger("transcripter.digest")

# Tag NAME (the user-facing free tag): Phase 0 loosened it from the old
# ASCII file-safe pattern to allow spaces, dots, underscores, dashes and
# ANY Unicode letters/digits (\w with re.UNICODE — Cyrillic etc.). First
# char must not be a space; ≤64 chars so the slug below always fits a
# filename segment. EXACT TWIN of the API's _TAG_RE (routes/tags.py) —
# change the two IN SYNC or the API would 202 a tag the worker rejects.
_SAFE_TAG_RE = re.compile(r"^[\w][ \w.-]{0,63}$", re.UNICODE)

# HTTP budget kept 30 s UNDER the Temporal start_to_close (2400 s) so a
# httpx.ReadTimeout fires (a plain Exception → activity raised) before
# Temporal cancels the activity (CancelledError bypasses except-Exception
# and leaves the workflow hanging). Same shape as summarize / enrich.
_HTTP_TIMEOUT_SEC = 2370.0

# Reuse the per-tag Cypher query: collect entities (deduped across the
# selection, so we know what's been mentioned over the last N sessions)
# and events (one row per recording so the digest can be per-session).
# Label-agnostic: profiles may override node labels (EnrichNodeLabels),
# so we select on PROPERTIES present on every node (tag + provenance)
# instead of `:Entity OR :Event`. Window membership: events are
# per-recording (origin only); shared entities qualify via their
# recording_ids list. MENTIONS edges are excluded — the per-session event
# text already conveys mentions; emitting them dilutes the prompt.
_DIGEST_CYPHER = """
MATCH (n)
WHERE n.tag = $tag
  AND (n.origin_recording_id IN $rec_ids
       OR any(x IN coalesce(n.recording_ids, []) WHERE x IN $rec_ids))
OPTIONAL MATCH (n)-[r]->(m)
WHERE m.tag = $tag AND type(r) <> 'MENTIONS'
  AND (m.origin_recording_id IN $rec_ids
       OR any(x IN coalesce(m.recording_ids, []) WHERE x IN $rec_ids))
RETURN
  n.tag AS tag,
  n.origin_recording_id AS origin,
  coalesce(n.recording_ids, []) AS rec_ids_all,
  n.slug AS slug,
  n.label AS label,
  n.type AS type,
  n.kind AS kind,
  n.ts AS ts,
  n.summary AS summary,
  type(r) AS rel_type,
  m.label AS rel_label,
  m.slug AS rel_slug
"""

# Reading-only access keeps the activity honest: a hung llama-server is
# the only realistic long pole; the graph is just a query.
_DIGEST_PROMPT_HEADER = (
    "You are a knowledge-base assistant for a recurring series of sessions.\n"
    "Below is the structured context (entities and events extracted from "
    "earlier sessions) for tag: {tag}.\n"
    "Produce a markdown digest of the last {last_n} sessions. Structure:\n"
    "1. One-paragraph overview.\n"
    "2. Recurring entities (people, places, items) seen across sessions.\n"
    "3. Per-session timeline (newest first): one bullet list of notable "
    "events per recording.\n"
    "4. Entity updates: for entities with state_change events, one line "
    "each — what changed for whom. Skip when none.\n"
    "5. Open threads / unresolved questions if any.\n"
    "{language_directive}"
    "Markdown only, no frontmatter — the host prepends its own.\n\n"
    "---\n\n"
    "Tag: {tag}\n"
    "Sessions: {sessions} (count: {count})\n"
    "Events (session title — date — kind — ts — summary):\n{events}\n\n"
    "Relations (from — rel — to):\n{relations}\n"
)

# Language directive variants for _render_prompt. The STT-detected code
# ('ru', 'en'…) maps to a BCP-47-ish name the model understands; the
# dominant language of the selection wins (Counter over non-None rows —
# 2026-09-04: the LLM ignored the soft "same language as the material"
# line and wrote English digests over Russian sessions).
_LANGUAGE_NAMES = {
    "ru": "Russian",
    "en": "English",
    "uk": "Ukrainian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "kk": "Kazakh",
    "be": "Belarusian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
    "hi": "Hindi",
    "tr": "Turkish",
}

# Soft fallback when no row carries a usable STT language code.
_LANGUAGE_DIRECTIVE_FALLBACK = (
    "Write in the same language as the session material. "
)


# ---------- public dataclasses --------------------------------------------------


@dataclass(frozen=True)
class DigestRow:
    """One Postgres row the digest feeds on."""

    recording_id: str
    title: str
    created_at: datetime
    # Timeline key: coalesce(recorded_at, created_at) — the date the
    # session HAPPENED (an import backdate), falling back to catalog time.
    recording_date: datetime
    # STT-detected language ('ru', 'en'…; None/'' /'unknown' normalize to
    # None). Feeds the digest prompt's explicit language directive
    # (2026-09-04: the LLM ignored the soft "same language as the
    # material" line and wrote English digests over Russian sessions).
    language: str | None = None


@dataclass(frozen=True)
class DigestGraphSlice:
    """Structured view of the live graph for the selected recordings."""

    entities: list[dict[str, object]]
    events: list[dict[str, object]]
    relations: list[dict[str, object]]


@dataclass(frozen=True)
class DigestInput:
    """Inputs handed to the LLM (prompt is built from this on the wire)."""

    tag: str
    last_n: int
    rows: list[DigestRow]
    graph: DigestGraphSlice


# ---------- helpers -------------------------------------------------------------


def _stage_language(s: Any, rec_ids: list[str]) -> dict[str, str | None]:
    """STT language per recording id, from the transcribe stage's
    ``details`` JSON (written by the transcribe activity). One query for
    the whole selection. Values are normalized: missing details,
    non-string values, ``''`` and ``'unknown'`` all map to None — a
    None language degrades the digest prompt to the soft
    "same language as the material" line instead of a wrong directive.
    """
    out: dict[str, str | None] = dict.fromkeys(rec_ids, None)
    for st in (
        s.query(Stage)
        .filter(Stage.recording_id.in_(rec_ids), Stage.kind == "transcribe")
        .all()
    ):
        lang = st.details.get("language") if isinstance(st.details, dict) else None
        if not (isinstance(lang, str) and lang.strip() and lang.strip() != "unknown"):
            continue  # missing / empty / 'unknown' → stays None
        out[st.recording_id] = lang.strip()
    return out


def safe_filename(tag: str) -> str:
    """Return the digest FILENAME for ``tag``: a Unicode-aware slug +
    ``.md``. The old contract used the raw tag as the filename, which the
    Phase 0 free tags (spaces, Cyrillic) forbid — now the display tag
    goes to frontmatter ``tag:`` verbatim and only the slug hits the
    filesystem (spaces → dashes, casefolded, same mapping enrich uses
    for entity slugs; "Мой Замок" → "мой-замок.md").

    The API already rejects bad tags with 400, so the regex guard only
    fires if something internal calls with a non-normalized value —
    failing loudly keeps a bad path out of the user's transcripts dir
    instead of dropping a file the user can't trace back.
    """
    if not _SAFE_TAG_RE.match(tag):
        raise ValueError(
            f"tag {tag!r} is not file-safe (unicode word chars, spaces, dots, "
            "underscores, dashes; must not start with a space)"
        )
    return f"{slugify(tag)}.md"


def _disambiguate_filename(
    directory: Path, filename: str
) -> str:
    """First ``<stem>[-N].md`` in ``directory`` not taken by another
    digest file. Two distinct tags can slug to the same name ("dnd dark
    castle" vs "dnd-dark-castle" → "dnd-dark-castle.md"); the -2 suffix
    (same shape as enrich._disambiguate / export.py folder suffixes)
    keeps both digests — the frontmatter ``tag:`` still identifies which
    is which."""
    candidate = filename
    n = 2
    while (directory / candidate).exists():
        stem, dot, ext = filename.rpartition(".")
        candidate = f"{stem}-{n}{dot}{ext}"
        n += 1
    return candidate


def _atomic_write(path: Path, content: str) -> None:
    """Unique-tmp + os.replace.

    Concurrent digests for the same tag (the user re-fires the endpoint
    while a previous run is mid-write) must never tear the note or
    interleave partial content. The export module carries the same
    contract; this stays deliberately lighter — no flock, because the
    activity retry policy caps concurrent writers at 1 (Temporal won't
    start a duplicate of the same workflow id).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _frontmatter(recording_ids: list[str], tag: str, count: int) -> str:
    """Obsidian-friendly YAML frontmatter (sort_keys=False for stable order)."""
    fm = {
        "tag": tag,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "recordings": list(recording_ids),
        "count": count,
    }
    return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()


# ---------- Postgres side -------------------------------------------------------


def _select_recordings(
    tag: str, last_n: int, *, include_recording_id: str | None = None
) -> list[DigestRow]:
    """Last N *done* recordings carrying ``tag`` (already normalized).

    ``tag == "untagged"`` is the built-in catch-all namespace: it matches
    recordings whose ``tags`` array is EMPTY (pg: ``array_length(tags,1)
    IS NULL``; sqlite: ``json_array_length(tags) = 0``), not rows that
    literally carry the word. Ordering and limit are the same as for a
    regular tag: newest first by ``created_at DESC, id DESC``.

    ``include_recording_id``: the auto-digest path runs at the tail of
    the enrich activity, BEFORE ``finalize_recording`` flips the
    recording to ``done`` — without it the just-enriched recording is
    invisible to the ``state == done`` filter and the digest is built
    from the wrong window (2026-09-04 pathfinder incident: the note was
    stamped with pf2 while pf1 was the one enriching, against a graph
    the purge had just emptied). The included row bypasses the state
    filter; when it also falls outside the ``last_n`` window it is
    fetched separately and merged back in its ``created_at`` position so
    newest-first stays true.

    Rows with NO ``meta/events.json`` anywhere are dropped: the digest
    prompt renders per-session events from that artifact, and a
    recording whose timeline artifact was wiped by a tag purge (see
    purge.py — single-tag recordings) must not surface empty in the
    digest until it is re-enriched. Multi-tag recordings keep the
    artifact on purge, so they stay selectable. The file is written by
    enrich BEFORE auto-digest runs, so the enriching recording always
    has one.
    """
    with session() as s:
        # or_(done, include): the include row rides along regardless of
        # its state; without the param this collapses to the plain done
        # filter. Shared verbatim by both dialect branches.
        def state_filter() -> Any:
            if include_recording_id is None:
                return Recording.state == RecordingState.done
            return or_(
                Recording.state == RecordingState.done,
                Recording.id == include_recording_id,
            )

        dialect_name = s.get_bind().dialect.name
        if dialect_name == "postgresql":
            tag_filter = (
                Recording.tags.contains([tag])
                if tag != "untagged"
                else text("array_length(tags, 1) IS NULL")
            )
            rows = (
                s.query(Recording)
                .filter(state_filter(), tag_filter)
                .order_by(Recording.created_at.desc(), Recording.id.desc())
                .limit(last_n)
                .all()
            )
        else:
            # SQLite: ``tags`` is JSON; ``EXISTS`` with json_each is portable.
            if tag == "untagged":
                tag_filter = text("json_array_length(recordings.tags) = 0")
            else:
                tag_json = tag.replace("\\", "\\\\").replace('"', '\\"')
                tag_filter = text(
                    f"EXISTS (SELECT 1 FROM json_each(recordings.tags) "
                    f"WHERE value = '{tag_json}')"
                )
            rows = (
                s.query(Recording)
                .filter(state_filter(), tag_filter)
                .order_by(Recording.created_at.desc(), Recording.id.desc())
                .limit(last_n)
                .all()
            )
        if (
            include_recording_id is not None
            and include_recording_id not in {r.id for r in rows}
        ):
            # Outside the last_n window (older recording, or a purge
            # wiped the other rows' artifacts): fetch it separately and
            # merge by created_at so the newest-first order stays true.
            extra = (
                s.query(Recording)
                .filter(Recording.id == include_recording_id)
                .one()
            )
            rows = sorted(
                rows + [extra],
                key=lambda r: (r.created_at, r.id),
                reverse=True,
            )
        langs = _stage_language(s, [r.id for r in rows])
    return [
        DigestRow(
            recording_id=r.id,
            title=r.title or "",
            created_at=r.created_at,
            recording_date=r.recorded_at or r.created_at,
            language=langs.get(r.id),
        )
        for r in rows
    ]


# ---------- Neo4j side ----------------------------------------------------------


def _fetch_graph_slice(
    tag: str,
    rec_ids: list[str],
    *,
    graph_uri: str,
    graph_user: str,
    graph_password: str,
    graph_database: str,
) -> DigestGraphSlice:
    """Read-side Cypher pull. Returns a structured slice for the LLM prompt."""
    if not rec_ids:
        return DigestGraphSlice(entities=[], events=[], relations=[])
    driver = GraphDatabase.driver(graph_uri, auth=(graph_user, graph_password))
    try:
        with driver.session(database=graph_database) as session_:
            result = session_.run(_DIGEST_CYPHER, tag=tag, rec_ids=rec_ids)
            entities: dict[tuple[str | None, str | None], dict[str, object]] = {}
            events: list[dict[str, object]] = []
            relations: list[dict[str, object]] = []
            rec_id_set = set(rec_ids)
            for row in result:
                # Entities always carry `label`; events never do — that
                # property (not the node label, which profiles can
                # override) is the discriminator.
                kind = "entity" if row["label"] is not None else "event"
                origin = row["origin"]
                if kind == "entity":
                    # Key by slug (the graph MERGE key): two DISAMBIGUATED
                    # entities can share (label, type) and must not fold
                    # into one digest bucket.
                    key = row["slug"]
                    bucket = entities.setdefault(
                        key,
                        {
                            "label": row["label"],
                            "type": row["type"],
                            "slug": row["slug"],
                            "sessions": set(),
                        },
                    )
                    # Sessions = this window's recordings that touched the
                    # entity (its own origin, plus the provenance list
                    # accumulated across MERGEs).
                    touched = {origin, *row["rec_ids_all"]} & rec_id_set
                    bucket["sessions"].update(touched)  # type: ignore[union-attr]
                else:
                    events.append(
                        {
                            "origin": origin,
                            "kind": row["kind"],
                            "ts": row["ts"],
                            "summary": row["summary"],
                        }
                    )
                if row["rel_type"]:
                    relations.append(
                        {
                            "from": row["label"],
                            "rel": row["rel_type"],
                            "to": row["rel_label"],
                            "from_slug": row["slug"],
                            "to_slug": row["rel_slug"],
                        }
                    )
        # Sets aren't JSON-friendly; coerce to sorted lists for the prompt.
        out_entities: list[dict[str, object]] = []
        for v in entities.values():
            sessions = sorted(v["sessions"])  # type: ignore[arg-type]
            out_entities.append(
                {
                    "label": v["label"],
                    "type": v["type"],
                    "sessions": sessions,
                    "session_count": len(sessions),
                }
            )
        return DigestGraphSlice(
            entities=out_entities,
            events=events,
            relations=relations,
        )
    finally:
        driver.close()


# ---------- prompt + LLM -------------------------------------------------------


def _render_prompt(
    tag: str,
    last_n: int,
    rows: list[DigestRow],
    graph: DigestGraphSlice,
) -> str:
    # Session identity for the LLM: "title (YYYY-MM-DD)" keyed by the
    # recording id — the graph slice's event "origin" values are raw ids,
    # and they get mapped through this before rendering.
    sessions: dict[str, str] = {
        r.recording_id: f"{r.title or '(untitled)'} ({r.recording_date.date().isoformat()})"
        for r in rows
    }
    # Language directive: the DOMINANT non-None language of the selection.
    # A mixed-language set of sessions is still one digest — majority
    # wins; the code is rendered as its English name in the directive
    # (see _LANGUAGE_NAMES) when known, else verbatim.
    langs = [r.language for r in rows if r.language]
    if langs:
        dominant = Counter(langs).most_common(1)[0][0]
        directive = (
            f"Write the ENTIRE digest in "
            f"{_LANGUAGE_NAMES.get(dominant, dominant)} ({dominant}). "
        )
    else:
        directive = _LANGUAGE_DIRECTIVE_FALLBACK
    entities_text = "\n".join(
        f"- {e['label']} ({e['type']}) — {e['session_count']} session(s)"
        for e in graph.entities
    ) or "- (none)"
    events_text = "\n".join(
        f"- {sessions.get(str(e['origin']), e['origin'])} "
        f"[{e['kind']} @ {e['ts']}] {e['summary']}"
        for e in graph.events
    ) or "- (none)"
    rels_text = "\n".join(
        f"- {r['from']} --[{r['rel']}]--> {r['to']}" for r in graph.relations
    ) or "- (none)"
    return _DIGEST_PROMPT_HEADER.format(
        tag=tag,
        last_n=last_n,
        count=len(rows),
        sessions=", ".join(sessions[id_] for id_ in sessions),
        entities=entities_text,
        events=events_text,
        relations=rels_text,
        language_directive=directive,
    )


def _call_llm(prompt: str, cfg: Any) -> str:
    """Single chat call. ``cfg.summarize`` is reused per the wave-B contract
    (the same local llama-server backs summarize / enrich / digest)."""
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.post(
        cfg.summarize.base_url.rstrip("/") + "/chat/completions",
        headers=headers,
        json={
            "model": cfg.summarize.model,
            "messages": system_first_messages(
                [
                    {"role": "system", "content": "Follow the user's instructions."},
                    {"role": "user", "content": prompt},
                ]
            ),
        },
        timeout=_HTTP_TIMEOUT_SEC,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ---------- public entry point -------------------------------------------------


def build_digest_input(
    tag: str,
    last_n: int,
    cfg: Any,
    *,
    include_recording_id: str | None = None,
) -> DigestInput:
    """Compose the LLM input from Postgres + Neo4j.

    Exposed separately from ``run_digest`` so unit tests can assert the
    prompt shape (entities grouped, events per-recording) without paying
    for an HTTP call.
    """
    rows = _select_recordings(tag, last_n, include_recording_id=include_recording_id)
    graph = _fetch_graph_slice(
        tag,
        [r.recording_id for r in rows],
        graph_uri=cfg.graph.uri,
        graph_user=cfg.graph.user,
        graph_password=os.environ.get(cfg.graph.password_env, ""),
        graph_database=cfg.graph.database,
    )
    return DigestInput(tag=tag, last_n=last_n, rows=rows, graph=graph)


def _note_recording_ids(
    rec_ids: list[str], recordings_root: Path, transcripts_root: Path
) -> set[str]:
    """Ids among ``rec_ids`` that have a ``meta/events.json`` timeline
    artifact — storage copy first, vault mirrors second (a vault-mode
    recording has no storage copy; same candidate order as
    graph_edit._meta_candidates). Purged-but-not-rebuilt recordings drop
    out of the digest window until they are re-enriched.
    """
    # Local import: export.scan_recording_folders is heavier than the
    # rest of this module (walks the vault tree); keep it out of the
    # import cycle and only pay it when a digest actually runs.
    from .export import Rec, scan_recording_folders

    out: set[str] = set()
    for rec_id in rec_ids:
        if (recordings_root / rec_id / "meta" / "events.json").is_file():
            out.add(rec_id)
            continue
        rec = Rec(id=rec_id, title="", created_at=datetime.now(UTC), duration_sec=None)
        for folder in scan_recording_folders(transcripts_root, rec):
            if (folder / ".transcripter" / "meta" / "events.json").is_file():
                out.add(rec_id)
                break
    return out


# Frontmatter block matcher for _existing_digest_for_tag: leading ---,
# YAML body, closing ---. DOTALL so multi-line YAML blocks match.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

def _existing_digest_for_tag(digests_dir: Path, tag: str) -> Path | None:
    """The file already carrying this tag's digest, if any.

    Scans ``digests/*.md`` sorted by name and matches the YAML
    frontmatter ``tag:`` against ``tag`` (same lookup the API's
    GET /tags/{tag}/digest performs). Regeneration must OVERWRITE the
    note wherever it lives — a ``-N`` suffix from an earlier slug
    collision is part of that tag's identity now, not a new file.
    """
    try:
        candidates = sorted(digests_dir.glob("*.md"))
    except OSError:
        return None
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        if isinstance(fm, dict) and fm.get("tag") == tag:
            return p
    return None


def write_digest(
    transcripts_root: Path,
    input: DigestInput,
    body: str,
) -> Path:
    """Atomically write the digest note under ``<transcripts_root>/digests/``.

    The filename is the tag's SLUG (see ``safe_filename``). When this
    tag ALREADY has a digest note (frontmatter ``tag:`` matches, even
    under a ``-N`` collision name), it is overwritten in place — the
    regenerate/auto-digest path must refresh the existing note, not
    pile up ``-2``, ``-3``… copies. Only a genuinely NEW tag whose slug
    collides with another tag's file gets a disambiguated name.

    Returns the path that was written. The caller is responsible for the
    LLM call; this function only assembles the final markdown and
    performs the atomic write.
    """
    digests_dir = transcripts_root / "digests"
    existing = _existing_digest_for_tag(digests_dir, input.tag)
    filename = (
        existing.name
        if existing is not None
        else _disambiguate_filename(digests_dir, safe_filename(input.tag))
    )
    target = digests_dir / filename
    recording_ids = [r.recording_id for r in input.rows]
    # tag: = the DISPLAY tag verbatim (spaces/Cyrillic as the user typed
    # it after normalization); the filename is only the slug.
    fm = _frontmatter(recording_ids, input.tag, len(recording_ids))
    content = f"---\n{fm}\n---\n\n{body.rstrip()}\n"
    _atomic_write(target, content)
    return target


def run_digest(
    tag: str,
    last_n: int,
    cfg: Any,
    transcripts_root: Path,
    *,
    include_recording_id: str | None = None,
) -> dict:
    """Top-level orchestrator. Returns the activity payload.

    Activity payload shape:
        ``{"written": True, "path": "<abs>", "recordings": [...]}`` — file
            was written.
        ``{"written": False, "reason": "<message>"}`` — nothing matched (no
            error; the API has already returned 202 with the workflow_id and
            the user checks the file via the transcripts dir or a
            follow-up workflow query).
        Raises ``ValueError`` for invalid tag normalization (defensive — the
            API already rejects).
        Lets ``httpx`` errors bubble (activity-level retry handles transient
            LLM hiccups per workflow's retry policy).
    """
    if not _SAFE_TAG_RE.match(tag):
        raise ValueError(
            f"tag {tag!r} is not file-safe (unicode word chars, spaces, dots, "
            "underscores, dashes; must not start with a space)"
        )
    input = build_digest_input(
        tag, last_n, cfg, include_recording_id=include_recording_id
    )
    if not input.rows:
        return {
            "written": False,
            "reason": f"no done recordings carry tag {tag!r}",
        }
    # Drop rows whose events.json was wiped by a purge and not yet
    # rebuilt: the LLM prompt renders per-session events from the
    # artifact, and an artifact-less row would surface as an empty
    # session (the 2026-09-04 pathfinder digest showed a pf2 session
    # with zero events after the purge).
    with_artifact = _note_recording_ids(
        [r.recording_id for r in input.rows], cfg.recordings_root, transcripts_root
    )
    input = replace(
        input,
        rows=[r for r in input.rows if r.recording_id in with_artifact],
    )
    if not input.rows:
        return {
            "written": False,
            "reason": f"no recordings with a timeline artifact carry tag {tag!r}",
        }
    prompt = _render_prompt(tag, last_n, input.rows, input.graph)
    body = _call_llm(prompt, cfg)
    path = write_digest(transcripts_root, input, body)
    return {
        "written": True,
        "path": str(path),
        "recordings": [r.recording_id for r in input.rows],
        "count": len(input.rows),
    }