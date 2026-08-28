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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from neo4j import GraphDatabase
from sqlalchemy import text

from .db import Recording, RecordingState, session

log = logging.getLogger("transcripter.digest")

# Tag name on disk: only the file-system-safe subset that survives every
# Obsidian / Vault / Windows-share combination. Anything outside is rejected
# at the API boundary (regex in routes/tags.py) so we can return 400 cleanly.
# Same set the slugify helper in enrich.py uses — kept in sync by hand.
_SAFE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# HTTP budget kept 30 s UNDER the Temporal start_to_close (2400 s) so a
# httpx.ReadTimeout fires (a plain Exception → activity raised) before
# Temporal cancels the activity (CancelledError bypasses except-Exception
# and leaves the workflow hanging). Same shape as summarize / enrich.
_HTTP_TIMEOUT_SEC = 2370.0

# Reuse the per-tag Cypher query: collect entities (deduped across the
# selection, so we know what's been mentioned over the last N sessions)
# and events (one row per recording so the digest can be per-session).
_DIGEST_CYPHER = """
MATCH (n)
WHERE (n:Entity OR n:Event)
  AND n.tag = $tag
  AND n.origin_recording_id IN $rec_ids
OPTIONAL MATCH (n)-[r]->(m)
WHERE (m:Entity OR m:Event) AND m.tag = $tag AND m.origin_recording_id IN $rec_ids
RETURN
  labels(n) AS labels,
  n.tag AS tag,
  n.origin_recording_id AS origin,
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
    "4. Open threads / unresolved questions if any.\n"
    "Write in the same language as the session material. Markdown only, "
    "no frontmatter — the host prepends its own.\n\n"
    "---\n\n"
    "Tag: {tag}\n"
    "Sessions (id, title, count): {count}\n"
    "Recording ids: {rec_ids}\n\n"
    "Entities (label — type — sessions):\n{entities}\n\n"
    "Events (recording_id — kind — ts — summary):\n{events}\n\n"
    "Relations (from — rel — to):\n{relations}\n"
)


# ---------- public dataclasses --------------------------------------------------


@dataclass(frozen=True)
class DigestRow:
    """One Postgres row the digest feeds on."""

    recording_id: str
    title: str
    created_at: datetime


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


def safe_filename(tag: str) -> str:
    """Return the file-safe form of ``tag`` or raise ``ValueError``.

    The API already rejects bad tags with 400, so this guard only fires if
    something internal calls with a non-normalized value — failing loudly
    keeps a bad path out of the user's transcripts dir instead of dropping
    a ``<hash>.md`` the user can't trace back.
    """
    if not _SAFE_TAG_RE.match(tag):
        raise ValueError(
            f"tag {tag!r} is not file-safe (only [a-z0-9._-], starting with "
            "an alphanumeric)"
        )
    return f"{tag}.md"


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


def _select_recordings(tag: str, last_n: int) -> list[DigestRow]:
    """Last N *done* recordings carrying ``tag`` (already normalized)."""
    with session() as s:
        dialect_name = s.get_bind().dialect.name
        if dialect_name == "postgresql":
            rows = (
                s.query(Recording)
                .filter(
                    Recording.state == RecordingState.done,
                    Recording.tags.contains([tag]),
                )
                .order_by(Recording.created_at.desc(), Recording.id.desc())
                .limit(last_n)
                .all()
            )
        else:
            # SQLite: ``tags`` is JSON; ``EXISTS`` with json_each is portable.
            tag_json = tag.replace("\\", "\\\\").replace('"', '\\"')
            rows = (
                s.query(Recording)
                .filter(
                    Recording.state == RecordingState.done,
                    text(
                        f"EXISTS (SELECT 1 FROM json_each(recordings.tags) "
                        f"WHERE value = '{tag_json}')"
                    ),
                )
                .order_by(Recording.created_at.desc(), Recording.id.desc())
                .limit(last_n)
                .all()
            )
    return [
        DigestRow(recording_id=r.id, title=r.title or "", created_at=r.created_at)
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
            for row in result:
                labels = set(row["labels"] or [])
                kind = "entity" if "Entity" in labels else "event"
                origin = row["origin"]
                if kind == "entity":
                    key = (row["label"], row["type"])
                    bucket = entities.setdefault(
                        key,
                        {
                            "label": row["label"],
                            "type": row["type"],
                            "slug": row["slug"],
                            "sessions": set(),
                        },
                    )
                    if origin is not None:
                        bucket["sessions"].add(origin)  # type: ignore[union-attr]
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
    entities_text = "\n".join(
        f"- {e['label']} ({e['type']}) — {e['session_count']} session(s)"
        for e in graph.entities
    ) or "- (none)"
    events_text = "\n".join(
        f"- {e['origin']} [{e['kind']} @ {e['ts']}] {e['summary']}"
        for e in graph.events
    ) or "- (none)"
    rels_text = "\n".join(
        f"- {r['from']} --[{r['rel']}]--> {r['to']}" for r in graph.relations
    ) or "- (none)"
    rec_ids = [r.recording_id for r in rows]
    return _DIGEST_PROMPT_HEADER.format(
        tag=tag,
        last_n=last_n,
        count=len(rows),
        rec_ids=", ".join(rec_ids),
        entities=entities_text,
        events=events_text,
        relations=rels_text,
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
            "messages": [
                {"role": "system", "content": "Follow the user's instructions."},
                {"role": "user", "content": prompt},
            ],
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
) -> DigestInput:
    """Compose the LLM input from Postgres + Neo4j.

    Exposed separately from ``run_digest`` so unit tests can assert the
    prompt shape (entities grouped, events per-recording) without paying
    for an HTTP call.
    """
    rows = _select_recordings(tag, last_n)
    graph = _fetch_graph_slice(
        tag,
        [r.recording_id for r in rows],
        graph_uri=cfg.graph.uri,
        graph_user=cfg.graph.user,
        graph_password=os.environ.get(cfg.graph.password_env, ""),
        graph_database=cfg.graph.database,
    )
    return DigestInput(tag=tag, last_n=last_n, rows=rows, graph=graph)


def write_digest(
    transcripts_root: Path,
    input: DigestInput,
    body: str,
) -> Path:
    """Atomically write the digest note under ``<transcripts_root>/digests/``.

    Returns the path that was written. The caller is responsible for the
    LLM call; this function only assembles the final markdown and
    performs the atomic write.
    """
    filename = safe_filename(input.tag)
    target = transcripts_root / "digests" / filename
    recording_ids = [r.recording_id for r in input.rows]
    fm = _frontmatter(recording_ids, input.tag, len(recording_ids))
    content = f"---\n{fm}\n---\n\n{body.rstrip()}\n"
    _atomic_write(target, content)
    return target


def run_digest(
    tag: str,
    last_n: int,
    cfg: Any,
    transcripts_root: Path,
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
            f"tag {tag!r} is not file-safe (only [a-z0-9._-], starting with "
            "an alphanumeric)"
        )
    input = build_digest_input(tag, last_n, cfg)
    if not input.rows:
        return {
            "written": False,
            "reason": f"no done recordings carry tag {tag!r}",
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