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
from pathlib import Path

import httpx

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


def build_recap(tag: str, transcripts_root: Path, max_chars: int = 4000) -> str | None:
    """Recap context for the summarize prompt: the tag's digest note body.

    Reads the digest note the SAME way digest._existing_digest_for_tag
    does (frontmatter ``tag:`` match under ``<root>/digests/*.md``), then
    strips the frontmatter. Returns None when no note carries this tag or
    none of them is readable — a missing knowledge base is normal (first
    session of a series), so that path only logs at info.
    Pure function: no LLM, no graph, no writes.
    """
    from .digest import _FRONTMATTER_RE, _existing_digest_for_tag

    digests_dir = transcripts_root / "digests"
    path = _existing_digest_for_tag(digests_dir, tag)
    if path is None:
        log.info("no digest note for tag %r — summarize runs without recap", tag)
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log.info("digest note %s unreadable — summarize runs without recap", path)
        return None
    body = _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n…(truncated)"
    return body


def summarize_transcript(
    meta: Path,
    cfg,
    prompt_template: str | None = None,
    title: str = "",
    recap_block: str | None = None,
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
            + "(digest of earlier sessions):\n\n"
            + recap_block
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
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

    Validation that ``{transcript}`` is present happens in the Profile model
    (profiles.py) — by the time we get here, the substitution is safe to run."""
    # Literal replacement of exactly two placeholders — NOT str.format: profile
    # prompts may embed JSON examples whose braces format() would misread
    # (same incident class as enrich._render_prompt, 2026-08-27).
    return template.replace("{title}", title or "").replace("{transcript}", transcript)
