"""Tag-scoped endpoints.

Wave C: POST /tags/{tag}/digest builds a markdown digest note of the
last N done recordings carrying the tag; GET /tags/{tag}/digest serves
the generated note back as text/markdown. Phase 3: GET
/tags/{tag}/timeline returns the tag's done sessions (newest first)
with their meta/events.json events, aggregated entities and the
digest-generated flag — served from Postgres + events.json only, no
graph session.

Phase 3.5: GET /tags/{tag}/search — semantic KNN over the tag's
sqlite-vec index (built by the worker's enrich/backfill). The query is
embedded through the same backend config the worker indexed with; a
missing index, unavailable backend or backend/model mismatch replies
503 with ``available: false`` + a backfill hint, never a silent
cross-vector-space search.

Tag normalization mirrors recordings.py: trim + lowercase. After that the
regex pins down file-system-safe forms (the digest activity writes
``digests/<tag>.md`` under the transcripts dir) — anything outside the
regex returns 400, with the same rationale the recordings module uses:
empty/whitespace tags are garbage rather than meaningful labels.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from temporalio.service import RPCError

from app import temporal_client
from app.config import ServerConfig
from app.db import EditOp, EditStatus, EditTarget, GraphEdit, get_session
from app.embeddings import embed_query, expected_index_meta
from app.semantic_index import index_status, knn_search
from app.vault import _tag_recordings, find_digest, scan_timeline

router = APIRouter(prefix="/tags")

_LOG = logging.getLogger("transcripter.api.tags")


# After trim+lowercase: must start with a word character (Unicode letter,
# digit or underscore), then word chars, spaces, dots, underscores, dashes
# — free-form user grouping per Phase 0 (a tag like "dnd dark castle" or
# «мой замок» is fine). First char not a space; ≤64 chars so the
# slugified digest filename always fits a filename segment. EXACT TWIN of
# worker digest._SAFE_TAG_RE — the worker must accept everything this
# regex accepts; change the two IN SYNC.
_TAG_RE = re.compile(r"^[\w][ \w.-]{0,63}$", re.UNICODE)


class DigestRequest(BaseModel):
    last_n: int = Field(default=5, ge=1, le=50)


def _normalize_tag(raw: str) -> str:
    """Trim + lowercase. Mirrors recordings._normalize_tags semantics
    (one element)."""
    return raw.strip().lower()


def _validate_tag(tag: str) -> None:
    """Apply the regex; 400 on anything outside it.

    Why here and not in the worker: the activity would already produce a
    clear ValueError, but only after a Postgres pull + a (possibly
    expensive) Neo4j session — failing at the boundary is cheaper and
    gives the user instant feedback in the UI.
    """
    if not tag:
        raise HTTPException(status_code=400, detail="tag is empty")
    if not _TAG_RE.match(tag):
        raise HTTPException(
            status_code=400,
            detail=(
                "tag must match ^[\\w][ \\w.-]{0,63}$ (unicode word chars, "
                "spaces, dots, underscores, dashes; must not start with a space)"
            ),
        )


def _tag_exists(tag: str) -> bool:
    """Whether any DONE recording carries the tag (search's 404 rule —
    the same done-only semantics as the timeline)."""
    gen = get_session()
    try:
        session = next(gen)
        return len(_tag_recordings(session, tag)) > 0
    finally:
        gen.close()


@router.get("/{tag}/search")
def get_search(
    tag: Annotated[str, Path()],
    request: Request,
    q: Annotated[str, Query(min_length=1)],
    k: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict:
    """Phase 3.5: semantic search over the tag's indexed transcript
    segments (worker-built ``<transcripts>/indexes/<tag>.sqlite``).

    404 unknown tag (no recordings carry it — same rule as timeline);
    503 ``{available: false}`` when the embedding backend is missing or
    the index file's {backend, model, dimensions} meta does not match
    the active config (model switch → run backfill)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    # embed first: a dead backend fails fast regardless of index state
    try:
        query_vec = embed_query(q.strip(), cfg)
    except Exception:  # noqa: BLE001 — backend down = unavailable, not a 500
        _LOG.warning("search: embedding backend failed for tag=%s", norm, exc_info=True)
        query_vec = None
    if query_vec is None:
        raise HTTPException(
            status_code=503,
            detail={
                "available": False,
                "reason": "embedding backend unavailable",
            },
        )
    status = index_status(cfg.vault.path, norm)
    if status is None or status["segments"] == 0:
        if not _tag_exists(norm):
            raise HTTPException(
                status_code=404, detail=f"no recordings for tag {norm}"
            )
        raise HTTPException(
            status_code=503,
            detail={
                "available": False,
                "reason": (
                    "no semantic index for this tag — index it first "
                    "(new recordings index automatically; run "
                    "`docker compose exec worker python -m worker.backfill_index` "
                    "for old ones)"
                ),
            },
        )
    expected = expected_index_meta(cfg)
    if any(status["meta"].get(key) != value for key, value in expected.items()):
        raise HTTPException(
            status_code=503,
            detail={
                "available": False,
                "reason": (
                    "index built with a different embedding backend/model "
                    f"({status['meta']}); re-index with "
                    "`docker compose exec worker python -m worker.backfill_index`"
                ),
            },
        )
    hits = knn_search(cfg.vault.path, norm, query_vec, k=k)
    return {
        "tag": norm,
        "query": q.strip(),
        "hits": [
            {
                "recording_id": h["recording_id"],
                "session_title": h["session_title"],
                "ts_start": h["ts_start"],
                "ts_end": h["ts_end"],
                "speaker": h["speaker"],
                "snippet": h["text"],
                "distance": h["distance"],
            }
            for h in hits
        ],
    }


@router.get("/{tag}/timeline")
def get_timeline(
    tag: Annotated[str, Path()],
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Phase 3: per-tag session timeline (newest first) + aggregated
    entities + whether a digest note exists. Postgres + events.json
    artifacts only — no graph session, so a timeline renders with the
    graph profile off."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    payload = scan_timeline(cfg, session, norm)
    if not payload["sessions"]:
        raise HTTPException(404, detail=f"no recordings for tag {norm}")
    return payload


@router.post("/{tag}/digest", status_code=202)
async def post_digest(
    body: DigestRequest,
    request: Request,
    tag: Annotated[str, Path()],
) -> dict:
    cfg: ServerConfig = request.app.state.config
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    if not cfg.graph.enabled:
        # Same UX shape as the recordings PATCH-409: a concrete error
        # the client can surface in the UI, not a Temporal 500 cascade.
        raise HTTPException(
            status_code=409,
            detail=(
                "graph backend not configured (graph.uri empty) — start the "
                "compose graph profile or set graph.uri in config.yaml"
            ),
        )
    try:
        # Attribute access (not ``from app.temporal_client import start_digest``)
        # so the conftest's monkeypatch on the module attribute is honored.
        workflow_id = await temporal_client.start_digest(norm, body.last_n)
    except Exception:  # noqa: BLE001 — same blind-catch shape as recordings.update_recording
        # Temporal being unreachable should not look like a 500 to the
        # client: same shape as PATCH rename uses (log + 503).
        _LOG.exception("start_digest failed for tag=%s", norm)
        raise HTTPException(
            status_code=503, detail="temporal unavailable; try again later"
        )
    return {"workflow_id": workflow_id, "tag": norm, "last_n": body.last_n}


@router.get("/{tag}/digest")
def get_digest(tag: Annotated[str, Path()], request: Request) -> FileResponse:
    """Serve the generated digest note for a tag (Phase 1).

    The worker names files by slug, so the API cannot reconstruct the
    filename from the raw tag — lookup is frontmatter matching, shared
    with the vault scan (app.vault.find_digest). No graph required:
    reading a note must work even with the graph profile off.
    """
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    md = find_digest(request.app.state.config, norm)
    if md is None:
        raise HTTPException(
            status_code=404, detail=f"digest not generated yet for tag {norm}"
        )
    return FileResponse(md, media_type="text/markdown")


class EntityPatchRequest(BaseModel):
    """Phase 4: label is required (1..200 chars after trim); type is
    optional — absent means "leave as is", never "clear"."""

    label: str = Field(min_length=1, max_length=200)
    type: str | None = Field(default=None, max_length=100)


@router.patch("/{tag}/entities/{slug}", status_code=202)
async def patch_entity(
    body: EntityPatchRequest,
    request: Request,
    tag: Annotated[str, Path()],
    slug: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase 4: rename ONE entity (label ± type) in the tag's namespace.

    The slug is identity (REL edges, known_entities, events.json
    mentions) and never changes; the label is display-only and moves.
    The worker arms the node with ``user_corrected: true`` so the dedup
    loop stops auto-merging it and future enrich runs keep the edit.

    Existence check: the SAME aggregation the timeline GET serves
    (events.json across the tag's DONE recordings) — the entity a user
    can click is exactly one of those rows. A slug that lives only in
    the graph (never in events.json) is not visible in any UI and
    therefore not renamable from here; the worker treats a missing node
    as non-retryable.

    202 like digest: the write is a Temporal workflow
    (start_rename_entity → RenameEntity → rename_entity activity); the
    graph lands asynchronously and the client applies the optimistic
    label immediately.
    """
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label must not be empty")
    cfg: ServerConfig = request.app.state.config
    if not cfg.graph.enabled:
        # Same UX shape as POST /digest: a concrete operator-facing
        # error, not a Temporal 500 cascade one hop later.
        raise HTTPException(
            status_code=409,
            detail=(
                "graph backend not configured (graph.uri empty) — start the "
                "compose graph profile or set graph.uri in config.yaml"
            ),
        )
    payload = scan_timeline(cfg, session, norm)
    if not payload["sessions"]:
        raise HTTPException(status_code=404, detail=f"no recordings for tag {norm}")
    slugs = {row["slug"] for row in payload["entities"]}
    if slug not in slugs:
        raise HTTPException(
            status_code=404, detail=f"entity {slug} not found in tag {norm}"
        )
    try:
        workflow_id = await temporal_client.start_rename_entity(
            norm, slug, label, body.type
        )
    except Exception:  # noqa: BLE001 — same blind-catch shape as post_digest
        _LOG.exception("start_rename_entity failed for %s/%s", norm, slug)
        raise HTTPException(
            status_code=503, detail="temporal unavailable; try again later"
        )
    return {"workflow_id": workflow_id, "tag": norm, "slug": slug, "label": label}


# ------------------------- Phase A: graph editing -------------------------


class EventPatchRequest(BaseModel):
    """PATCH one event: any of ts/kind/summary/mentions; absent fields
    are left as-is. ``feedback_text`` (optional, ≤500 chars) is the NL
    rule stored for the enrich prompt block (Phase B)."""

    ts: str | None = Field(default=None, max_length=32)
    kind: str | None = Field(default=None, max_length=100)
    summary: str | None = Field(default=None, max_length=2000)
    mentions: list[str] | None = None
    feedback_text: str | None = Field(default=None, max_length=500)


class RelationCreateRequest(BaseModel):
    from_slug: str = Field(min_length=1, max_length=200)
    to_slug: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    feedback_text: str | None = Field(default=None, max_length=500)


class RelationDeleteRequest(BaseModel):
    from_slug: str = Field(min_length=1, max_length=200)
    to_slug: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    feedback_text: str | None = Field(default=None, max_length=500)


class EntityDeleteRequest(BaseModel):
    feedback_text: str | None = Field(default=None, max_length=500)


class EntityMergeRequest(BaseModel):
    source_slug: str = Field(min_length=1, max_length=200)
    target_slug: str = Field(min_length=1, max_length=200)
    feedback_text: str | None = Field(default=None, max_length=500)


def _require_graph(cfg: ServerConfig, tag: str) -> None:
    """409 with the operator-facing detail when the graph backend is
    off — same UX shape as POST /digest and PATCH entities."""
    if not cfg.graph.enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "graph backend not configured (graph.uri empty) — start the "
                "compose graph profile or set graph.uri in config.yaml"
            ),
        )


def _timeline_or_404(cfg: ServerConfig, session: Session, tag: str) -> dict:
    payload = scan_timeline(cfg, session, tag)
    if not payload["sessions"]:
        raise HTTPException(status_code=404, detail=f"no recordings for tag {tag}")
    return payload


def _start_edit_workflow(
    session: Session,
    *,
    tag: str,
    target: EditTarget,
    op: EditOp,
    obj_key: str,
    before: dict,
    after: dict,
    anchor: dict,
    feedback_text: str | None,
    source: str = "user",
) -> int:
    """Insert the graph_edits row (applied-pending) and start the
    ApplyGraphEdit workflow. Returns the edit id. Raises 503 on Temporal
    failure AFTER the row insert — the row stays and the audit UI shows
    it as not-yet-applied (re-triggerable later)."""
    row = GraphEdit(
        tag=tag,
        target=target,
        op=op,
        obj_key=obj_key,
        before=before,
        after=after,
        anchor=anchor,
        feedback_text=(feedback_text or None),
        source=source,
        status=EditStatus.applied,
        applied_namespaces=[tag],
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.id


@router.patch("/{tag}/events/{event_key}", status_code=202)
async def patch_event(
    body: EventPatchRequest,
    request: Request,
    tag: Annotated[str, Path()],
    event_key: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: edit ONE event (summary/kind/ts/mentions) of the tag's
    timeline. Existence check = the SAME timeline payload the UI serves
    (events.json across the tag's DONE recordings); the workflow
    applies to the graph + artifact copies of the origin recording."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    payload = _timeline_or_404(cfg, session, norm)
    found = None
    for sess_ in payload["sessions"]:
        for ev in sess_["events"]:
            if ev.get("event_key") == event_key:
                found = (sess_, ev)
                break
        if found:
            break
    if found is None:
        raise HTTPException(
            status_code=404, detail=f"event {event_key} not found in tag {norm}"
        )
    sess_, ev = found
    after: dict = {}
    for field in ("ts", "kind", "summary", "mentions"):
        val = getattr(body, field)
        if val is not None:
            after[field] = val
    if not after:
        raise HTTPException(status_code=400, detail="nothing to update")
    edit_id = _start_edit_workflow(
        session,
        tag=norm,
        target=EditTarget.event,
        op=EditOp.update,
        obj_key=event_key,
        before={"ts": ev["ts"], "kind": ev["kind"], "summary": ev["summary"]},
        after=after,
        anchor={
            "origin_recording_id": sess_["recording_id"],
            "ts": ev["ts"],
            "kind": ev["kind"],
            "before_summary": ev["summary"],
        },
        feedback_text=body.feedback_text,
    )
    workflow_id = await _start_or_503(edit_id)
    return {"workflow_id": workflow_id, "edit_id": edit_id, "tag": norm}


@router.delete("/{tag}/events/{event_key}", status_code=202)
async def delete_event(
    request: Request,
    tag: Annotated[str, Path()],
    event_key: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    payload = _timeline_or_404(cfg, session, norm)
    found = None
    for sess_ in payload["sessions"]:
        for ev in sess_["events"]:
            if ev.get("event_key") == event_key:
                found = (sess_, ev)
                break
        if found:
            break
    if found is None:
        raise HTTPException(
            status_code=404, detail=f"event {event_key} not found in tag {norm}"
        )
    sess_, ev = found
    edit_id = _start_edit_workflow(
        session,
        tag=norm,
        target=EditTarget.event,
        op=EditOp.delete,
        obj_key=event_key,
        before={"ts": ev["ts"], "kind": ev["kind"], "summary": ev["summary"]},
        after={},
        anchor={
            "origin_recording_id": sess_["recording_id"],
            "ts": ev["ts"],
            "kind": ev["kind"],
            "before_summary": ev["summary"],
        },
        feedback_text=None,
    )
    workflow_id = await _start_or_503(edit_id)
    return {"workflow_id": workflow_id, "edit_id": edit_id, "tag": norm}


@router.post("/{tag}/relations", status_code=202)
async def create_relation(
    body: RelationCreateRequest,
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: create a REL edge between two of the tag's entities
    (user-authored — the overlay pass re-creates it after every
    regenerate)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    payload = _timeline_or_404(cfg, session, norm)
    slugs = {row["slug"] for row in payload["entities"]}
    for s in (body.from_slug, body.to_slug):
        if s not in slugs:
            raise HTTPException(status_code=404, detail=f"entity {s} not found in tag {norm}")
    edit_id = _start_edit_workflow(
        session,
        tag=norm,
        target=EditTarget.relation,
        op=EditOp.create,
        obj_key=f"{body.from_slug}|{body.to_slug}|{body.type}",
        before={},
        after={"from": body.from_slug, "to": body.to_slug, "type": body.type},
        anchor={},
        feedback_text=body.feedback_text,
    )
    workflow_id = await _start_or_503(edit_id)
    return {"workflow_id": workflow_id, "edit_id": edit_id, "tag": norm}


@router.delete("/{tag}/relations", status_code=202)
async def delete_relation(
    body: RelationDeleteRequest,
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: delete a REL edge (tombstone — the overlay re-deletes
    it after every regenerate; user decisions outrank the model)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    _timeline_or_404(cfg, session, norm)
    edit_id = _start_edit_workflow(
        session,
        tag=norm,
        target=EditTarget.relation,
        op=EditOp.delete,
        obj_key=f"{body.from_slug}|{body.to_slug}|{body.type}",
        before={"from": body.from_slug, "to": body.to_slug, "type": body.type},
        after={},
        anchor={},
        feedback_text=body.feedback_text,
    )
    workflow_id = await _start_or_503(edit_id)
    return {"workflow_id": workflow_id, "edit_id": edit_id, "tag": norm}


@router.delete("/{tag}/entities/{slug}", status_code=202)
async def delete_entity(
    request: Request,
    tag: Annotated[str, Path()],
    slug: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: delete ONE entity (node + REL/MENTIONS edges) and prune
    its slug from every events.json of the tag."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    payload = _timeline_or_404(cfg, session, norm)
    slugs = {row["slug"] for row in payload["entities"]}
    if slug not in slugs:
        raise HTTPException(
            status_code=404, detail=f"entity {slug} not found in tag {norm}"
        )
    edit_id = _start_edit_workflow(
        session,
        tag=norm,
        target=EditTarget.entity,
        op=EditOp.delete,
        obj_key=slug,
        before={},
        after={},
        anchor={},
        feedback_text=None,
    )
    workflow_id = await _start_or_503(edit_id)
    return {"workflow_id": workflow_id, "edit_id": edit_id, "tag": norm}


@router.post("/{tag}/entities/merge", status_code=202)
async def merge_entities(
    body: EntityMergeRequest,
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: fold source_slug into target_slug (redirect edges,
    union recording_ids, tombstone the source — dedup can never re-mint
    it; the overlay re-applies the mapping after every regenerate)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    payload = _timeline_or_404(cfg, session, norm)
    slugs = {row["slug"] for row in payload["entities"]}
    for s in (body.source_slug, body.target_slug):
        if s not in slugs:
            raise HTTPException(status_code=404, detail=f"entity {s} not found in tag {norm}")
    if body.source_slug == body.target_slug:
        raise HTTPException(status_code=400, detail="cannot merge entity into itself")
    edit_id = _start_edit_workflow(
        session,
        tag=norm,
        target=EditTarget.entity,
        op=EditOp.merge,
        obj_key=body.source_slug,
        before={"source": body.source_slug, "target": body.target_slug},
        after={},
        anchor={},
        feedback_text=body.feedback_text,
    )
    workflow_id = await _start_or_503(edit_id)
    return {"workflow_id": workflow_id, "edit_id": edit_id, "tag": norm}


@router.get("/{tag}/edits")
def list_edits(
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A audit log: every edit row of the tag (applied, orphaned,
    retired) newest first — the Corrections tab source."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    rows = (
        session.query(GraphEdit)
        .filter(GraphEdit.tag == norm)
        .order_by(GraphEdit.created_at.desc(), GraphEdit.id.desc())
        .limit(200)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "tag": r.tag,
                "target": r.target.value,
                "op": r.op.value,
                "obj_key": r.obj_key,
                "anchor": r.anchor,
                "before": r.before,
                "after": r.after,
                "feedback_text": r.feedback_text,
                "source": r.source,
                "status": r.status.value,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/{tag}/edits/{edit_id}/retire", status_code=202)
def retire_edit(
    request: Request,
    tag: Annotated[str, Path()],
    edit_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: retire a feedback rule (orphaned edit) — the row stops
    being rendered into the {corrections} prompt block and the overlay
    stops re-applying it. Deterministic state move, no workflow."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    row = session.get(GraphEdit, edit_id)
    if row is None or row.tag != norm:
        raise HTTPException(status_code=404, detail=f"edit {edit_id} not found for tag {norm}")
    row.status = EditStatus.retired
    session.commit()
    return {"edit_id": edit_id, "status": "retired"}


@router.get("/{tag}/graph")
def get_graph(
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: the tag's nodes + edges for the Lattice tab —
    aggregated from events.json (the SAME read-model the timeline and
    vault serve; no Neo4j session in the API). Entities dedupe by slug
    with the freshest label; relations dedupe by (from, to, type)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    payload = _timeline_or_404(cfg, session, norm)
    entities: dict[str, dict] = {}
    rels: dict[tuple[str, str, str], dict] = {}
    for sess_ in payload["sessions"]:
        rid = sess_["recording_id"]
        doc = None  # re-read raw doc for entities/relations arrays
        from app.vault import _read_events_json

        doc = _read_events_json(cfg, rid)
        if not isinstance(doc, dict):
            continue
        for ent in doc.get("entities", []) or []:
            if isinstance(ent, dict) and ent.get("slug"):
                slug = ent["slug"]
                if slug not in entities:
                    entities[slug] = {
                        "slug": slug,
                        "label": ent.get("label") or slug,
                        "type": ent.get("type") or "",
                    }
        for rel in doc.get("relations", []) or []:
            if isinstance(rel, dict):
                key = (rel.get("from", ""), rel.get("to", ""), rel.get("type", ""))
                if all(key):
                    rels.setdefault(
                        key,
                        {"from": key[0], "to": key[1], "type": key[2], "sessions": 0},
                    )
                    rels[key]["sessions"] += 1
    # Aggregated entities carry session counts from the timeline payload.
    counts = {row["slug"]: row["sessions"] for row in payload["entities"]}
    for slug, ent in entities.items():
        ent["sessions"] = counts.get(slug, 0)
    return {
        "tag": norm,
        "entities": sorted(entities.values(), key=lambda e: (-e["sessions"], e["slug"])),
        "relations": sorted(rels.values(), key=lambda r: (r["from"], r["to"], r["type"])),
    }


@router.get("/{tag}/digest/status")
def get_digest_status(
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase A: digest renewal state — ``queued`` when the newest edit
    row is younger than the digest note's mtime (renewal pending, the
    Digest tab shows the brass lamp), ``fresh`` otherwise. Single
    source of truth: the SAME comparison the GraphMaintenance activity
    uses for its skip-check."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    from datetime import UTC, datetime

    row = (
        session.query(GraphEdit)
        .filter(GraphEdit.tag == norm, GraphEdit.status != EditStatus.retired)
        .order_by(GraphEdit.created_at.desc())
        .first()
    )
    if row is None:
        return {"state": "fresh", "last_edit_at": None, "debounce_sec": cfg.graph.edit_debounce_sec}
    note = find_digest(cfg, norm)
    mtime = None
    if note is not None:
        try:
            mtime = datetime.fromtimestamp(note.stat().st_mtime, tz=UTC)
        except OSError:
            mtime = None
    last_edit = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    state = "queued" if (mtime is None or mtime < last_edit) else "fresh"
    return {
        "state": state,
        "last_edit_at": last_edit.isoformat(),
        "debounce_sec": cfg.graph.edit_debounce_sec,
    }


# ---------------------------------------------------------------------------
# Phase C: "Correct the record" — AI-translated fixes (preview → confirm →
# apply). The LLM PROPOSES; the user confirms; fix_apply mutates through
# the phase-A updaters. Rate limiting is per-process in-memory state on
# app.state: 1 in-flight preview per tag + a cooldown so a stuck client
# cannot stack LLM calls (self-hosted model, parallel=1).
# ---------------------------------------------------------------------------

_FIX_PREVIEW_COOLDOWN_SEC = 30.0


class FixPreviewRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=500)
    recording_id: str | None = None


class FixApplyRequest(BaseModel):
    proposal: dict
    feedback_text: str | None = Field(default=None, max_length=500)


def _preview_gate(app_state: Any, tag: str) -> None:
    """409 while a preview for this tag is in flight or inside the
    cooldown window. State: {tag: (workflow_id | None, until_ts)}."""
    import time

    gate = getattr(app_state, "_fix_preview_gate", None)
    if gate is None:
        gate = {}
        app_state._fix_preview_gate = gate
    now = time.monotonic()
    wf_id, until = gate.get(tag, (None, 0.0))
    if wf_id is not None:
        raise HTTPException(
            status_code=409, detail="a preview for this tag is already running"
        )
    if now < until:
        raise HTTPException(
            status_code=429,
            detail=f"preview cooldown; retry in {int(until - now) + 1}s",
        )
    gate[tag] = (f"pending-{now}", now + _FIX_PREVIEW_COOLDOWN_SEC)


def _preview_release(app_state: Any, tag: str, workflow_id: str | None) -> None:
    """Move the gate from pending to done: cooldown from NOW (the LLM
    call just finished), no in-flight slot held."""
    import time

    gate = getattr(app_state, "_fix_preview_gate", {})
    gate[tag] = (None, time.monotonic() + _FIX_PREVIEW_COOLDOWN_SEC)


@router.post("/{tag}/fix-preview", status_code=202)
async def post_fix_preview(
    body: FixPreviewRequest,
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase C: translate ONE natural-language correction into a
    proposal of graph ops (ONE LLM call, no mutation). Result is polled
    from GET /{tag}/fix-preview/{workflow_id}."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    _timeline_or_404(cfg, session, norm)
    _preview_gate(request.app.state, norm)
    try:
        workflow_id = await temporal_client.start_fix_preview(
            norm, body.instruction.strip(), body.recording_id
        )
    except Exception:  # noqa: BLE001 — same blind-catch shape as post_digest
        _LOG.exception("fix-preview start failed for tag=%s", norm)
        _preview_release(request.app.state, norm, None)
        raise HTTPException(status_code=503, detail="temporal unavailable")
    return {"workflow_id": workflow_id, "tag": norm}


@router.get("/{tag}/fix-preview/{workflow_id}")
async def get_fix_preview(
    request: Request,
    tag: Annotated[str, Path()],
    workflow_id: Annotated[str, Path()],
) -> dict:
    """Poll the preview result. 200 {state, proposal?, reason?} —
    ``running`` until the workflow finishes, then the activity's
    structured result (ok/busy/unparseable/invalid). Releases the
    per-tag gate when the run settles."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    if not workflow_id.startswith("graph-fix-preview-"):
        raise HTTPException(status_code=400, detail="not a fix-preview workflow id")
    from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

    client = await temporal_client.get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except RPCError:  # unknown workflow id (already-evicted or typo)
        _preview_release(request.app.state, norm, workflow_id)
        return {"state": "unknown"}
    if desc.status == WorkflowExecutionStatus.RUNNING:
        return {"state": "running"}
    try:
        result = await handle.result()
    except WorkflowFailureError as e:
        _preview_release(request.app.state, norm, workflow_id)
        return {"state": "failed", "detail": str(e)[:300]}
    except RPCError:  # completed but evicted (retention) — nothing to fetch
        _preview_release(request.app.state, norm, workflow_id)
        return {"state": "unknown"}
    _preview_release(request.app.state, norm, workflow_id)
    if not result.get("ok"):
        return {"state": result.get("reason", "failed"), "detail": result.get("detail")}
    return {
        "state": "ready",
        "proposal": result["proposal"],
        "context": result.get("context", {}),
    }


@router.post("/{tag}/fix-apply", status_code=202)
async def post_fix_apply(
    body: FixApplyRequest,
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Phase C: apply the confirmed proposal all-or-nothing. The
    activity re-validates every op against CURRENT state; stale ops
    surface through the poll endpoint as a rejection list (the diff the
    user saw is exactly what lands)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    _timeline_or_404(cfg, session, norm)
    ops = body.proposal.get("ops")
    if not isinstance(ops, list) or not ops:
        raise HTTPException(status_code=400, detail="proposal.ops must be a non-empty list")
    try:
        workflow_id = await temporal_client.start_fix_apply(
            norm, body.proposal, body.feedback_text
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("fix-apply start failed for tag=%s", norm)
        raise HTTPException(status_code=503, detail="temporal unavailable")
    return {"workflow_id": workflow_id, "tag": norm}


async def _start_memory_or_503(tag: str, rebuild: bool) -> str:
    try:
        return await temporal_client.start_rebuild_tag_memory(tag, rebuild)
    except Exception as e:
        if "already started" in str(e).lower():
            raise HTTPException(
                status_code=409,
                detail="a purge/rebuild is already running for this tag",
            ) from e
        _LOG.exception("memory purge/rebuild start failed for tag=%s", tag)
        raise HTTPException(
            status_code=503, detail="temporal unavailable; try again later"
        )


def _require_done_recordings(tag: str, session: Session) -> int:
    """404 when no DONE recording carries the tag (nothing to purge or
    rebuild); returns the done count for the response."""
    count = len(_tag_recordings(session, tag))
    if count == 0:
        raise HTTPException(
            status_code=404, detail=f"no done recordings carry tag {tag}"
        )
    return count


def _require_not_processing(tag: str, session: Session) -> None:
    """409 when any recording of the tag is mid-pipeline: a purge racing
    a live enrich would delete nodes the running activity is about to
    re-write (and the rebuild's child workflows would collide with the
    deterministic process-recording-<id> ids)."""
    gen = get_session()
    try:
        s = next(gen)
        from sqlalchemy import text as _text

        dialect = s.get_bind().dialect.name
        if dialect == "postgresql":
            stmt = _text(
                "SELECT count(*) FROM recordings WHERE :tag = ANY(tags) "
                "AND state = 'processing'"
            )
        else:
            stmt = _text(
                "SELECT count(*) FROM recordings WHERE EXISTS ("
                "SELECT 1 FROM json_each(recordings.tags) "
                "WHERE value = :tag) AND state = 'processing'"
            )
        if s.execute(stmt, {"tag": tag}).scalar():
            raise HTTPException(
                status_code=409,
                detail=(
                    "a recording of this tag is still processing — "
                    "wait for the pipeline to finish before purging"
                ),
            )
    finally:
        gen.close()


@router.delete("/{tag}/memory", status_code=202)
async def purge_memory(
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Admin: wipe the tag's MEMORY — Neo4j namespace, graph_edits,
    digest note, semantic index, and the per-recording events.json
    timeline artifacts of single-tag recordings (multi-tag recordings
    keep their shared artifact) — keeping audio, transcripts and
    summaries. 202 + workflow: a large namespace purges in batched
    DETACH DELETEs, which belongs behind a pollable workflow, not a
    synchronous handler."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    # Processing beats 404: a tag whose only recording is mid-pipeline
    # must answer "wait for the pipeline", not "nothing to purge".
    _require_not_processing(norm, session)
    done = _require_done_recordings(norm, session)
    workflow_id = await _start_memory_or_503(norm, rebuild=False)
    return {"workflow_id": workflow_id, "tag": norm, "done_recordings": done}


@router.post("/{tag}/rebuild", status_code=202)
async def rebuild_memory(
    request: Request,
    tag: Annotated[str, Path()],
    session: Session = Depends(get_session),
) -> dict:
    """Admin: purge the tag's memory, then re-run enrich per done
    recording (oldest first, sequential). Destructive like purge — the
    confirm belongs in the UI. 202 + workflow id (deterministic per
    tag: a live rebuild 409s)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    # Same guard order as purge: processing → done-records → start.
    _require_not_processing(norm, session)
    done = _require_done_recordings(norm, session)
    workflow_id = await _start_memory_or_503(norm, rebuild=True)
    return {"workflow_id": workflow_id, "tag": norm, "done_recordings": done}


@router.get("/{tag}/memory/{workflow_id}")
async def get_memory_status(
    request: Request,
    tag: Annotated[str, Path()],
    workflow_id: Annotated[str, Path()],
) -> dict:
    """Poll a purge/rebuild: running (with the workflow's progress
    query: total/done/current) | done | failed | unknown. The progress
    query is only meaningful for rebuilds; purge-only runs report
    done=0/total=0 until they finish."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    if not workflow_id.startswith("rebuild-tag-memory-"):
        raise HTTPException(status_code=400, detail="not a memory workflow id")
    from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

    client = await temporal_client.get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except RPCError:  # unknown workflow id (evicted or typo)
        return {"state": "unknown"}
    if desc.status == WorkflowExecutionStatus.RUNNING:
        try:
            progress = await handle.query("progress") or {}
        except RPCError:
            progress = {}
        return {"state": "running", "progress": progress}
    try:
        result = await handle.result()
    except WorkflowFailureError as e:
        return {"state": "failed", "detail": str(e)[:300]}
    except RPCError:  # completed but evicted — nothing to fetch
        return {"state": "unknown"}
    return {"state": "done", "result": result}


@router.get("/{tag}/fix-apply/{workflow_id}")
async def get_fix_apply(
    request: Request,
    tag: Annotated[str, Path()],
    workflow_id: Annotated[str, Path()],
) -> dict:
    """Poll the apply result: running | ok | stale (with per-op
    rejections)."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    _require_graph(cfg, norm)
    if not workflow_id.startswith("graph-fix-apply-"):
        raise HTTPException(status_code=400, detail="not a fix-apply workflow id")
    from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

    client = await temporal_client.get_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except RPCError:  # unknown workflow id (already-evicted or typo)
        return {"state": "unknown"}
    if desc.status == WorkflowExecutionStatus.RUNNING:
        return {"state": "running"}
    try:
        result = await handle.result()
    except WorkflowFailureError as e:
        return {"state": "failed", "detail": str(e)[:300]}
    except RPCError:  # completed but evicted — nothing to fetch
        return {"state": "unknown"}
    if result.get("ok"):
        return {
            "state": "ok",
            "applied": result.get("applied", 0),
            "edit_ids": result.get("edit_ids", []),
        }
    return {
        "state": result.get("reason", "failed"),
        "rejections": result.get("rejections", []),
    }


async def _start_or_503(edit_id: int) -> str:
    try:
        return await temporal_client.start_apply_graph_edit(edit_id)
    except Exception:  # noqa: BLE001 — same blind-catch shape as post_digest
        _LOG.exception("start_apply_graph_edit failed for edit=%s", edit_id)
        raise HTTPException(
            status_code=503, detail="temporal unavailable; try again later"
        )


class TagCount(BaseModel):
    tag: str
    count: int


class TagListResponse(BaseModel):
    items: list[TagCount]


@router.get("")
def list_tags(request: Request, session: Session = Depends(get_session)) -> dict:
    """Distinct free tags with recording counts (Phase 0): the source for
    the client's tag suggestions. Counts include recordings in ANY state
    (a tag on an uploading capture is real user intent) — ordering is
    count DESC then tag ASC so the UI shows popular tags first and the
    tail is deterministic. Dialect split mirrors worker digest
    ``_select_recordings``: Postgres unnests the TEXT[], SQLite explodes
    the JSON array (tests).
    """

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        rows = session.execute(
            text(
                "SELECT tag, count(*) AS count FROM recordings, "
                "unnest(recordings.tags) AS tag "
                "WHERE tag <> '' GROUP BY tag "
                "ORDER BY count DESC, tag ASC"
            )
        ).all()
    else:
        rows = session.execute(
            text(
                "SELECT value AS tag, count(*) AS count FROM recordings, "
                "json_each(recordings.tags) "
                "WHERE value <> '' GROUP BY value "
                "ORDER BY count DESC, tag ASC"
            )
        ).all()
    return {"items": [{"tag": row[0], "count": row[1]} for row in rows]}