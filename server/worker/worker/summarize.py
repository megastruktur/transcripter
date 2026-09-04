"""Summarization via OpenAI-compatible chat endpoint (opt-in).

When a profile's prompt is supplied (wave A — yaml knowledge-graph profiles),
the prompt is used verbatim with ``{title}`` and ``{transcript}`` substituted
inline and sent as a SINGLE user message; the system message is fixed to
"Follow the user's instructions." per the contract.

Without a profile prompt, the legacy behavior is preserved bit-for-bit:
SYSTEM_PROMPT + transcript[:100_000] as user.
"""

import logging
import os
import sqlite3
import struct
from pathlib import Path
from typing import Any

import httpx

from .llm_payload import system_first_messages

log = logging.getLogger("transcripter.summarize")

SYSTEM_PROMPT = (
    "You are a meeting assistant. Summarize the call transcript in the same "
    "language as the transcript. Structure: 3-5 bullet key points, then "
    "decisions made (if any), then action items with owners if identifiable."
)

# Wave A profile-mode system prompt: per contract, the profile prompt is the
# whole instruction and the system message is a fixed pointer.
PROFILE_SYSTEM_PROMPT = "Follow the user's instructions."

_TRANSCRIPT_LIMIT = 100_000  # legacy truncate cap (unchanged)

# Recap-retrieval defaults (overridable via summarize.recap_k /
# summarize.recap_budget_chars): 6 hits keep the block informative without
# flooding the prior-context; 1600 chars rides comfortably next to the
# 4000-char digest cap inside the 100k prompt budget.
_RECAP_K_DEFAULT = 6
_RECAP_BUDGET_DEFAULT = 1600
_PER_HIT_CHARS = 420
_MAX_HITS_PER_RECORDING = 2
# KNN over-fetch before the self-exclusion filter (see the comment at
# the query): fixed, deliberately wide. Covers a regenerate whose own
# segments dominate the top of the ranking.
_KNN_FETCH = 200


def _int_knob(holder: Any, name: str, default: int) -> int:
    """Read an int config knob; MagicMock/test configs fall back to the
    default instead of exploding on int(MagicMock)."""
    try:
        v = int(getattr(holder, name))
    except (AttributeError, TypeError, ValueError):
        return default
    return v if v > 0 else default


def _fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def build_recap(
    tag: str,
    transcripts_root: Path,
    max_chars: int = 4000,
    *,
    recording_id: str = "",
    meta_dir: Path | None = None,
    cfg: Any = None,
) -> str | None:
    """Recap context for the summarize prompt: the tag's digest note body,
    OPTIONALLY extended with semantically related segments from the tag's
    Phase 3.5 index (the recap-retrieval tail).

    Digest-only when called without the retrieval kwargs (legacy callers,
    tests) or when the index/backend is unavailable — the LLM keeps the
    rolling-narrative prior it always had. With ``recording_id`` +
    ``meta_dir`` + ``cfg``, KNN over ``indexes/<slug>.sqlite`` retrieves
    segments from OTHER recordings of the tag — the CURRENT recording is
    excluded because stage order is summarize → enrich, so on a regenerate
    the current session is already indexed and must not come back as its
    own "prior" context — and the hits render into a compact block
    appended after the digest body.

    The digest keeps its own ``max_chars`` cap and the retrieval block its
    own budget knob: one shared cap would let a huge digest erase the
    retrieval (or vice versa).

    Returns None when there is nothing at all (no digest note AND no
    usable retrieval) — the prompt then runs without prior context.
    """
    from .digest import _FRONTMATTER_RE, _existing_digest_for_tag

    digests_dir = transcripts_root / "digests"
    path = _existing_digest_for_tag(digests_dir, tag)
    body: str | None = None
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log.info("digest note %s unreadable — summarize runs without recap", path)
        else:
            body = _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")
            if len(body) > max_chars:
                body = body[:max_chars].rstrip() + "\n…(truncated)"
    if cfg is None or recording_id == "" or meta_dir is None:
        return body
    # Best-effort retrieval: any failure logs and keeps the digest-only
    # recap — a search hiccup must never fail the summarize stage.
    try:
        block = _related_earlier_discussion(
            tag, transcripts_root, recording_id, meta_dir, cfg
        )
    except Exception:  # retrieval is optional prior context — never fatal
        log.info(
            "recap retrieval failed for tag %r — digest-only recap", tag, exc_info=True
        )
        return body
    if not block:
        return body
    if body is None:
        return block
    return body.rstrip("\n") + "\n\n" + block


def _related_earlier_discussion(
    tag: str,
    transcripts_root: Path,
    recording_id: str,
    meta_dir: Path,
    cfg: Any,
) -> str:
    """Render the retrieval block; empty string when nothing retrievable.

    Query = the recording's FIRST transcript window (the opening states
    the agenda, so it retrieves the right earlier discussion even for a
    first recording of a tag that has no digest note yet). Hits come
    from the tag's vec0 index; per-recording diversity is capped so one
    long session cannot eat the whole block.
    """
    from .embeddings import embed_texts
    from .semantic_index import index_path, segment_transcripts

    path = index_path(transcripts_root, tag)
    if not path.is_file():
        return ""
    windows = segment_transcripts(meta_dir)
    if not windows:
        return ""
    k = _int_knob(getattr(cfg, "summarize", None), "recap_k", _RECAP_K_DEFAULT)
    budget = _int_knob(
        getattr(cfg, "summarize", None), "recap_budget_chars", _RECAP_BUDGET_DEFAULT
    )
    query_vec = embed_texts([windows[0].text], cfg)
    if not query_vec or not query_vec[0]:
        return ""
    vec = query_vec[0]
    import sqlite_vec

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        # Over-fetch then filter in Python: vec0's MATCH operator cannot
        # take a WHERE on the joined meta table, so exclusion of the
        # current recording happens AFTER the KNN (same read-only idiom
        # as the api's search routes). The fetch is a FIXED 200, not
        # k*4: on a regenerate the query window sits verbatim in the
        # index (distance ~0), so a small over-fetch can be ALL self
        # rows and starve the retrieval to zero (live-verified on the
        # daily blob: 818 segments, k*4=24 → 0 prior hits). Brute-force
        # KNN over a per-tag index of hundreds-to-low-thousands of rows
        # is milliseconds — 200 covers any single recording's share.
        rows = db.execute(
            "SELECT m.recording_id, m.session_title, m.ts_start, m.text, distance "
            "FROM segments JOIN segments_meta AS m ON segments.rowid = m.rowid "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (struct.pack(f"{len(vec)}f", *vec), _KNN_FETCH),
        ).fetchall()
    finally:
        db.close()
    hits: list[str] = []
    per_rec: dict[str, int] = {}
    used = 0
    for rid, session_title, ts_start, text, _distance in rows:
        if rid == recording_id:
            continue
        if per_rec.get(rid, 0) >= _MAX_HITS_PER_RECORDING:
            continue
        text = (text or "").strip()
        if not text:
            continue
        if len(text) > _PER_HIT_CHARS:
            text = text[:_PER_HIT_CHARS].rstrip() + "…"
        line = f"• «{session_title or rid}» @ {_fmt_ts(ts_start)}: {text}"
        if used + len(line) > budget and hits:
            break
        hits.append(line)
        per_rec[rid] = per_rec.get(rid, 0) + 1
        used += len(line)
        if len(hits) >= k:
            break
    if not hits:
        log.info(
            "recap retrieval: tag %r → 0 prior hits (self=%s excluded)", tag, recording_id
        )
        return ""
    log.info("recap retrieval: tag %r → %d prior hits", tag, len(hits))
    return (
        "Related earlier discussion (retrieved by semantic search over "
        "prior sessions of this series):\n" + "\n".join(hits)
    )


def summarize_transcript(
    meta: Path,
    cfg,
    prompt_template: str | None = None,
    title: str = "",
    recap_block: str | None = None,
    vocabulary_block: str | None = None,
) -> str:
    transcript = (meta / "transcript.md").read_text(encoding="utf-8")
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    # Keyless local endpoints reject (and httpx forbids) an empty "Bearer ".
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    if prompt_template is not None:
        # Profile mode: single user message with substitution; fixed system.
        # Apply the same truncate cap to keep the wire shape stable.
        user_content = _render_profile_prompt(prompt_template, title, transcript)
        system = PROFILE_SYSTEM_PROMPT
        user = user_content[:_TRANSCRIPT_LIMIT]
    else:
        system = SYSTEM_PROMPT
        user = transcript[:_TRANSCRIPT_LIMIT]
    # Recap rides INSIDE the single system message (appended after the
    # fixed instruction). NEVER a second system entry: llama-server's
    # Jinja chat template for this model rejects a system message in
    # the middle ("System message must be at the beginning", HTTP 500,
    # verified live 2026-08-29). Already capped by build_recap, never
    # truncated here — and it must NOT eat into the user-side cap.
    if recap_block:
        system = (
            system
            + "\n\nPrior context from this series' knowledge base "
            + "(digest and retrieved excerpts of earlier sessions):\n\n"
            + recap_block
        )
    # Vocabulary rides the same single-system-message rail as the recap
    # (same llama-server template constraint — see above). Appended
    # after the recap so terminology stays the LAST instruction before
    # the transcript; already capped by the caller.
    if vocabulary_block:
        system = (
            system
            + "\n\nГлоссарий(tag vocabulary — сохраняй точное написание терминов и имён):\n\n"
            + vocabulary_block
        )
    messages = system_first_messages(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    )
    r = httpx.post(
        cfg.summarize.base_url.rstrip("/") + "/chat/completions",
        headers=headers,
        json={
            "model": cfg.summarize.model,
            "messages": messages,
        },
        # Must stay 30s UNDER the Temporal start_to_close (2400s) so
        # httpx.ReadTimeout (an Exception, caught → stage failed) fires
        # before Temporal cancellation (CancelledError bypasses
        # except Exception and leaves the stage stuck running).
        timeout=2370,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _render_profile_prompt(template: str, title: str, transcript: str) -> str:
    """Format a profile prompt with {title} and {transcript} placeholders.

    Literal ``str.replace`` on purpose: ``str.format`` dies on KeyError
    the moment a profile's JSON-schema example carries literal braces
    (the 2026-08-28 bug in both summarize and enrich — see the lesson
    log; profiles are validated brace-free by the loader, so by the time
    we get here, the substitution is safe to run)."""
    return template.replace("{title}", title or "").replace("{transcript}", transcript)
