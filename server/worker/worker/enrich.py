"""Knowledge-graph enrichment (wave B).

Given a transcript and a profile-driven ``enrich.prompt``, ask an
OpenAI-compatible chat endpoint for a JSON object describing events,
entities, and relations; normalize and de-duplicate, then write the
result into Neo4j inside a single transaction so re-runs of this stage
on the same recording are idempotent.

Design notes (locked by the wave-B plan):

* LLM call uses ``response_format={"type": "json_object"}`` so the model
  is *forced* to emit a JSON object. Two retries on a non-JSON response
  before the stage is marked failed. Budgets match the summarize stage
  (2400/2370) — the LiteLLM proxy ceiling is the binding constraint.
* Slug normalization: lowercase, non-alnum → ``-``, collapse repeats,
  strip. Two records reaching the same slug on different labels is
  resolved by a one-shot LLM question "same entity? Y/N"; an LLM error
  is treated as "same" (best-effort, never blocks the stage).
* Graph writes run in one transaction:
  ``MATCH (n {origin_recording_id: $rec}) DETACH DELETE n``
  followed by ``MERGE`` of entities (tag, slug) + ``CREATE`` of events
  + edges. The DETACH DELETE is the idempotency mechanism — a second
  run on the same recording produces the same graph state, never a
  duplicated one.
* The Neo4j driver is sync. We hand its blocking calls to
  ``asyncio.to_thread`` so the worker's event loop (and Temporal
  heartbeats) keep ticking — the same pattern ``summarize.py`` uses
  for the chat endpoint. There is no async bolt driver in the
  official client.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from neo4j import GraphDatabase

log = logging.getLogger("transcripter.enrich")

# Wire shape is locked (see wave-B contract §4): events + entities +
# relations. The profile prompt steers the domain; the structure is
# the host's. A response missing one of these keys still parses — the
# missing list defaults to [].
_EXTRACTION_KEYS = ("events", "entities", "relations")

# HTTP budget kept 30 s UNDER the Temporal start_to_close (2400 s) so a
# httpx.ReadTimeout fires (a plain Exception → stage failed) before
# Temporal cancels the activity (CancelledError bypasses
# except-Exception and leaves the stage stuck running). Matches
# summarize.py exactly.
_HTTP_TIMEOUT_SEC = 2370.0
_TEMPORAL_START_TO_CLOSE_SEC = 2400

# Two retries inside the activity (matching the wave-B contract). A
# third failure → stage failed (best-effort, recording stays done).
_MAX_LLM_ATTEMPTS = 3

# Slug-normalization regex: collapse runs of non-word chars to dashes.
# Unicode-aware: \w keeps Cyrillic and other letters — the shipped
# pathfinder profile is Russian, and ASCII-only slugification collapsed
# every Cyrillic label to "unknown", folding distinct entities into one
# MERGE key. \w also keeps "_" (harmless in a slug).
_NON_ALNUM = re.compile(r"[^\w]+", re.UNICODE)
_MULTI_DASH = re.compile(r"-{2,}")

# A model that says "same" sometimes fences the answer with quotes or
# punctuation. Be lenient — anything that contains a clear "Y" before
# the first "N" counts.
_DEDUP_AFFIRMATIVE = {"y", "yes", "true", "same", "да", "是的"}
_DEDUP_NEGATIVE = {"n", "no", "false", "different", "нет", "不"}


@dataclass(frozen=True)
class ExtractedEvent:
    ts: str
    kind: str
    summary: str


@dataclass(frozen=True)
class ExtractedEntity:
    slug: str
    label: str
    type: str


@dataclass(frozen=True)
class ExtractedRelation:
    from_slug: str
    to_slug: str
    type: str


@dataclass
class ExtractedGraph:
    events: list[ExtractedEvent] = field(default_factory=list)
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)


def slugify(label: str) -> str:
    """Casefold + non-word chars → ``-`` + collapse + strip.

    Unicode-aware: Cyrillic (and other scripts) survive — the slug is the
    MERGE key, and an ASCII-only mapping collapses every non-ASCII label
    to ``"unknown"``, folding distinct entities into one node. Truly
    empty results (punctuation-only labels) still collapse to
    ``"unknown"`` so we never hand a blank key to Cypher.
    """
    s = _NON_ALNUM.sub("-", label.casefold()).strip("-")
    s = _MULTI_DASH.sub("-", s)
    return s or "unknown"


def _coerce_event(raw: Any) -> ExtractedEvent | None:
    if not isinstance(raw, dict):
        return None
    ts = str(raw.get("ts", "")).strip()
    kind = str(raw.get("kind", "")).strip()
    summary = str(raw.get("summary", "")).strip()
    if not (ts and kind and summary):
        return None
    return ExtractedEvent(ts=ts, kind=kind, summary=summary)


def _coerce_entity(raw: Any) -> ExtractedEntity | None:
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label", "")).strip()
    type_ = str(raw.get("type", "")).strip() or "unknown"
    slug_src = str(raw.get("slug", "")).strip() or label
    if not label:
        return None
    return ExtractedEntity(slug=slugify(slug_src), label=label, type=type_)


def _coerce_relation(raw: Any) -> ExtractedRelation | None:
    if not isinstance(raw, dict):
        return None
    f = str(raw.get("from_slug", "")).strip()
    t = str(raw.get("to_slug", "")).strip()
    type_ = str(raw.get("type", "")).strip() or "related"
    if not (f and t):
        return None
    return ExtractedRelation(from_slug=slugify(f), to_slug=slugify(t), type=type_)


def _parse_extraction(payload: Any) -> ExtractedGraph:
    if not isinstance(payload, dict):
        raise TypeError("LLM response is not a JSON object")
    raw_events = payload.get("events") or []
    raw_entities = payload.get("entities") or []
    raw_relations = payload.get("relations") or []
    return ExtractedGraph(
        events=[e for e in (_coerce_event(x) for x in raw_events) if e is not None],
        entities=[e for e in (_coerce_entity(x) for x in raw_entities) if e is not None],
        relations=[r for r in (_coerce_relation(x) for x in raw_relations) if r is not None],
    )


def _render_prompt(template: str, title: str, transcript: str) -> str:
    """Substitute {title} / {transcript}. The contract requires ``{transcript}``;
    profiles.py already enforces that on load. ``{title}`` is optional."""
    # Literal replacement of exactly two placeholders — NOT str.format:
    # profile prompts legitimately embed JSON schema examples with braces
    # ({"events": [...]}) which format() would read as replacement fields
    # and die with KeyError (observed live 2026-08-27 on the pathfinder
    # profile's enrich prompt).
    return template.replace("{title}", title or "").replace("{transcript}", transcript)


def extract_from_transcript(
    transcript_path: Path,
    title: str,
    prompt_template: str,
    cfg: Any,
) -> ExtractedGraph:
    """One HTTP call to the chat endpoint; retry the same call twice if the
    model returns non-JSON or HTTP 5xx. Raises after the third failure —
    the activity catches and marks the stage failed.

    ``cfg.summarize`` is reused for the chat URL / model / auth: the
    same local llama-server (or LiteLLM proxy) backs both summarize
    and enrich — adding a second LLM config would just fork the
    deployment for no semantic gain.
    """
    transcript = transcript_path.read_text(encoding="utf-8")
    user_content = _render_prompt(prompt_template, title, transcript)
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    messages = [
        {"role": "system", "content": "Follow the user's instructions."},
        {"role": "user", "content": user_content},
    ]

    last_err: Exception | None = None
    for attempt in range(_MAX_LLM_ATTEMPTS):
        try:
            r = httpx.post(
                cfg.summarize.base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json={
                    "model": cfg.summarize.model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                },
                timeout=_HTTP_TIMEOUT_SEC,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            payload = json.loads(content)
            return _parse_extraction(payload)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            last_err = exc
            log.warning(
                "enrich: LLM attempt %d/%d failed: %s",
                attempt + 1,
                _MAX_LLM_ATTEMPTS,
                exc,
            )
            continue
    assert last_err is not None
    raise last_err


def _dedup_prompt(
    new_label: str, new_type: str, existing_label: str, existing_type: str
) -> str:
    return (
        "You are deduplicating entities for a knowledge graph.\n"
        f"Existing entity: label={existing_label!r}, type={existing_type!r}.\n"
        f"New candidate:  label={new_label!r}, type={new_type!r}.\n"
        "Are these the same real-world entity? Answer with a single token: Y or N."
    )


def _parse_yes_no(text: str) -> bool | None:
    """Return True for Y, False for N, None when ambiguous."""
    t = text.strip().lower().strip(".,!? \t\n\"'`")
    if not t:
        return None
    head = t.split()[0] if t.split() else t
    head = head.strip(".,!? \t\n\"'`")
    if head in _DEDUP_AFFIRMATIVE:
        return True
    if head in _DEDUP_NEGATIVE:
        return False
    if t in _DEDUP_AFFIRMATIVE:
        return True
    if t in _DEDUP_NEGATIVE:
        return False
    return None


def ask_same_entity(
    new_label: str,
    new_type: str,
    existing_label: str,
    existing_type: str,
    cfg: Any,
) -> bool:
    """LLM "same entity? Y/N". Errors → True (best-effort, never blocks).

    Two things matter here:

    * The answer must be deterministic — we treat anything not parseable
      as YES so a flaky LLM cannot corrupt the graph.
    * The HTTP call must NOT take 2400 s. We use a tight 30 s budget:
      a hung llama-server is not worth an activity timeout, the entity
      is deduplicated either way and the stage moves on.
    """
    prompt = _dedup_prompt(new_label, new_type, existing_label, existing_type)
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = httpx.post(
            cfg.summarize.base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            json={
                "model": cfg.summarize.model,
                "messages": [
                    {"role": "system", "content": "Answer with a single Y or N."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=30.0,
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("enrich dedup: LLM failed (%s); treating as same", exc)
        return True
    verdict = _parse_yes_no(answer)
    if verdict is None:
        log.warning("enrich dedup: ambiguous LLM answer %r; treating as same", answer)
        return True
    return verdict


def _safe_label(label: str) -> str:
    """Return ``label`` iff it matches Cypher label grammar, else "Entity".

    Cypher requires node/relationship labels to be valid identifiers;
    profile-supplied labels are user input and must be validated before
    being interpolated into a query string.
    """
    match = _SAFE_LABEL.match(label)
    return match.group(0) if match is not None else "Entity"

def write_to_graph(
    rec_id: str,
    tag: str,
    graph: ExtractedGraph,
    node_labels: Any,
    graph_uri: str,
    graph_user: str,
    graph_password: str,
    graph_database: str,
) -> int:
    """Single transaction:

    1. ``MATCH (n {origin_recording_id: $rec}) DETACH DELETE n`` —
       idempotency (a re-run produces the same state, never duplicates).
    2. ``MERGE`` entities by ``(tag, slug)``. The dedup loop above has
       already resolved same-entity collisions and re-slugged the new
       candidate (``-2``, ``-3`` suffix). Carry ``origin_recording_id``
       on every node for the same reason: the next run can purge them
       cleanly.
    3. ``CREATE`` events with origin + tag. Events are NOT merged — each
       recording's events are unique by construction, and a new run
       replaces the previous batch (the DETACH DELETE above).
    4. ``MERGE`` ``(:Event)-[:MENTIONS]->(:Entity)`` and
       ``(:Entity)-[:REL {type}]->(Entity)``.

    Returns the count of entity rows written. Errors propagate — the
    activity marks the stage failed.
    """
    # Build parameterized Cypher so the driver escapes every value
    # (the labels and types come from profile prompts, which are
    # user-controlled yaml — never interpolate into the query text).
    entity_label = str(getattr(node_labels, "entity", "Entity"))
    event_label = str(getattr(node_labels, "event", "Event"))
    safe_entity_label = _safe_label(entity_label)
    safe_event_label = _safe_label(event_label)

    driver = GraphDatabase.driver(graph_uri, auth=(graph_user, graph_password))
    try:
        with (
            driver.session(database=graph_database) as session,
            session.begin_transaction() as tx,
        ):
            entity_query = cast(
                "Any",
                (
                    f"MERGE (e:`{safe_entity_label}` {{tag: $tag, slug: $slug}}) "
                    "ON CREATE SET e.label = $label, e.type = $type, "
                    "e.origin_recording_id = $rec, e.first_seen_recording = $rec, "
                    "e.recording_ids = [$rec] "
                    # Multi-recording provenance: a shared entity MERGEs onto
                    # one node, so origin_recording_id alone can never show
                    # recurrence — digests read recording_ids instead.
                    "ON MATCH SET e.label = $label, e.type = $type, "
                    "e.recording_ids = CASE WHEN $rec IN coalesce(e.recording_ids, []) "
                    "THEN e.recording_ids ELSE e.recording_ids + $rec END "
                    "RETURN elementId(e)"
                ),
            )
            event_query = cast(
                "Any",
                (
                    f"CREATE (e:`{safe_event_label}` {{"
                    "ts: $ts, kind: $kind, summary: $summary, "
                    # Not an f-string: a single closing brace is literal here
                    # ("}}" would survive verbatim and break Cypher — caught
                    # by scripts/graph_probe.py against a live Neo4j).
                    "tag: $tag, origin_recording_id: $rec"
                    "}) RETURN elementId(e)"
                ),
            )
            mentions_query = cast(
                "Any",
                (
                    "MATCH (a), (b) WHERE elementId(a) = $a AND elementId(b) = $b "
                    "MERGE (a)-[:MENTIONS]->(b)"
                ),
            )

            delete_query = cast(
                "Any",
                "MATCH (n {origin_recording_id: $rec}) DETACH DELETE n",
            )
            tx.run(delete_query, rec=rec_id)

            # First pass: entities. Build a slug → id map via MERGE so
            # the second pass (relations) can refer to them by key.
            slug_to_node: dict[str, str] = {}
            for ent in graph.entities:
                node = tx.run(
                    entity_query,
                    tag=tag,
                    slug=ent.slug,
                    label=ent.label,
                    type=ent.type,
                    rec=rec_id,
                ).single()
                if node is not None:
                    slug_to_node[ent.slug] = node[0]
            # Second pass: events.
            event_ids: list[str] = []
            for ev in graph.events:
                node = tx.run(
                    event_query,
                    ts=ev.ts,
                    kind=ev.kind,
                    summary=ev.summary,
                    tag=tag,
                    rec=rec_id,
                ).single()
                if node is not None:
                    event_ids.append(node[0])

            # Third pass: edges. MENTIONS from events to entities
            # (case-insensitive label match against the event summary),
            # then REL between entities.
            for ev_id, ev in zip(event_ids, graph.events, strict=False):
                summary_lower = ev.summary.lower()
                for slug, node_id in slug_to_node.items():
                    ent_label = next(
                        (e.label for e in graph.entities if e.slug == slug),
                        "",
                    ).lower()
                    # Word-boundary match: bare substring containment links
                    # "Orc" to summaries mentioning "Orcus"/"orchestra".
                    if ent_label and re.search(
                        r"\b" + re.escape(ent_label) + r"\b", summary_lower
                    ):
                        tx.run(
                            mentions_query,
                            a=ev_id,
                            b=node_id,
                        )


            for rel in graph.relations:
                a = slug_to_node.get(rel.from_slug)
                b = slug_to_node.get(rel.to_slug)
                if not (a and b):
                    continue
                tx.run(
                    (
                        "MATCH (a), (b) WHERE elementId(a) = $a AND elementId(b) = $b "
                        "MERGE (a)-[r:REL {type: $type}]->(b)"
                    ),
                    a=a,
                    b=b,
                    type=rel.type,
                )
            return len(slug_to_node)
    finally:
        driver.close()


_SAFE_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def resolve_slugs(
    graph: ExtractedGraph,
    cfg: Any,
    tag: str,
    existing_lookup: ExistingEntityLookup | None = None,
) -> ExtractedGraph:
    """Run the two-level dedup loop on ``graph.entities``.

    Level 1 (cheap): group by slug. Within each group, the FIRST
    entity is the canonical; subsequent collisions are candidate
    duplicates.

    Level 2 (LLM, per pair): for each collision, ask "same entity?"
    using both labels and types. YES → drop the candidate (it merges
    into the first). NO → re-slug the candidate with ``-2``, ``-3``,
    ... until it stops colliding.

    ``existing_lookup(slug)`` lets the caller pre-seed collisions with
    entities already in the graph from previous recordings — same
    question, same answer.
    """
    seen: dict[str, ExtractedEntity] = {}
    out: list[ExtractedEntity] = []
    # Pre-resolution slug → resolved slug, so relations (authored against
    # the raw extraction) can be re-anchored after dedup/re-slugging.
    # Keyed by SLUG, not label: labels are not slug-safe (case, punct,
    # spaces) and two entities may share one label.
    remap: dict[str, str] = {}
    for ent in graph.entities:
        # Re-slug up front so labels differing only in case/punct join
        # the same group cheaply.
        slug = ent.slug
        orig_slug = ent.slug
        same = None
        if slug in seen:
            existing = seen[slug]
            same = ask_same_entity(
                ent.label,
                ent.type,
                existing.label,
                existing.type,
                cfg,
            )
        elif existing_lookup is not None:
            existing = existing_lookup(slug)
            if existing is not None:
                same = ask_same_entity(
                    ent.label,
                    ent.type,
                    existing["label"],
                    existing["type"],
                    cfg,
                )
                if same:
                    seen[slug] = ExtractedEntity(
                        slug=existing["slug"], label=existing["label"], type=existing["type"]
                    )
                    out.append(seen[slug])
                    remap.setdefault(orig_slug, existing["slug"])
                    continue
                # Step past every existing collision in the live graph
                # AND every reserved slug inside this extraction. Both
                # sets are off-limits: ``-2``, ``-3`` … must be unique
                # by the time we MERGE.
                slug = _next_free_slug(slug, seen, existing_lookup)
        if same is True:
            # Merged into the canonical/existing entity: relations pointing
            # at this duplicate must land on the surviving slug.
            remap.setdefault(orig_slug, seen[orig_slug].slug if orig_slug in seen else slug)
            continue
        if same is False and slug in seen:
            slug = _disambiguate(slug, seen)

        ent = ExtractedEntity(slug=slug, label=ent.label, type=ent.type)
        seen[slug] = ent
        out.append(ent)
        remap.setdefault(orig_slug, slug)
    # Re-anchor relations to the resolved slugs (pre-resolution slug ->
    # resolved slug); relations referencing dropped/renamed duplicates
    # follow their entity's final slug, unknown slugs pass through.
    out_relations: list[ExtractedRelation] = []
    for rel in graph.relations:
        f = remap.get(rel.from_slug, rel.from_slug)
        t = remap.get(rel.to_slug, rel.to_slug)
        out_relations.append(ExtractedRelation(from_slug=f, to_slug=t, type=rel.type))
    return ExtractedGraph(events=graph.events, entities=out, relations=out_relations)

def _next_free_slug(
    slug: str,
    taken: dict[str, ExtractedEntity],
    lookup: Any,
) -> str:
    """First slug not in ``taken`` AND not present in the live graph.

    Re-runs on the same recording must produce identical graph state:
    ``lookup`` excludes the current recording's own nodes (they are about
    to be DETACH DELETEd and rewritten), so a re-run RECLAIMS its own
    previously-disambiguated slugs instead of drifting ``-2`` → ``-3``.
    The local ``taken`` dict catches collisions inside this extraction;
    ``lookup`` catches collisions with OTHER recordings' nodes."""
    n = 2
    while True:
        candidate = f"{slug}-{n}"
        if candidate not in taken and lookup(candidate) is None:
            return candidate
        n += 1
        if n > 999:
            return f"{slug}-{hash((slug, n)) & 0xFFFF:x}"


def _disambiguate(slug: str, taken: dict[str, ExtractedEntity]) -> str:
    n = 2
    while True:
        candidate = f"{slug}-{n}"
        if candidate not in taken:
            return candidate
        n += 1
        if n > 999:
            # Pathological: bail with a hash so we never spin forever.
            return f"{slug}-{hash(slug) & 0xFFFF:x}"


class ExistingEntityLookup:
    """Read-side helper that surfaces (slug, label, type) rows for the
    dedup pre-seed. The activity passes this in so a recording can
    detect collisions with nodes from earlier recordings on the same
    tag."""

    def __init__(self, driver: Any, database: str, tag: str, exclude_rec: str = "") -> None:
        self._driver = driver
        self._database = database
        self._tag = tag
        # Nodes written by THIS recording are excluded: they are deleted
        # and rewritten by the same run, so they must not count as taken
        # (a regenerate would otherwise drift disambiguated slugs).
        self._exclude_rec = exclude_rec

    def close(self) -> None:
        """Release the underlying driver. The OWNING side (whoever built
        the lookup via pre_existing_lookup) closes through this method —
        never by reaching into ``_driver`` directly."""
        self._driver.close()

    def __call__(self, slug: str) -> dict[str, str] | None:
        with self._driver.session(database=self._database) as session:
            row = session.run(
                "MATCH (e {tag: $tag, slug: $slug}) "
                # $rec = '' (tests/legacy callers) keeps every node in play;
                # a null origin_recording_id (foreign/manually created node)
                # must not silently opt out of dedup either.
                "WHERE $rec = '' OR coalesce(e.origin_recording_id, '') <> $rec "
                "RETURN e.label AS label, e.type AS type, e.slug AS slug LIMIT 1",
                tag=self._tag,
                slug=slug,
                rec=self._exclude_rec,
            ).single()
        if row is None:
            return None
        return {"label": row["label"], "type": row["type"], "slug": row["slug"]}


def pre_existing_lookup(
    graph_uri: str,
    graph_user: str,
    graph_password: str,
    graph_database: str,
    tag: str,
    exclude_rec: str = "",
) -> ExistingEntityLookup:
    """Build an ``ExistingEntityLookup`` bound to the configured graph."""
    driver = GraphDatabase.driver(graph_uri, auth=(graph_user, graph_password))

    return ExistingEntityLookup(driver, graph_database, tag, exclude_rec)
