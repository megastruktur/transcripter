"""Graph edit updaters (Phase A): the deterministic write layer.

Every accepted edit goes through ONE of the ``apply_*`` functions here —
the manual API endpoints and the Phase C fix-apply share them, so an
agent can never write anything the deterministic path couldn't.

Each apply does three things, always:
1. mutates the Neo4j namespace copy (tag-scoped);
2. rewrites ``meta/events.json`` (storage copy + vault mirror) so the
   timeline/vault read-model moves in the same stroke;
3. nothing else — the API inserts the ``graph_edits`` row and signals
   the GraphMaintenance workflow; the overlay pass (``reapply_overlay``)
   re-applies stored edits after an enrich regenerate.

Best-effort shape: errors propagate to the activity (which fails the
edit workflow, visible in Temporal UI) — but nothing here can ever fail
an ENRICH run; the overlay caller wraps this module in try/except.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from neo4j import GraphDatabase

log = logging.getLogger("transcripter.graph_edit")

# Fuzzy re-anchor thresholds (Phase A critic resolution #1): an edit
# whose original event vanished after a regenerate re-anchors onto the
# new node when kind matches exactly and summary similarity is at least
# this ratio (difflib.SequenceMatcher — deterministic, no LLM). Below
# the threshold the edit turns orphaned and surfaces in the audit UI;
# the {corrections} feedback block (Phase B) still keeps future
# extraction from resurrecting the error.
_ANCHOR_SIM_THRESHOLD = 0.60


def compute_event_key(rec_id: str, ts: str, kind: str, summary: str, occurrence: int = 0) -> str:
    """EXACT TWIN of enrich.compute_event_key / app.vault.compute_event_key
    — one definition per venv, change all three IN SYNC."""
    raw = f"{rec_id}\x1f{ts}\x1f{kind}\x1f{summary}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return digest if occurrence == 0 else f"{digest}-{occurrence}"


def _events_with_keys(recording_id: str, events: list[dict]) -> list[dict]:
    """Fill ``event_key`` for events that lack one (legacy files)."""
    seen: dict[str, int] = {}
    out: list[dict] = []
    for ev in events:
        key = ev.get("event_key")
        if not isinstance(key, str) or not key:
            base = compute_event_key(
                recording_id, ev.get("ts", ""), ev.get("kind", ""), ev.get("summary", "")
            )
            n = seen.get(base, 0)
            seen[base] = n + 1
            key = base if n == 0 else f"{base}-{n}"
        out.append({**ev, "event_key": key})
    return out


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class VaultPaths:
    """Where a recording's events.json copies live (storage + vault
    mirror). ``recordings_root`` is the worker's storage root; the vault
    mirror folders come from the export scan (already id8-matched)."""

    recordings_root: Path
    vault_root: Path
    vault_folders: list[Path]


def _meta_candidates(recording_id: str, paths: VaultPaths) -> list[Path]:
    """Both possible events.json locations, storage first (the API read
    order). A vault-mode recording has no storage copy — the mirror is
    the only one."""
    out = [paths.recordings_root / recording_id / "meta" / "events.json"]
    for folder in paths.vault_folders:
        out.append(folder / ".transcripter" / "meta" / "events.json")
    return out


def rewrite_events_json(recording_id: str, paths: VaultPaths, mutate) -> bool:
    """Read-modify-write every events.json copy of one recording.

    ``mutate(doc) -> bool`` receives the parsed dict (guaranteed a dict)
    and returns whether anything changed; unchanged copies are skipped
    untouched. Writes are atomic (unique-tmp + os.replace — the repo
    idiom). Returns True when at least one copy changed.
    """
    changed = False
    for path in _meta_candidates(recording_id, paths):
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("edit: unreadable %s: %s", path, exc)
            continue
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("edit: malformed %s: %s", path, exc)
            continue
        if not isinstance(doc, dict):
            log.warning("edit: %s is not an object — skipped", path)
            continue
        events = doc.get("events")
        if isinstance(events, list):
            doc["events"] = _events_with_keys(recording_id, events)
        try:
            if not mutate(doc):
                continue
        except Exception:  # a mutator bug must not corrupt the file
            log.exception("edit: mutator failed on %s — copy skipped", path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
            changed = True
        finally:
            tmp.unlink(missing_ok=True)
    return changed


# --- graph side helpers --------------------------------------------------------


def _driver(cfg: Any):
    return GraphDatabase.driver(
        cfg.graph.uri,
        auth=(cfg.graph.user, os.environ.get(cfg.graph.password_env, "")),
    )


def _find_event_node(session: Any, tag: str, event_key: str) -> dict | None:
    """The (tag, event_key) event node as a plain dict, None when absent.
    Legacy nodes without the property never match — the API serves
    computed keys and the events.json rewrite SETs the property on
    first edit, so within one generation addressing is exact."""
    row = session.run(
        "MATCH (e {tag: $tag, event_key: $key}) "
        "RETURN elementId(e) AS id, e.ts AS ts, e.kind AS kind, "
        "e.summary AS summary, e.origin_recording_id AS origin, "
        "e.recording_title AS title LIMIT 1",
        tag=tag,
        key=event_key,
    ).single()
    return dict(row) if row is not None else None


def _recording_events(session: Any, origin: str) -> list[dict]:
    """All event nodes of one recording across EVERY namespace (the
    overlay re-anchor scope — copies of the same extraction)."""
    rows = session.run(
        "MATCH (e {origin_recording_id: $origin}) "
        "WHERE e.event_key IS NOT NULL "
        "RETURN elementId(e) AS id, e.tag AS tag, e.event_key AS event_key, "
        "e.ts AS ts, e.kind AS kind, e.summary AS summary",
        origin=origin,
    )
    return [dict(r) for r in rows]


# --- apply operations (one per EditTarget/Op) ----------------------------------


def apply_event_update(
    cfg: Any,
    paths: VaultPaths,
    tag: str,
    event_key: str,
    after: dict[str, Any],
    anchor: dict[str, Any],
) -> dict[str, Any]:
    """PATCH one event (summary/kind/ts/mentions) in the tag namespace +
    every events.json copy of the origin recording. Returns the applied
    payload (missing fields filled from the current node) + re-anchor
    context for the edit row."""
    with _driver(cfg) as driver, driver.session(database=cfg.graph.database) as session:
        node = _find_event_node(session, tag, event_key)
        if node is None:
            return {"ok": False, "reason": "event not found in namespace"}
        sets: list[str] = []
        params: dict[str, Any] = {"id": node["id"]}
        for field in ("ts", "kind", "summary"):
            if field in after:
                sets.append(f"e.{field} = ${field}")
                params[field] = after[field]
        if "mentions" in after and isinstance(after["mentions"], list):
            sets.append("e.mentions = $mentions")
            params["mentions"] = after["mentions"]
        if sets:
            set_query: str = "MATCH (e) WHERE elementId(e) = $id SET "
            set_query += ", ".join(sets)
            session.run(query=cast("Any", set_query), **params)
        applied = {
            "ts": after.get("ts", node["ts"]),
            "kind": after.get("kind", node["kind"]),
            "summary": after.get("summary", node["summary"]),
            "mentions": after.get("mentions"),
        }
        origin = node["origin"]

    # events.json: same key-based addressing (the artifact carries keys).
    def _mutate(doc: dict) -> bool:
        changed = False
        for ev in doc.get("events", []):
            if isinstance(ev, dict) and ev.get("event_key") == event_key:
                for field in ("ts", "kind", "summary", "mentions"):
                    if field in after:
                        ev[field] = after[field]
                        changed = True
        return changed

    rewrite_events_json(origin, paths, _mutate)
    return {
        "ok": True,
        "origin": origin,
        "applied": applied,
        "anchor": {
            "origin_recording_id": origin,
            "kind": applied.get("kind", node_kind(node)),
            "ts": applied.get("ts", ""),
            "before_summary": anchor.get("before_summary", ""),
        },
    }


def node_kind(node: dict) -> str:
    return node.get("kind") or ""


def apply_event_delete(cfg: Any, paths: VaultPaths, tag: str, event_key: str) -> dict[str, Any]:
    with _driver(cfg) as driver, driver.session(database=cfg.graph.database) as session:
        node = _find_event_node(session, tag, event_key)
        if node is None:
            return {"ok": False, "reason": "event not found in namespace"}
        session.run("MATCH (e) WHERE elementId(e) = $id DETACH DELETE e", id=node["id"])
        origin = node["origin"]

    def _mutate(doc: dict) -> bool:
        before = doc.get("events", [])
        after = [
            ev for ev in before if not (isinstance(ev, dict) and ev.get("event_key") == event_key)
        ]
        if len(after) == len(before):
            return False
        doc["events"] = after
        return True

    rewrite_events_json(origin, paths, _mutate)
    return {"ok": True, "origin": origin}


_REL_KEY_RE = re.compile(r"^[\w.-]+$")


def relation_key(from_slug: str, to_slug: str, rel_type: str) -> str:
    """Stable identity for a REL edge in the edit store."""
    return f"{from_slug}|{to_slug}|{rel_type}"


def apply_relation_create(
    cfg: Any, paths: VaultPaths, tag: str, from_slug: str, to_slug: str, rel_type: str
) -> dict[str, Any]:
    with _driver(cfg) as driver, driver.session(database=cfg.graph.database) as session:
        row = session.run(
            "MATCH (a {tag: $tag, slug: $fs}), (b {tag: $tag, slug: $ts}) "
            "RETURN elementId(a) AS a, elementId(b) AS b",
            tag=tag,
            fs=from_slug,
            ts=to_slug,
        ).single()
        if row is None:
            return {"ok": False, "reason": "endpoint entity not found in namespace"}
        session.run(
            "MATCH (a), (b) WHERE elementId(a) = $a AND elementId(b) = $b "
            "MERGE (a)-[r:REL {type: $type}]->(b)",
            a=row["a"],
            b=row["b"],
            type=rel_type,
        )

    # events.json: relations carry no origin; user-created edges land in
    # EVERY events.json copy of the tag's recordings that mentions BOTH
    # slugs — the graph tab aggregates from the same files.
    def _mutate(doc: dict) -> bool:
        rels = doc.setdefault("relations", [])
        have = {(r.get("from"), r.get("to"), r.get("type")) for r in rels if isinstance(r, dict)}
        if (from_slug, to_slug, rel_type) in have:
            return False
        slugs = {e.get("slug") for e in doc.get("entities", []) if isinstance(e, dict)}
        if from_slug in slugs and to_slug in slugs:
            rels.append({"from": from_slug, "to": to_slug, "type": rel_type})
            return True
        return False

    changed_any = False
    for rec_id in tag_recording_ids(cfg, tag):
        changed_any |= rewrite_events_json(rec_id, paths, _mutate)
    return {"ok": True, "events_touched": changed_any}


def apply_relation_delete(
    cfg: Any, paths: VaultPaths, tag: str, from_slug: str, to_slug: str, rel_type: str
) -> dict[str, Any]:
    with _driver(cfg) as driver, driver.session(database=cfg.graph.database) as session:
        result = session.run(
            "MATCH ({tag: $tag, slug: $fs})-[r:REL {type: $type}]"
            "->({tag: $tag, slug: $ts}) DELETE r RETURN count(r) AS n",
            tag=tag,
            fs=from_slug,
            ts=to_slug,
            type=rel_type,
        ).single()

    def _mutate(doc: dict) -> bool:
        rels = doc.get("relations", [])
        after = [
            r
            for r in rels
            if not (
                isinstance(r, dict)
                and r.get("from") == from_slug
                and r.get("to") == to_slug
                and r.get("type") == rel_type
            )
        ]
        if len(after) == len(rels):
            return False
        doc["relations"] = after
        return True

    for rec_id in tag_recording_ids(cfg, tag):
        rewrite_events_json(rec_id, paths, _mutate)
    return {"ok": True, "deleted": result["n"] if result else 0}


def tag_recording_ids(cfg: Any, tag: str) -> list[str]:
    """DONE recordings carrying the tag (the events.json rewrite scope).
    Mirrors digest._select_recordings / vault._tag_recordings semantics
    without a session dependency: reads the catalog directly."""
    from .db import Recording, RecordingState, session

    with session() as s:
        rows = (
            s.query(Recording.id)
            .filter(
                Recording.state == RecordingState.done,
                Recording.tags.contains([tag]),
            )
            .all()
        )
        return [r[0] for r in rows]


def apply_entity_delete(cfg: Any, paths: VaultPaths, tag: str, slug: str) -> dict[str, Any]:
    with _driver(cfg) as driver, driver.session(database=cfg.graph.database) as session:
        row = session.run(
            "MATCH (e {tag: $tag, slug: $slug}) RETURN elementId(e) AS id LIMIT 1",
            tag=tag,
            slug=slug,
        ).single()
        if row is None:
            return {"ok": False, "reason": "entity not found in namespace"}
        session.run("MATCH (e) WHERE elementId(e) = $id DETACH DELETE e", id=row["id"])

    for rec_id in tag_recording_ids(cfg, tag):
        rewrite_events_json(rec_id, paths, lambda doc: _prune_slug_from_doc(doc, slug))
    return {"ok": True}


def _prune_slug_from_doc(doc: dict, slug: str) -> bool:
    """Drop ``slug`` from entities[], event mentions and relations of
    one events.json doc. Shared by apply_entity_delete (every doc of
    the tag) and the overlay's entity-tombstone pass (this recording's
    doc after a regenerate re-minted the slug)."""
    changed = False
    ents = doc.get("entities", [])
    after = [e for e in ents if not (isinstance(e, dict) and e.get("slug") == slug)]
    if len(after) != len(ents):
        doc["entities"] = after
        changed = True
    for ev in doc.get("events", []):
        if not isinstance(ev, dict):
            continue
        mentions = ev.get("mentions")
        if isinstance(mentions, list) and slug in mentions:
            ev["mentions"] = [m for m in mentions if m != slug]
            changed = True
    rels = doc.get("relations", [])
    rel_after = [
        r for r in rels if not (isinstance(r, dict) and slug in (r.get("from"), r.get("to")))
    ]
    if len(rel_after) != len(rels):
        doc["relations"] = rel_after
        changed = True
    return changed


def apply_entity_merge(
    cfg: Any, paths: VaultPaths, tag: str, source_slug: str, target_slug: str
) -> dict[str, Any]:
    """Fold ``source`` into ``target``: redirect REL edges both ways,
    redirect MENTIONS, union recording_ids, tombstone the source node.

    Tombstone semantics: the source node is deleted; a
    ``merged_into`` property is written to the TARGET (and the edit row
    keeps source→target forever) so the overlay pass can re-apply the
    mapping after a regenerate re-mints the source slug.
    """
    if source_slug == target_slug:
        return {"ok": False, "reason": "cannot merge entity into itself"}
    with _driver(cfg) as driver, driver.session(database=cfg.graph.database) as session:
        ids = session.run(
            "MATCH (e {tag: $tag}) WHERE e.slug IN [$s, $t] "
            "RETURN e.slug AS slug, elementId(e) AS id",
            tag=tag,
            s=source_slug,
            t=target_slug,
        )
        by_slug = {r["slug"]: r["id"] for r in ids}
        if source_slug not in by_slug:
            return {"ok": False, "reason": f"source entity {source_slug} not found"}
        if target_slug not in by_slug:
            return {"ok": False, "reason": f"target entity {target_slug} not found"}
        # 1. REL edges: point both directions at the target.
        session.run(
            "MATCH (src {tag: $tag, slug: $s})-[r:REL]->(other {tag: $tag}) "
            "WHERE other.slug <> $t "
            "MERGE (tgt {tag: $tag, slug: $t})-[nr:REL {type: r.type}]->(other) "
            "DELETE r",
            tag=tag,
            s=source_slug,
            t=target_slug,
        )
        session.run(
            "MATCH (other {tag: $tag})-[r:REL]->(src {tag: $tag, slug: $s}) "
            "WHERE other.slug <> $t "
            "MERGE (other)-[nr:REL {type: r.type}]->(tgt {tag: $tag, slug: $t}) "
            "DELETE r",
            tag=tag,
            s=source_slug,
            t=target_slug,
        )
        # 2. MENTIONS: events that mentioned the source now mention target.
        session.run(
            "MATCH (ev)-[m:MENTIONS]->(src {tag: $tag, slug: $s}) "
            "MERGE (ev)-[:MENTIONS]->(tgt {tag: $tag, slug: $t}) DELETE m",
            tag=tag,
            s=source_slug,
            t=target_slug,
        )
        # 3. recording_ids union onto the target, then drop the source.
        session.run(
            "MATCH (src {tag: $tag, slug: $s}), (tgt {tag: $tag, slug: $t}) "
            "SET tgt.recording_ids = [x IN coalesce(tgt.recording_ids, []) "
            "+ coalesce(src.recording_ids, []) WHERE x IS NOT NULL | x] "
            "WITH src, tgt, collect(DISTINCT x) AS uni "
            "SET tgt.recording_ids = uni "
            "DETACH DELETE src",
            tag=tag,
            s=source_slug,
            t=target_slug,
        )
        session.run(
            "MATCH (tgt {tag: $tag, slug: $t}) "
            "SET tgt.merged_slugs = [x IN coalesce(tgt.merged_slugs, []) "
            "WHERE x <> $s | x] + [$s]",
            tag=tag,
            t=target_slug,
            s=source_slug,
        )

    # events.json: entities[] collapse + mentions/relations rewritten.
    def _mutate(doc: dict) -> bool:
        changed = False
        ents = doc.get("entities", [])
        src_present = any(isinstance(e, dict) and e.get("slug") == source_slug for e in ents)
        tgt_present = any(isinstance(e, dict) and e.get("slug") == target_slug for e in ents)
        if src_present:
            doc["entities"] = [
                e for e in ents if not (isinstance(e, dict) and e.get("slug") == source_slug)
            ]
            changed = True
        for ev in doc.get("events", []):
            if not isinstance(ev, dict):
                continue
            mentions = ev.get("mentions")
            if isinstance(mentions, list) and source_slug in mentions:
                ev["mentions"] = [target_slug if m == source_slug else m for m in mentions]
                # dedupe in place
                seen_m: set[str] = set()
                ev["mentions"] = [m for m in ev["mentions"] if not (m in seen_m or seen_m.add(m))]
                changed = True
        for r in doc.get("relations", []):
            if not isinstance(r, dict):
                continue
            moved = False
            if r.get("from") == source_slug:
                r["from"] = target_slug
                moved = True
            if r.get("to") == source_slug:
                r["to"] = target_slug
                moved = True
            if moved:
                changed = True
        if changed and tgt_present:
            # drop self-loop relations the redirect may have created
            doc["relations"] = [
                r
                for r in doc.get("relations", [])
                if not (isinstance(r, dict) and r.get("from") == r.get("to"))
            ]
        return changed

    for rec_id in tag_recording_ids(cfg, tag):
        rewrite_events_json(rec_id, paths, _mutate)
    return {"ok": True}


def reapply_overlay(cfg: Any, tag: str, recording_id: str) -> dict[str, int]:
    """Phase A overlay pass: after an enrich regenerate rewrote the
    recording's nodes (purge + re-extraction), re-apply every ACTIVE
    edit row of the tag that concerns this recording's objects.

    Order matters: merges first (they define slug canonicalization),
    then entity tombstones, then relation tombstones/creates, then
    event updates/deletes (fuzzy re-anchored). Idempotent by
    construction (MERGE / key match / similarity gate). Best-effort:
    raises propagate to the caller (the enrich hook) which catches and
    logs — this pass NEVER fails enrich.
    """
    from .db import EditOp, EditStatus, EditTarget, GraphEdit, session

    counts = {"reanchored": 0, "orphaned": 0, "relations": 0, "merges": 0, "entities": 0}
    with session() as s:
        rows = (
            s.query(GraphEdit)
            .filter(
                GraphEdit.tag == tag,
                GraphEdit.status == EditStatus.applied,
            )
            .order_by(GraphEdit.created_at.asc(), GraphEdit.id.asc())
            .all()
        )
        edits = [
            {
                "id": r.id,
                "target": r.target,
                "op": r.op,
                "obj_key": r.obj_key,
                "before": r.before or {},
                "after": r.after or {},
                "anchor": r.anchor or {},
            }
            for r in rows
        ]
    if not edits:
        return counts
    paths = vault_paths_for(cfg, recording_id)
    # Merge-mapping (critic #4): slugs folded by a merge resolve to the
    # canonical target BEFORE any relation/event reference below.
    merge_map: dict[str, str] = {}
    for ed in edits:
        if ed["target"] == EditTarget.entity and ed["op"] == EditOp.merge:
            src, tgt = ed["before"].get("source"), ed["before"].get("target")
            if src and tgt:
                merge_map.setdefault(src, tgt)
    # 1. Merges: canonicalize any re-minted source slug.
    for ed in edits:
        if ed["target"] == EditTarget.entity and ed["op"] == EditOp.merge:
            src, tgt = ed["before"].get("source"), ed["before"].get("target")
            if src and tgt:
                with _driver(cfg) as dr, dr.session(database=cfg.graph.database) as sess:
                    sess.run(
                        "MATCH (s {tag: $tag, slug: $src}) DETACH DELETE s",
                        tag=tag,
                        src=src,
                    )
                counts["merges"] += 1
    # 1b. Entity tombstones: a regenerate re-mints user-deleted slugs
    # straight from the transcript (the model never saw the deletion) —
    # without this pass the deleted entity resurfaces in Neo4j, in
    # events.json, and then in the {known_entities} prompt block of
    # every later regenerate. Merges ran first: a deleted+merged slug
    # is already gone, the DETACH DELETE below is a no-op for it.
    for ed in edits:
        if ed["target"] != EditTarget.entity or ed["op"] != EditOp.delete:
            continue
        slug = ed["obj_key"]
        if not slug:
            continue
        with _driver(cfg) as dr, dr.session(database=cfg.graph.database) as sess:
            sess.run(
                "MATCH (e {tag: $tag, slug: $slug}) DETACH DELETE e",
                tag=tag,
                slug=slug,
            )
        rewrite_events_json(
            recording_id, paths, lambda doc, s=slug: _prune_slug_from_doc(doc, s)
        )
        counts["entities"] += 1
    # 2. Relations: re-create user edges, re-delete tombstoned ones.
    for ed in edits:
        if ed["target"] != EditTarget.relation:
            continue
        fs, ts_, rt = (
            merge_map.get(ed["after"].get("from"), ed["after"].get("from")),
            merge_map.get(ed["after"].get("to"), ed["after"].get("to")),
            ed["after"].get("type"),
        )
        if ed["op"] == EditOp.create and fs and ts_ and rt:
            with _driver(cfg) as dr, dr.session(database=cfg.graph.database) as sess:
                sess.run(
                    "MATCH (a {tag: $tag, slug: $fs}), (b {tag: $tag, slug: $ts}) "
                    "MERGE (a)-[:REL {type: $type}]->(b)",
                    tag=tag,
                    fs=fs,
                    ts=ts_,
                    type=rt,
                )
            counts["relations"] += 1
        elif ed["op"] == EditOp.delete:
            del_fs = merge_map.get(ed["before"].get("from"), ed["before"].get("from"))
            del_ts = merge_map.get(ed["before"].get("to"), ed["before"].get("to"))
            del_rt = ed["before"].get("type")
            if del_fs and del_ts and del_rt:
                with _driver(cfg) as dr, dr.session(database=cfg.graph.database) as sess:
                    sess.run(
                        "MATCH ({tag: $tag, slug: $fs})"
                        "-[r:REL {type: $type}]->({tag: $tag, slug: $ts}) DELETE r",
                        tag=tag,
                        fs=del_fs,
                        ts=del_ts,
                        type=del_rt,
                    )
                counts["relations"] += 1
    # 3. Event edits: fuzzy re-anchor against the fresh generation.
    with _driver(cfg) as dr, dr.session(database=cfg.graph.database) as sess:
        fresh = [e for e in _recording_events(sess, recording_id) if e["tag"] == tag]
    for ed in edits:
        if ed["target"] != EditTarget.event:
            continue
        anchor = ed["anchor"] or {}
        matched = None
        for cand in fresh:
            if cand["kind"] != anchor.get("kind"):
                continue
            if (
                similarity(cand["summary"] or "", anchor.get("before_summary", ""))
                >= _ANCHOR_SIM_THRESHOLD
            ):
                matched = cand
                break
        if matched is None:
            _set_edit_status(ed["id"], EditStatus.orphaned)
            counts["orphaned"] += 1
            continue
        if ed["op"] == EditOp.delete:
            with _driver(cfg) as dr, dr.session(database=cfg.graph.database) as sess:
                sess.run(
                    "MATCH (e) WHERE elementId(e) = $id DETACH DELETE e",
                    id=matched["id"],
                )
            rewrite_events_json(
                recording_id,
                paths,
                lambda doc, k=matched["event_key"]: _drop_event(doc, k),
            )
        else:  # update
            after = ed["after"]
            sets = []
            params: dict[str, Any] = {"id": matched["id"]}
            for field in ("ts", "kind", "summary", "mentions"):
                if field in after:
                    sets.append(f"e.{field} = ${field}")
                    params[field] = after[field]
            if sets:
                with _driver(cfg) as dr, dr.session(database=cfg.graph.database) as sess:
                    sess.run(
                        "MATCH (e) WHERE elementId(e) = $id SET " + ", ".join(sets),
                        **params,
                    )
            rewrite_events_json(
                recording_id,
                paths,
                lambda doc, k=matched["event_key"], a=after: _patch_event(doc, k, a),
            )
        # Re-key the edit row to the new generation.
        _rekey_edit(ed["id"], matched["event_key"], matched["summary"])
        counts["reanchored"] += 1
    return counts


def _drop_event(doc: dict, event_key: str) -> bool:
    before = doc.get("events", [])
    after = [ev for ev in before if not (isinstance(ev, dict) and ev.get("event_key") == event_key)]
    if len(after) == len(before):
        return False
    doc["events"] = after
    return True


def _patch_event(doc: dict, event_key: str, after: dict) -> bool:
    changed = False
    for ev in doc.get("events", []):
        if isinstance(ev, dict) and ev.get("event_key") == event_key:
            for field in ("ts", "kind", "summary", "mentions"):
                if field in after:
                    ev[field] = after[field]
                    changed = True
    return changed


def _set_edit_status(edit_id: int, status) -> None:
    from .db import GraphEdit, session

    with session() as s:
        row = s.get(GraphEdit, edit_id)
        if row is not None:
            row.status = status
            s.commit()


def _rekey_edit(edit_id: int, event_key: str, before_summary: str) -> None:
    """Point the edit row at the new generation's key + anchor text."""
    from .db import GraphEdit, session

    with session() as s:
        row = s.get(GraphEdit, edit_id)
        if row is not None:
            row.obj_key = event_key
            anchor = dict(row.anchor or {})
            anchor["before_summary"] = before_summary
            row.anchor = anchor
            s.commit()


def vault_paths_for(cfg: Any, recording_id: str) -> VaultPaths:
    """VaultPaths for one recording: vault folders via the export scan
    (id8-matched), storage root from the config."""
    from .export import Rec, scan_recording_folders

    rec = Rec(recording_id, "", _utcnow_placeholder(), None)
    folders = scan_recording_folders(cfg.vault.path, rec)
    return VaultPaths(
        recordings_root=cfg.recordings_root,
        vault_root=cfg.vault.path,
        vault_folders=folders,
    )


def _utcnow_placeholder():
    from datetime import UTC, datetime

    return datetime.now(UTC)
