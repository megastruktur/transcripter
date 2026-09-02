"""Phase C: the "Correct the record" fix agent — a translator, never a
writer.

One LLM call (``cfg.summarize``, no hardcoded models) turns a natural-
language correction instruction into a JSON proposal of graph ops —
the SAME op shapes the manual edit API accepts. No graph mutation
happens here; the proposal is applied (or rejected) by ``fix_apply``,
which re-validates every op against the CURRENT state and runs
all-or-nothing through the phase-A updaters.

Serialization mandate (self-hosted LLM, parallel=1): preview = ONE
call inside ONE activity; apply = deterministic updaters only, no LLM.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .enrich import _json_payload
from .graph_edit import tag_recording_ids
from .llm_payload import system_first_messages

log = logging.getLogger("transcripter.graph_fix")

# HTTP budget mirrors the plan's ~120 s preview envelope: the activity's
# start_to_close is 180 s; the call must time out first (httpx raises a
# plain Exception → structured "busy" result, never a Temporal cancel).
_FIX_HTTP_TIMEOUT_SEC = 120.0

# Context caps (critic finding #3): the model sees a bounded slice —
# events of the target recording (or the newest recording of the tag),
# the tag's entity/relation aggregate, and a transcript window around
# any ts the proposal is about.
_MAX_EVENTS_IN_CONTEXT = 120
_MAX_ENTITIES_IN_CONTEXT = 150
_MAX_RELATIONS_IN_CONTEXT = 150
_MAX_TRANSCRIPT_CHARS = 6000

# How many ops a single proposal may carry (all-or-nothing apply).
_MAX_PROPOSAL_OPS = 12

def _read_events_doc(cfg: Any, rec_id: str) -> dict:
    """events.json of one recording: storage copy first, then vault
    mirrors (VaultPaths.vault_folders carry the recording folder; the
    artifact sits at ``.transcripter/meta/events.json`` inside it).
    Missing or unreadable → empty doc (best-effort context, never a
    hard fail)."""
    from .activities import meta_dir
    from .graph_edit import vault_paths_for

    candidates = [meta_dir(rec_id) / "events.json"]
    for folder in vault_paths_for(cfg, rec_id).vault_folders:
        candidates.append(folder / ".transcripter" / "meta" / "events.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("fix-preview: unreadable events.json %s: %s", path, e)
    return {}

def _parse_ts(ts: str) -> int | None:
    """Lenient HH:MM:SS / MM:SS / H:MM:SS → seconds (critic #3: ts
    format mismatch must not kill the context build)."""
    m = re.fullmatch(
        r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d+))?", (ts or "").strip()
    )
    if not m:
        return None
    h = int(m.group(1) or 0)
    mi, sec = int(m.group(2)), int(m.group(3))
    if mi > 59 or sec > 59:
        return None
    return h * 3600 + mi * 60 + sec


def _transcript_window(path: Path, around_ts: str | None) -> str:
    """Capped transcript excerpt. ``around_ts`` given → a ±90 s window
    around the timestamp; otherwise the head of the transcript. The
    excerpt is capped to _MAX_TRANSCRIPT_CHARS regardless."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    if around_ts:
        target = _parse_ts(around_ts)
        if target is not None:
            # Transcript lines carry [HH:MM:SS] / [MM:SS] stamps in most
            # profiles; fall back to the head when none parse.
            keep: list[str] = []
            for line in lines:
                stamp = re.match(r"\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)", line)
                if stamp:
                    s = _parse_ts(stamp.group(1))
                    if s is not None and abs(s - target) <= 90:
                        keep.append(line)
            if keep:
                lines = keep
    excerpt = "\n".join(lines)
    return excerpt[:_MAX_TRANSCRIPT_CHARS]


def _fix_prompt(
    instruction: str,
    recording_title: str,
    events: list[dict],
    entities: list[dict],
    relations: list[dict],
    transcript_excerpt: str,
) -> str:
    events_slice = [
        {k: e.get(k) for k in ("event_key", "ts", "kind", "summary")}
        for e in events[:_MAX_EVENTS_IN_CONTEXT]
    ]
    ent_slice = [
        {k: e.get(k) for k in ("slug", "label", "type")}
        for e in entities[:_MAX_ENTITIES_IN_CONTEXT]
    ]
    rel_slice = [
        {k: e.get(k) for k in ("from", "to", "type")}
        for r in relations[:_MAX_RELATIONS_IN_CONTEXT]
        for e in [r]
    ]
    return (
        "You propose corrections to a knowledge graph extracted from "
        "call recordings. The operator says the extraction got "
        "something wrong. Translate their instruction into a list of "
        "graph operations. You are a translator, not an author: only "
        "ops that follow from the instruction and the current state.\n"
        "\n"
        f"OPERATOR INSTRUCTION:\n{instruction}\n"
        "\n"
        f"RECORDING: {recording_title}\n"
        "\n"
        "CURRENT EVENTS (event_key is the address for edit/delete):\n"
        f"{json.dumps(events_slice, ensure_ascii=False, indent=1)}\n"
        "\n"
        "CURRENT ENTITIES (slug is the address):\n"
        f"{json.dumps(ent_slice, ensure_ascii=False, indent=1)}\n"
        "\n"
        "CURRENT RELATIONS (from/to are slugs):\n"
        f"{json.dumps(rel_slice, ensure_ascii=False, indent=1)}\n"
        "\n"
        "TRANSCRIPT EXCERPT (may be partial):\n"
        f"{transcript_excerpt}\n"
        "\n"
        'Return a JSON object: {"ops": [...], "rationale": ["...", ...]}\n'
        "Each op is one of:\n"
        ' {"op": "event_update", "event_key": "...", "after": '
        '{"summary": "...", "kind": "...", "ts": "..."} (only changed fields)\n'
        ' {"op": "event_delete", "event_key": "..."}\n'
        ' {"op": "relation_create", "from": "slug", "to": "slug", "type": "..."}\n'
        ' {"op": "relation_delete", "from": "slug", "to": "slug", "type": "..."}\n'
        ' {"op": "entity_merge", "source": "slug", "target": "slug"}\n'
        ' {"op": "entity_delete", "slug": "..."}\n'
        "rationale: one short line per op, in the same order.\n"
        "If the instruction needs no graph change, return empty ops "
        '({"ops": [], "rationale": []}). Never invent event_keys or slugs '
        "that are not in the current state above."
    )


_EVENT_OPS = {"event_update", "event_delete"}
_KNOWN_OPS = _EVENT_OPS | {
    "relation_create",
    "relation_delete",
    "entity_merge",
    "entity_delete",
}


def _parse_proposal(payload: Any) -> dict:
    """Model JSON → validated proposal {ops, rationale}. Raises
    ValueError on structural garbage; unknown/oversized op lists are
    rejected outright (the API surfaces 502 with the parse error)."""
    if not isinstance(payload, dict):
        raise TypeError("proposal is not a JSON object")
    raw_ops = payload.get("ops", [])
    rationale = payload.get("rationale", [])
    if not isinstance(raw_ops, list) or not isinstance(rationale, list):
        raise TypeError("ops/rationale must be lists")
    if len(raw_ops) > _MAX_PROPOSAL_OPS:
        raise ValueError(f"proposal carries {len(raw_ops)} ops (max {_MAX_PROPOSAL_OPS})")
    ops: list[dict] = []
    for i, op in enumerate(raw_ops):
        if not isinstance(op, dict):
            raise TypeError(f"op #{i} is not an object")
        kind = op.get("op")
        if kind not in _KNOWN_OPS:
            raise ValueError(f"op #{i}: unknown op {kind!r}")
        if kind in _EVENT_OPS:
            key = op.get("event_key")
            if not isinstance(key, str) or not key:
                raise ValueError(f"op #{i}: event_key required")
            if kind == "event_update":
                after = op.get("after")
                if not isinstance(after, dict) or not after:
                    raise ValueError(f"op #{i}: event_update needs a non-empty after")
                ops.append({"op": kind, "event_key": key, "after": after})
            else:
                ops.append({"op": kind, "event_key": key})
        elif kind in ("relation_create", "relation_delete"):
            f, t, ty = op.get("from"), op.get("to"), op.get("type")
            if not all(isinstance(x, str) and x for x in (f, t, ty)):
                raise ValueError(f"op #{i}: from/to/type required")
            ops.append({"op": kind, "from": f, "to": t, "type": ty})
        elif kind == "entity_merge":
            src, tgt = op.get("source"), op.get("target")
            if not isinstance(src, str) or not isinstance(tgt, str) or not src or not tgt:
                raise ValueError(f"op #{i}: source/target required")
            ops.append({"op": kind, "source": src, "target": tgt})
        else:  # entity_delete
            slug = op.get("slug")
            if not isinstance(slug, str) or not slug:
                raise ValueError(f"op #{i}: slug required")
            ops.append({"op": kind, "slug": slug})
    notes = [str(r) for r in rationale if r is not None]
    return {"ops": ops, "rationale": notes[: len(ops)] if ops else []}


def build_fix_context(
    cfg: Any, tag: str, recording_id: str | None
) -> tuple[list[dict], list[dict], list[dict], str, str, str]:
    """(events, entities, relations, title, transcript_excerpt, rec_id)
    for the fix prompt. Target recording = ``recording_id`` when given
    and present, else the NEWEST done recording of the tag. The
    entity/relation aggregate spans the tag (the fix may touch any
    of them); events come from the target recording only."""
    from .db import Recording, session

    rec_ids = tag_recording_ids(cfg, tag)
    rid = ""
    if recording_id and recording_id in rec_ids:
        rid = recording_id
    elif rec_ids:
        rid = recording_id if recording_id else rec_ids[-1]
        if rid not in rec_ids:
            rid = rec_ids[-1]
    title = ""
    if rid:
        with session() as s:
            row = s.get(Recording, rid)
            title = row.title if row else ""
    doc = _read_events_doc(cfg, rid) if rid else {}
    events = [e for e in doc.get("events", []) if isinstance(e, dict)]
    entities = [e for e in doc.get("entities", []) if isinstance(e, dict)]
    relations = [r for r in doc.get("relations", []) if isinstance(r, dict)]

    # Tag-wide aggregate for entities/relations (dedup by slug / triple).
    for other in rec_ids:
        if other == rid:
            continue
        d = _read_events_doc(cfg, other)
        have = {e.get("slug") for e in entities}
        for e in d.get("entities", []):
            if isinstance(e, dict) and e.get("slug") not in have:
                entities.append(e)
                have.add(e.get("slug"))
        have_rel = {(r.get("from"), r.get("to"), r.get("type")) for r in relations}
        for r in d.get("relations", []):
            if isinstance(r, dict) and (r.get("from"), r.get("to"), r.get("type")) not in have_rel:
                relations.append(r)
                have_rel.add((r.get("from"), r.get("to"), r.get("type")))

    excerpt = ""
    if rid:
        from .activities import meta_dir

        excerpt = _transcript_window(meta_dir(rid) / "transcript.md", None)
    return events, entities, relations, title, excerpt, rid


def run_fix_preview(
    cfg: Any, tag: str, instruction: str, recording_id: str | None
) -> dict:
    """ONE LLM call → validated proposal. Returns
    ``{"ok": True, "proposal": {...}, "context": {...}}`` or
    ``{"ok": False, "reason": "busy", ...}`` — never raises for a
    timeout/unhealthy proxy (structured busy; the UI shows
    'Summarizer busy — retry shortly')."""
    events, entities, relations, title, excerpt, rid = build_fix_context(
        cfg, tag, recording_id
    )
    prompt = _fix_prompt(
        instruction, title, events, entities, relations, excerpt
    )
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    messages = system_first_messages(
        [
            {"role": "system", "content": "Follow the user's instructions."},
            {"role": "user", "content": prompt},
        ]
    )
    try:
        r = httpx.post(
            cfg.summarize.base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            json={
                "model": cfg.summarize.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
            timeout=_FIX_HTTP_TIMEOUT_SEC,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("fix-preview LLM call failed: %s", exc)
        return {"ok": False, "reason": "busy", "detail": str(exc)[:200]}
    content = r.json()["choices"][0]["message"]["content"]
    try:
        payload = json.loads(_json_payload(content))
    except (ValueError, TypeError) as exc:
        log.warning("fix-preview: unparseable model output: %s", exc)
        return {
            "ok": False,
            "reason": "unparseable",
            "detail": str(exc)[:200],
        }
    try:
        proposal = _parse_proposal(payload)
    except ValueError as exc:
        return {"ok": False, "reason": "invalid", "detail": str(exc)[:200]}
    return {
        "ok": True,
        "proposal": proposal,
        "context": {"recording_id": rid, "title": title},
    }
