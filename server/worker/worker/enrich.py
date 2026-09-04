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

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from neo4j import GraphDatabase

from .embeddings import ensure_vector_index, same_entity_decision
from .llm_payload import system_first_messages

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
# ttrpg profile is Russian, and ASCII-only slugification collapsed
# every Cyrillic label to "unknown", folding distinct entities into one
# MERGE key. \w also keeps "_" (harmless in a slug).
_NON_ALNUM = re.compile(r"[^\w]+", re.UNICODE)
_MULTI_DASH = re.compile(r"-{2,}")

# A model that says "same" sometimes fences the answer with quotes or
# punctuation. Be lenient — anything that contains a clear "Y" before
# the first "N" counts.
_DEDUP_AFFIRMATIVE = {"y", "yes", "true", "same", "да", "是的"}
_DEDUP_NEGATIVE = {"n", "no", "false", "different", "нет", "不"}

# Phase 2: built-in fallback extraction prompt. Used by the enrich
# activity ONLY when no profile matches the recording's type AND
# ``graph.enrich_all`` is on — a profile that matched but has no enrich
# section still means the author opted out. Russian like the shipped
# profiles; deliberately minimal ontology because a domain profile
# always beats it. MUST keep all three placeholders ({title},
# {transcript}, {known_entities}, {corrections}): the activity always
# renders the known-entities and corrections blocks for it, and the
# same literal-.replace() rules apply (JSON braces below are safe).
_FALLBACK_ENRICH_PROMPT = """\
Ты извлекаешь структурированные данные из транскрипта записи «{title}».
Верни JSON-объект:
{"events": [{"ts": "…", "kind": "…", "summary": "…"}],
 "entities": [{"slug": "…", "label": "…", "type": "person|org|project|place|thing"}],
 "relations": [{"from_slug": "…", "to_slug": "…", "type": "…"}]}
Правила: только факты из транскрипта; slug — lowercase, слова через дефис;
kind — один из: milestone (веха), change (изменение), decision (решение), meeting (встреча);
type — один из: person (человек), org (организация), project (проект),
place (место), thing (предмет);
events — ключевые моменты в хронологическом порядке;
relations — связи между сущностями (например: works_on, member_of, located_in);
событие может нести "entities": [slug, …] — упомянутые сущности;
Пустые разделы — пустые списки.
Известные сущности этого пространства (переиспользуй их slug, если упоминаешь):
{known_entities}
Устойчивые правки оператора к прошлым ошибкам извлечения (это не факты
транскрипта — это указания; применяй их, если предмет правки возникает):
{corrections}
ТРАНСКРИПТ:
{transcript}"""

@dataclass(frozen=True)
class ExtractedEvent:
    ts: str
    kind: str
    summary: str
    # Phase 2: model-declared mentions (wire ``entities: [slug, ...]``),
    # already slugified, deduped, and filtered to slugs the extraction
    # defines. Empty → the label-in-summary heuristic fills in (see
    # ``_event_mentions``).
    mentions: list[str] = field(default_factory=list)


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


def compute_event_key(rec_id: str, ts: str, kind: str, summary: str, occurrence: int = 0) -> str:
    """Phase A: deterministic event identity WITHIN ONE generation.

    hash(origin_recording_id, ts, kind, summary) + an occurrence suffix
    when the SAME (ts, kind, summary) triple appears twice in one
    extraction (the LLM sometimes emits near-identical events). The key
    is NOT stable across regenerate — re-extraction rewords summaries —
    which is exactly why the edit-store keeps the fuzzy re-anchor
    context (``anchor``) and re-keys edits on every overlay pass; the
    key only has to address an event between regenerates.
    """
    raw = f"{rec_id}\x1f{ts}\x1f{kind}\x1f{summary}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return digest if occurrence == 0 else f"{digest}-{occurrence}"


def compute_event_keys(rec_id: str, events: list[ExtractedEvent]) -> list[str]:
    """Keys for a whole extraction, occurrence-suffixed on collision
    (list order keeps them deterministic for the same extraction)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for ev in events:
        base = compute_event_key(rec_id, ev.ts, ev.kind, ev.summary)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base}-{n}")
    return out


def _coerce_mentions(raw: Any, known_slugs: set[str] | None) -> list[str]:
    """Wire ``entities: [slug, ...]`` on an event → deduped slug list.

    Each item is slugified (models mix slugs and labels) and deduped in
    order. When ``known_slugs`` is given, slugs outside the extraction's
    entity set are dropped with a debug log — a mention of a
    non-extracted entity could never become a graph edge. A non-list
    (or empty) value simply means "no declared mentions" and the
    heuristic kicks in downstream.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        slug = slugify(str(item))
        if not slug or slug in out:
            continue
        if known_slugs is not None and slug not in known_slugs:
            log.debug("enrich: dropping declared mention of unknown slug %r", slug)
            continue
        out.append(slug)
    return out


def _coerce_event(
    raw: Any, known_slugs: set[str] | None = None
) -> ExtractedEvent | None:
    if not isinstance(raw, dict):
        return None
    ts = str(raw.get("ts", "")).strip()
    kind = str(raw.get("kind", "")).strip()
    summary = str(raw.get("summary", "")).strip()
    if not (ts and kind and summary):
        return None
    mentions = _coerce_mentions(raw.get("entities"), known_slugs)
    return ExtractedEvent(ts=ts, kind=kind, summary=summary, mentions=mentions)


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
    # Entities coerce FIRST: declared mentions are only kept when their
    # slug exists in the extraction's own entity set (unknown slugs are
    # dropped in _coerce_event).
    entities = [e for e in (_coerce_entity(x) for x in raw_entities) if e is not None]
    known_slugs = {e.slug for e in entities}
    return ExtractedGraph(
        events=[
            e
            for e in (_coerce_event(x, known_slugs) for x in raw_events)
            if e is not None
        ],
        entities=entities,
        relations=[r for r in (_coerce_relation(x) for x in raw_relations) if r is not None],
    )
def _render_prompt(
    template: str,
    title: str,
    transcript: str,
    known_entities: str = "",
    corrections: str = "",
) -> str:
    """Substitute {title} / {transcript} / {known_entities} /
    {corrections}. The contract requires ``{transcript}``; profiles.py
    already enforces that on load — and also enforces
    ``{known_entities}`` whenever a profile enables the known-entities
    lookup. ``{title}`` is optional; ``{corrections}`` renders only
    when the profile opts in by using the placeholder (no config
    knob — an unused placeholder renders empty)."""
    # Literal replacement of exactly four placeholders — NOT str.format:
    # profile prompts legitimately embed JSON schema examples with braces
    # ({"events": [...]}) which format() would read as replacement fields
    # and die with KeyError (observed live 2026-08-27 on a profile's
    # enrich prompt).
    return (
        template.replace("{title}", title or "")
        .replace("{transcript}", transcript)
        .replace("{known_entities}", known_entities)
        .replace("{corrections}", corrections)
    )


def extract_from_transcript(
    transcript_path: Path,
    title: str,
    prompt_template: str,
    cfg: Any,
    known_entities: str = "",
    corrections: str = "",
) -> ExtractedGraph:
    """One HTTP call to the chat endpoint; retry the same call twice if the
    model returns non-JSON or HTTP 5xx. Raises after the third failure —
    the activity catches and marks the stage failed.

    ``cfg.summarize`` is reused for the chat URL / model / auth: the
    same local llama-server (or LiteLLM proxy) backs both summarize
    and enrich — adding a second LLM config would just fork the
    deployment for no semantic gain.

    ``corrections`` is the pre-rendered ``{corrections}`` block
    (feedback_text of active graph edits for the tag); ``known_entities``
    is the pre-rendered block for the optional ``{known_entities}``
    placeholder (empty when the prompt doesn't use it — the activity
    skips the graph lookup entirely in that case).
    """
    transcript = transcript_path.read_text(encoding="utf-8")
    user_content = _render_prompt(
        prompt_template, title, transcript, known_entities, corrections
    )
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    messages = system_first_messages(
        [
            {"role": "system", "content": "Follow the user's instructions."},
            {"role": "user", "content": user_content},
        ]
    )

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
            payload = _loads_lenient(_json_payload(content))
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


def _loads_lenient(raw: str) -> Any:
    """json.loads with one repair pass: on JSONDecodeError, retry once on
    ``_repair_json(raw)``; if that still fails, re-raise the ORIGINAL
    error (it points at the text the model actually produced). A valid
    payload never passes through the repairer — zero mutation risk for
    the happy path."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_err:
        repaired = _repair_json(raw)
        if repaired != raw:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        raise first_err from None


def _json_payload(content: str) -> str:
    """Model output → raw JSON text. qwen3.6 via llama.cpp with server-side
    reasoning budget 0 leaks a bare `</think>` and wraps the JSON in ```json
    fences even when response_format=json_object is set — strip both or
    json.loads dies at char 0."""
    s = content.split("</think>")[-1].strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()

def _repair_json(s: str) -> str:
    """Best-effort repair of near-valid JSON the model emits despite
    response_format=json_object: trailing commas before ``}``/``]`` and
    unquoted object keys. The live failure mode (2026-09-04, pf1):
    ``Expecting property name enclosed in double quotes: line 93 column 1``
    — a dangling comma before the closing brace. Token-character walk
    with in-string tracking: repairs fire ONLY outside string literals,
    so a value like ``"text,\\n}"`` is never touched. A comma emits (or
    drops) only AFTER looking past the following whitespace: a ``}``/
    ``]`` there makes it trailing (dropped), an identifier followed by
    ``:`` makes it a key separator (emitted, key quoted). Returns the
    input unchanged when nothing repairable is found — the caller
    re-raises the ORIGINAL json.loads error in that case."""
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch in "{,":
            # Look past whitespace after `{`/`,`: `}`/`]` → a trailing
            # comma (drop it); an identifier + `:` → an unquoted key
            # (emit separator + quote the identifier). Both computed
            # BEFORE deciding, so the two repairs compose: keys after a
            # dropped comma still get quoted on the NEXT iteration.
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if ch == "," and j < n and s[j] in "}]":
                i += 1  # trailing comma: drop, keep whitespace
                continue
            m = _IDENT_RE.match(s, j)
            if m is not None:
                k = m.end()
                k2 = k
                while k2 < n and s[k2] in " \t\r\n":
                    k2 += 1
                if k2 < n and s[k2] == ":":
                    out.append(ch)  # the `{`/`,` separator, exactly once
                    out.append(s[i + 1 : j])  # whitespace kept verbatim
                    out.append('"')
                    out.append(m.group(0))
                    out.append('"')
                    out.append(s[k:k2])
                    out.append(":")
                    i = k2 + 1
                    continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
                "messages": system_first_messages(
                    [
                        {"role": "system", "content": "Answer with a single Y or N."},
                        {"role": "user", "content": prompt},
                    ]
                ),
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


# --- Phase 3-F F3: soft gate ahead of the dedup LLM batch ----------------------
# The live incident: extraction (2400 s budget) starved the trivial Y/N
# calls (30 s budget) in the SAME shared LiteLLM FIFO queue; every Y/N
# timed out and gray-zone pairs merged as "same". The gate sends ONE
# probe Y/N before the resolve_slugs batch: a healthy answer proves the
# queue serves small calls too; a ReadTimeout/429/5xx means the proxy is
# overloaded and the whole dedup-LLM batch would burn its per-call 30 s
# for nothing.

# Probe/backoff budget: 3 probes with a 60 s ×2 backoff (60 s, 120 s) —
# bounded ~3.5 min on top of the activity, priced well under the 2400 s
# envelope.
_GATE_TIMEOUT_SEC = 30.0
_GATE_BACKOFF_SEC = 60.0
_GATE_MAX_ATTEMPTS = 3


def _gate_probe(cfg: Any) -> bool:
    """ONE tiny Y/N call; True = proxy healthy for small requests.

    Raises on ReadTimeout/transport errors/429/5xx (the caller backs
    off and re-probes). A 2xx with an ambiguous body counts as healthy —
    the queue answered; parsing quality is ask_same_entity's problem.
    """
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.post(
        cfg.summarize.base_url.rstrip("/") + "/chat/completions",
        headers=headers,
        json={
            "model": cfg.summarize.model,
            "messages": system_first_messages(
                [
                    {"role": "system", "content": "Answer with a single Y or N."},
                    {"role": "user", "content": "Are X and X the same entity? Y or N."},
                ]
            ),
        },
        timeout=_GATE_TIMEOUT_SEC,
    )
    r.raise_for_status()
    r.json()["choices"][0]["message"]["content"]
    return True


def dedup_llm_gate(cfg: Any) -> bool:
    """True → proceed with LLM dedup; False → skip it (3 failed probes).

    Best-effort by design: a probe success does not promise the whole
    batch survives (ask_same_entity still swallows its own errors);
    a probe failure skips the LLM leg so the phase-2.5 prefilter alone
    decides, with the gray zone merging as "same" — exactly today's
    per-call error behavior, applied to the whole batch up front.
    """
    backoff = _GATE_BACKOFF_SEC
    for attempt in range(1, _GATE_MAX_ATTEMPTS + 1):
        try:
            _gate_probe(cfg)
            return True
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning(
                "enrich dedup gate: probe %d/%d failed (%s)",
                attempt,
                _GATE_MAX_ATTEMPTS,
                exc,
            )
            if attempt < _GATE_MAX_ATTEMPTS:
                time.sleep(backoff)
                backoff *= 2
    log.warning(
        "enrich dedup gate: proxy unhealthy after %d probes; "
        "skipping LLM dedup for this run (prefilter-only)",
        _GATE_MAX_ATTEMPTS,
    )
    return False



def _dedup_verdict(
    new_label: str,
    new_type: str,
    existing_label: str,
    existing_type: str,
    cfg: Any,
    new_vec: Any,
    existing_vec: Any,
    llm_enabled: bool = True,
) -> bool:
    """Phase 2.5 prefilter + LLM fallback for ONE collision pair.

    Both vectors present: cosine >= ``graph.embed_tau_high`` → same and
    cosine <= ``graph.embed_tau_low`` → distinct, with NO LLM call. The
    gray zone in between — and every missing-vector case — falls through
    to the classic ``ask_same_entity`` Y/N (errors → same, best-effort).
    With ``llm_enabled=False`` (Phase 3-F F3: the soft gate judged the
    proxy unhealthy) the gray zone merges as "same" WITHOUT the call —
    the exact verdict an errored ask_same_entity returns today.
    """
    decision = same_entity_decision(
        new_label,
        new_type,
        existing_label,
        existing_type,
        cfg,
        new_vec,
        existing_vec,
    )
    if decision == "same":
        return True
    if decision == "distinct":
        return False
    if not llm_enabled:
        return True
    return ask_same_entity(new_label, new_type, existing_label, existing_type, cfg)


def _safe_label(label: str) -> str:
    """Return ``label`` iff it matches Cypher label grammar, else "Entity".

    Cypher requires node/relationship labels to be valid identifiers;
    profile-supplied labels are user input and must be validated before
    being interpolated into a query string.
    """
    match = _SAFE_LABEL.match(label)
    return match.group(0) if match is not None else "Entity"


_SAFE_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def write_to_graph(
    rec_id: str,
    tag: str,
    graph: ExtractedGraph,
    node_labels: Any,
    graph_uri: str,
    graph_user: str,
    graph_password: str,
    graph_database: str,
    purge_origin: bool = True,
    recording_date: str = "",
    recording_title: str = "",
    embeddings: dict[str, list[float]] | None = None,
) -> int:
    """Single transaction, writing the extraction into the ONE namespace
    ``tag`` (the activity calls this once per namespace):

    1. ``MATCH (n {origin_recording_id: $rec}) DETACH DELETE n`` —
       idempotency (a re-run produces the same state, never duplicates).
       The match is on the ``origin_recording_id`` PROPERTY, deliberately
       NOT scoped to ``tag``: the first purge (``purge_origin=True``)
       removes this recording's copies from EVERY namespace at once, so
       tags edited between regenerates leave no stale copies behind.
       Namespace calls 2..N pass ``purge_origin=False`` — deleting again
       is a no-op anyway (nothing matches), but skipping the query keeps
       N namespaces from doing N full scans.
       Leak audit 2026-08-28: the ONLY writer of graph nodes is this
       function; every node carries ``origin_recording_id`` (entity,
       event, and every MERGE/CREATE branch below), so the property-scoped
       DELETE provably reaches all of them. No other write path exists →
       no leak.
    2. ``MERGE`` entities by ``(tag, slug)``. The dedup loop above has
       already resolved same-entity collisions and re-slugged the new
       candidate (``-2``, ``-3`` suffix). Carry ``origin_recording_id``
       on every node for the same reason: the next run can purge them
       cleanly.
    3. ``CREATE`` events with origin + tag + ``recording_date`` (ISO-8601
       UTC string from coalesce(recorded_at, created_at)) +
       ``recording_title`` — the timeline keys Phase 1 digests/clients
       read. Events are NOT merged — each recording's events are unique
       by construction, and a new run replaces the previous batch (the
       DETACH DELETE above).
    4. ``MERGE`` ``(:Event)-[:MENTIONS]->(:Entity)`` and
       ``(:Entity)-[:REL {type}]->(Entity)``.

    Phase 2.5: when ``embeddings`` (FINAL slug → vector) is given, every
    entity MERGE sets ``e.embedding`` ON CREATE only — a node merged
    from a previous recording keeps its original vector (recurrence
    doesn't change identity). ``ensure_vector_index`` runs first
    (cheap IF NOT EXISTS) so fresh installs self-provision the ANN
    index the next embedder batch will query.

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
        if embeddings is not None:
            # Idempotent (IF NOT EXISTS) and outside the write
            # transaction: self-provisions the ANN index on fresh
            # installs; a no-op on the live graph (index already ONLINE).
            ensure_vector_index(driver, graph_database)
        with (
            driver.session(database=graph_database) as session,
            session.begin_transaction() as tx,
        ):
            # Phase 2.5: the embedding clause exists ONLY when the
            # caller passed vectors — with ``embeddings=None`` (model
            # off/unavailable) the property is never touched at all.
            # Frozen at first sight: ON CREATE only, ON MATCH
            # deliberately leaves the existing vector — a recurring
            # entity keeps the embedding its node was born with.
            embed_clause = (
                "ON CREATE SET e.embedding = $embedding " if embeddings is not None else ""
            )
            entity_query = cast(
                "Any",
                (
                    f"MERGE (e:`{safe_entity_label}` {{tag: $tag, slug: $slug}}) "
                    "ON CREATE SET e.label = $label, e.type = $type, "
                    "e.origin_recording_id = $rec, e.first_seen_recording = $rec, "
                    "e.recording_ids = [$rec] "
                    + embed_clause +
                    # Multi-recording provenance: a shared entity MERGEs onto
                    # one node, so origin_recording_id alone can never show
                    # recurrence — digests read recording_ids instead.
                    # Phase 4: a user-corrected label/type is authoritative —
                    # a recurring extraction must not stomp the user's edit
                    # back onto the ASR label ("Валли" → "Валя" regression).
                    "ON MATCH SET e.label = CASE WHEN coalesce(e.user_corrected, false) "
                    "THEN e.label ELSE $label END, "
                    "e.type = CASE WHEN coalesce(e.user_corrected, false) "
                    "THEN e.type ELSE $type END, "
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
                    "event_key: $event_key, "
                    "recording_date: $recording_date, recording_title: $recording_title, "
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
                # Phase 4: a user-corrected node must survive ANY
                # regenerate — coalesce(...) = false matches both a
                # false flag and a missing property; only true exempts.
                # Without this, a single-recording tag (every node
                # origin-authored by the ONE recording) loses the user's
                # label edit on every regenerate — the exact regression
                # phase 4 exists to prevent.
                "MATCH (n {origin_recording_id: $rec}) "
                "WHERE coalesce(n.user_corrected, false) = false "
                "DETACH DELETE n",
            )
            if purge_origin:
                tx.run(delete_query, rec=rec_id)
            # Phase A event keys: deterministic identity within ONE
            # generation (see ``compute_event_keys``). Computed once for
            # the whole recording so the artifact and every namespace
            # copy share identical keys (occurrence suffixes stable by
            # list order).
            event_keys = compute_event_keys(rec_id, graph.events)

            # First pass: entities. Build a slug → id map via MERGE so
            # the second pass (relations) can refer to them by key.
            slug_to_node: dict[str, str] = {}
            for ent in graph.entities:
                entity_params: dict[str, Any] = {
                    "tag": tag,
                    "slug": ent.slug,
                    "label": ent.label,
                    "type": ent.type,
                    "rec": rec_id,
                }
                if embeddings is not None:
                    # Missing key → None → Cypher drops any stale property
                    # instead of writing a corrupt vector.
                    entity_params["embedding"] = embeddings.get(ent.slug)
                node = tx.run(entity_query, **entity_params).single()
                if node is not None:
                    slug_to_node[ent.slug] = node[0]
            # Second pass: events.
            event_ids: list[str] = []
            for ev, ev_key in zip(graph.events, event_keys, strict=False):
                node = tx.run(
                    event_query,
                    ts=ev.ts,
                    kind=ev.kind,
                    summary=ev.summary,
                    event_key=ev_key,
                    tag=tag,
                    rec=rec_id,
                    recording_date=recording_date,
                    recording_title=recording_title,
                ).single()
                if node is not None:
                    event_ids.append(node[0])

            # Third pass: edges. MENTIONS from events to entities via
            # the SAME resolver the events.json artifact uses
            # (``_event_mentions``): model-declared mentions win when the
            # wire event carried ``entities: [...]``, the case-
            # insensitive word-boundary label match is the fallback — so
            # the artifact and the graph can never disagree. Then REL
            # between entities.
            for ev_id, ev in zip(event_ids, graph.events, strict=False):
                for slug in _event_mentions(ev, graph.entities):
                    node_id = slug_to_node.get(slug)
                    if node_id is not None:
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


def rename_entity_in_graph(
    tag: str,
    slug: str,
    label: str,
    type_: str | None,
    cfg: Any,
    graph_uri: str,
    graph_user: str,
    graph_password: str,
    graph_database: str,
) -> dict[str, Any]:
    """Phase 4: user-initiated entity rename — set label (+ optional type)
    and ``user_corrected: true`` on the ONE node ``(Entity {tag, slug})``.

    The slug NEVER changes (it is the node identity: REL edges,
    known_entities rendering, events.json mentions all key on it); only
    the display label moves. ``user_corrected`` arms the dedup guard —
    resolve_slugs will never auto-merge this node again — and the
    write_to_graph ON MATCH clause will not stomp the corrected label
    on recurrence.

    Re-embedding: a node that CARRIES an embedding gets the new label's
    vector written in the same transaction (a stale vector would keep
    answering the 2.5 cosine prefilter with the OLD name's geometry —
    the exact drift the rename is supposed to fix). A node WITHOUT one
    gets a vector now when the embedder answers (cheap — the same one
    embed_texts call already pays for the re-embed case; the node joins
    the ANN index early instead of waiting for the next enrich MERGE
    ON CREATE).

    Returns ``{"ok": bool, "re_embedded": bool}``; ``ok=False`` means
    the (tag, slug) pair matches nothing — the API has already 404'd on
    the events.json aggregation, but the graph can still disagree (an
    entity mentioned only in events, a namespace write that failed).
    """
    driver = GraphDatabase.driver(graph_uri, auth=(graph_user, graph_password))
    try:
        with driver.session(database=graph_database) as session:
            # Snapshot FIRST: does the node exist, does it carry a vector.
            row = session.run(
                "MATCH (e {tag: $tag, slug: $slug}) "
                "RETURN e.embedding AS embedding LIMIT 1",
                tag=tag,
                slug=slug,
            ).single()
            if row is None:
                return {"ok": False, "re_embedded": False}
            had_embedding = row["embedding"] is not None
        new_vec: list[float] | None = None
        if had_embedding or getattr(cfg.graph, "embed_enabled", None) is True:
            new_vec = _embed_one(label, cfg)
        with (
            driver.session(database=graph_database) as session,
            session.begin_transaction() as tx,
        ):
            type_clause = ", e.type = $type" if type_ is not None else ""
            tx.run(
                "MATCH (e {tag: $tag, slug: $slug}) "
                "SET e.label = $label" + type_clause + ", "
                "e.user_corrected = true"
                + (", e.embedding = $vec" if new_vec is not None else ""),
                tag=tag,
                slug=slug,
                label=label,
                type=type_,
                vec=new_vec,
            )
        return {"ok": True, "re_embedded": new_vec is not None}
    finally:
        driver.close()


def _embed_one(text: str, cfg: Any) -> list[float] | None:
    """Embed ONE label via the shared client; None when the backend is
    off/unavailable (the caller then leaves the node vectorless)."""
    from .embeddings import embed_texts

    try:
        vecs = embed_texts([text], cfg)
        return vecs[0] if vecs else None
    except Exception:
        log.exception("rename_entity: embedding failed; leaving node vectorless")
        return None


def _event_mentions(event: ExtractedEvent, entities: list[ExtractedEntity]) -> list[str]:
    """Slugs of entities the event references.

    Phase 2: model-declared mentions (wire ``entities: [slug, ...]``,
    carried on ``event.mentions``) win verbatim when present — the model
    saw the known-entities block and is the best judge of what it
    mentioned. Fallback (no declared list): the Phase 1 label-occurrence
    heuristic — word-boundary, case-insensitive; bare substring
    containment would link "Orc" to "Orcus"/"orchestra". BOTH the
    ``meta/events.json`` artifact and the ``write_to_graph`` MENTIONS
    edges resolve through this function, so they can never disagree.
    """
    if event.mentions:
        return list(event.mentions)
    summary_lower = event.summary.lower()
    return [
        ent.slug
        for ent in entities
        if ent.label
        and re.search(r"\b" + re.escape(ent.label.lower()) + r"\b", summary_lower)
    ]


def write_events_json(
    path: Path,
    *,
    recording_id: str,
    recording_date: str,
    recording_title: str,
    profile_id: str,
    namespaces: list[str],
    resolved: ExtractedGraph,
    corrected_labels: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Write the recording's timeline artifact ``meta/events.json``.

    Atomic (unique-tmp + ``os.replace`` — the same idiom as the digest
    note and export artifacts) so a regenerate can never serve a torn
    file. The shape is the locked Phase 1 client contract:

    ``{recording_id, recording_date (ISO-8601 UTC),
    recording_title, profile_id, namespaces,
    events: [{event_key, ts, kind, summary, mentions}],
    entities: [{slug, label, type}],
    relations: [{from, to, type}]}``

    ``mentions`` per event: slugs whose label the summary references —
    see ``_event_mentions`` (mirrors the graph's MENTIONS edges).
    ``entities``/``relations`` come from the FIRST namespace's resolved
    extraction (namespaces are copies; the first write also purged).
    ``corrected_labels`` (phase 4): slug → (label, type) overrides from
    user-corrected graph nodes — the artifact must display the user's
    label ("Валли"), not the fresh extraction's ASR guess, so the
    timeline stays in sync with the graph after a rename.
    """
    corrected = corrected_labels or {}
    event_keys = compute_event_keys(recording_id, resolved.events)
    payload = {
        "recording_id": recording_id,
        "recording_date": recording_date,
        "recording_title": recording_title,
        "profile_id": profile_id,
        "namespaces": list(namespaces),
        "events": [
            {
                "event_key": ev_key,
                "ts": ev.ts,
                "kind": ev.kind,
                "summary": ev.summary,
                "mentions": _event_mentions(ev, resolved.entities),
            }
            for ev, ev_key in zip(resolved.events, event_keys, strict=False)
        ],
        "entities": [
            {
                "slug": e.slug,
                "label": corrected.get(e.slug, (e.label, ""))[0],
                "type": corrected.get(e.slug, ("", e.type))[1],
            }
            for e in resolved.entities
        ],
        "relations": [
            {"from": r.from_slug, "to": r.to_slug, "type": r.type}
            for r in resolved.relations
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def resolve_slugs(
    graph: ExtractedGraph,
    cfg: Any,
    tag: str,
    existing_lookup: ExistingEntityLookup | None = None,
    llm_enabled: bool = True,
) -> ExtractedGraph:
    """Run the two-level dedup loop on ``graph.entities``.

    Level 1 (cheap): group by slug. Within each group, the FIRST
    entity is the canonical; subsequent collisions are candidate
    duplicates.

    Level 2 (LLM, per pair): for each collision, ask "same entity?"
    using both labels and types. YES → drop the candidate (it merges
    into the first). NO → re-slug the candidate with ``-2``, ``-3``,
    ... until it stops colliding.

    Phase 2.5 embedding prefilter: when ``graph.embed_enabled`` and the
    ONNX model is available, each collision first consults
    ``same_entity_decision`` — cosine >= tau_high merges without the
    LLM, cosine <= tau_low splits without it, and only the gray zone
    (or a missing vector) reaches the LLM Y/N call. All labels are
    embedded in ONE batch up front; live-graph comparison vectors come
    from the existing node's stored ``embedding`` (read by the same
    MATCH in ``ExistingEntityLookup``).

    ``existing_lookup(slug)`` lets the caller pre-seed collisions with
    entities already in the graph from previous recordings — same
    question, same answer.

    Phase 3-F F3: ``llm_enabled=False`` (the soft gate's
    dedup_llm_probe failed 3×) keeps the prefilter zones but resolves
    every gray-zone pair as "same" without any LLM call — identical to
    per-call error semantics, applied up front.
    """
    # One batched inference for the whole extraction, in entity ORDER
    # (slugs collide, positions don't). ``local_vecs`` answers
    # within-extraction comparisons; ``seen_vecs`` remembers the vector
    # of each surviving canonical so later collisions of the same slug
    # compare against it.
    local_vecs: list[list[float]] = []
    seen_vecs: dict[str, list[float]] = {}
    # Config-level off → the embedding machinery is not even consulted
    # (and MagicMock test configs, whose attributes are not literally
    # True, take the pure-LLM path). Phase 3.5: the batch goes through
    # the single embed_texts client — local ONNX or http backend alike.
    if getattr(cfg.graph, "embed_enabled", None) is True:
        try:
            from .embeddings import embed_texts

            vecs = embed_texts([ent.label for ent in graph.entities], cfg)
            if vecs is not None:
                local_vecs = vecs
        except Exception:
            log.exception("enrich: embedding batch failed; falling back to LLM dedup")
            local_vecs = []
    seen: dict[str, ExtractedEntity] = {}
    out: list[ExtractedEntity] = []
    # Pre-resolution slug → resolved slug, so relations (authored against
    # the raw extraction) can be re-anchored after dedup/re-slugging.
    # Keyed by SLUG, not label: labels are not slug-safe (case, punct,
    # spaces) and two entities may share one label.
    remap: dict[str, str] = {}
    for idx, ent in enumerate(graph.entities):
        # Re-slug up front so labels differing only in case/punct join
        # the same group cheaply.
        slug = ent.slug
        orig_slug = ent.slug
        vec = local_vecs[idx] if idx < len(local_vecs) else None
        same = None
        if slug in seen:
            existing = seen[slug]
            same = _dedup_verdict(
                ent.label,
                ent.type,
                existing.label,
                existing.type,
                cfg,
                vec,
                seen_vecs.get(slug),
                llm_enabled=llm_enabled,
            )
        elif existing_lookup is not None:
            existing = existing_lookup(slug)
            if existing is not None:
                if existing.get("user_corrected"):
                    # Phase 4 dedup guard: a user-corrected node is
                    # authoritative — neither the cosine prefilter nor
                    # the LLM Y/N may merge into it (and the gray-zone
                    # "merge on error" path must not either). Treat as
                    # distinct and step past; one log line per hit.
                    log.info(
                        "dedup: %s/%s is user-corrected; keeping distinct",
                        tag,
                        slug,
                    )
                    same = False
                else:
                    same = _dedup_verdict(
                        ent.label,
                        ent.type,
                        existing["label"],
                        existing["type"],
                        cfg,
                        vec,
                        existing.get("embedding"),
                        llm_enabled=llm_enabled,
                    )
                if same:
                    seen[slug] = ExtractedEntity(
                        slug=existing["slug"], label=existing["label"], type=existing["type"]
                    )
                    out.append(seen[slug])
                    remap.setdefault(orig_slug, existing["slug"])
                    # The canonical lives in the GRAPH: its stored vector
                    # is the one future same-slug candidates compare to.
                    if existing.get("embedding"):
                        seen_vecs[slug] = existing["embedding"]
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
        if vec is not None:
            seen_vecs[slug] = vec
    # Re-anchor relations to the resolved slugs (pre-resolution slug ->
    # resolved slug); relations referencing dropped/renamed duplicates
    # follow their entity's final slug, unknown slugs pass through.
    out_relations: list[ExtractedRelation] = []
    for rel in graph.relations:
        f = remap.get(rel.from_slug, rel.from_slug)
        t = remap.get(rel.to_slug, rel.to_slug)
        out_relations.append(ExtractedRelation(from_slug=f, to_slug=t, type=rel.type))
    return ExtractedGraph(events=graph.events, entities=out, relations=out_relations)

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

    def __call__(self, slug: str) -> dict[str, Any] | None:
        with self._driver.session(database=self._database) as session:
            row = session.run(
                "MATCH (e {tag: $tag, slug: $slug}) "
                # $rec = '' (tests/legacy callers) keeps every node in play;
                # a null origin_recording_id (foreign/manually created node)
                # must not silently opt out of dedup either.
                "WHERE $rec = '' OR coalesce(e.origin_recording_id, '') <> $rec "
                "RETURN e.label AS label, e.type AS type, e.slug AS slug, "
                # Phase 4: user_corrected rides along so resolve_slugs can
                # refuse to auto-merge a user-renamed node.
                "e.embedding AS embedding, "
                "coalesce(e.user_corrected, false) AS user_corrected LIMIT 1",
                tag=self._tag,
                slug=slug,
                rec=self._exclude_rec,
            ).single()
        if row is None:
            return None
        return {
            "label": row["label"],
            "type": row["type"],
            "slug": row["slug"],
            # Phase 2.5: the live-graph side of the cosine prefilter —
            # None on nodes written before the embedding phase.
            "embedding": row["embedding"],
            # Phase 4: false on every legacy/test row (coalesce).
            "user_corrected": bool(row["user_corrected"]),
        }


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


def user_corrected_labels(
    graph_uri: str,
    graph_user: str,
    graph_password: str,
    graph_database: str,
    tag: str,
) -> dict[str, tuple[str, str]]:
    """Slug → (label, type) of user-corrected nodes in the namespace.

    Phase 4 rename overlay for ``write_events_json``: after a rename the
    graph node carries the authoritative label, but a later regenerate
    re-writes ``events.json`` from the fresh extraction, whose ASR label
    ("Valiy") would resurface in the timeline. The activity overlays
    these rows so the artifact stays in sync with the graph. Empty dict
    on any graph failure — best-effort, never fails enrich.
    """
    try:
        driver = GraphDatabase.driver(graph_uri, auth=(graph_user, graph_password))
        try:
            with driver.session(database=graph_database) as session:
                rows = session.run(
                    "MATCH (e {tag: $tag}) WHERE coalesce(e.user_corrected, false) "
                    "RETURN e.slug AS slug, e.label AS label, e.type AS type",
                    tag=tag,
                ).data()
        finally:
            driver.close()
    except Exception:
        log.exception("enrich: user-corrected overlay lookup failed; skipping")
        return {}
    return {r["slug"]: (r["label"], r["type"]) for r in rows}


def render_known_entities(rows: list[dict[str, str]]) -> str:
    """Render the ``{known_entities}`` prompt block.

    One ``- slug — label (type)`` line per row; an empty snapshot
    renders an EMPTY string (2026-08-30: the former literal ``(none)``
    made qwen3.6-35b deterministically emit broken JSON — a doubled
    ``{`` right after the events array — on the daily-blob regenerate;
    the same prompt with an empty block parses 3/3. Empty input must
    read exactly like a disabled lookup). ``rows`` comes from
    ``list_known_entities`` (the pre-extraction snapshot of the target
    namespace).
    """
    return "\n".join(
        f"- {row['slug']} — {row['label']} ({row['type']})" for row in rows
    )


def render_corrections(items: list[str]) -> str:
    """Render the ``{corrections}`` prompt block (phase B).

    One ``- text`` line per active feedback_text. Same empty-input
    rule as ``render_known_entities``: no corrections → EMPTY string
    (a literal ``(none)`` block historically breaks qwen3.6 JSON
    output — see the note there). Caps live in the caller
    (``_CORRECTIONS_MAX_ITEMS`` / ``_CORRECTIONS_MAX_CHARS``): the
    rendered block must stay bounded no matter how many edits
    accumulate.
    """
    return "\n".join(f"- {text}" for text in items)


# Phase B caps (critic finding #7): top-20 most-recent feedback items,
# whole block ~2000 chars — the prompt stays bounded as edits
# accumulate; older items silently age out of the block (the audit UI
# can retire them entirely).
_CORRECTIONS_MAX_ITEMS = 20
_CORRECTIONS_MAX_CHARS = 2000


def active_corrections_for_tags(tags: list[str]) -> list[str]:
    """feedback_text of ACTIVE edits (status=applied, feedback present)
    for ``tags``, most recent first, capped by the phase-B constants.

    Postgres lookup (graph_edits), not Neo4j: corrections are user
    decisions, not extracted facts. Lazy ``worker.db`` import — the
    same pattern ``graph_edit.reapply_overlay`` uses (import at module
    top would tie the enrich module to the DB engine lifecycle).
    Called from the enrich activity in a worker thread; any failure
    returns [] — corrections are best-effort prompt enhancement and
    must NEVER fail the stage.
    """
    from .db import EditStatus, GraphEdit, session

    try:
        with session() as db:
            rows = (
                db.query(GraphEdit)
                .filter(
                    GraphEdit.tag.in_(tags),
                    GraphEdit.status == EditStatus.applied,
                    GraphEdit.feedback_text.isnot(None),
                )
                .order_by(GraphEdit.created_at.desc())
                .limit(_CORRECTIONS_MAX_ITEMS)
                .all()
            )
    except Exception:
        log.exception("enrich: corrections lookup failed; continuing without")
        return []
    items = [r.feedback_text for r in rows if r.feedback_text]
    block: list[str] = []
    total = 0
    for text in items:
        if total + len(text) > _CORRECTIONS_MAX_CHARS and block:
            break
        block.append(text)
        total += len(text)
    return block




def list_known_entities(
    graph_uri: str,
    graph_user: str,
    graph_password: str,
    graph_database: str,
    tag: str,
    exclude_rec: str = "",
    limit: int = 25,
) -> list[dict[str, str]]:
    """Top-``limit`` (slug, label, type) rows already present in
    namespace ``tag`` — the pre-extraction snapshot that feeds the
    ``{known_entities}`` prompt block so the model reuses established
    slugs instead of minting near-duplicates.

    Same exclusion rule as the dedup lookup: nodes authored by
    ``exclude_rec`` are invisible (a regenerate must not be steered by
    the very nodes it is about to delete). Ordering: most-recurring
    first (``recording_ids`` length), slug as tiebreak — "top" entities
    are the ones other recordings already reference. Event nodes carry
    no ``slug`` and are excluded by the ``IS NOT NULL`` guard
    (selection stays property-based: node labels are
    profile-overridable).
    """
    driver = GraphDatabase.driver(graph_uri, auth=(graph_user, graph_password))
    try:
        with driver.session(database=graph_database) as session:
            rows = session.run(
                "MATCH (e {tag: $tag}) "
                "WHERE e.slug IS NOT NULL "
                "AND ($rec = '' OR coalesce(e.origin_recording_id, '') <> $rec) "
                "RETURN e.slug AS slug, e.label AS label, e.type AS type "
                "ORDER BY size(coalesce(e.recording_ids, [])) DESC, e.slug "
                "LIMIT $limit",
                tag=tag,
                rec=exclude_rec,
                limit=limit,
            )
            return [
                {"slug": r["slug"], "label": r["label"], "type": r["type"]}
                for r in rows
            ]
    finally:
        driver.close()
