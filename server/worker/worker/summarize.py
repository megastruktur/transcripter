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


def summarize_transcript(
    meta: Path,
    cfg,
    prompt_template: str | None = None,
    title: str = "",
) -> str:
    transcript = (meta / "transcript.md").read_text(encoding="utf-8")
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    # Keyless local endpoints reject (and httpx forbids) an empty "Bearer ".
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    if prompt_template is not None:
        # Profile mode: single user message with substitution; fixed system.
        # Apply the same truncate cap to keep the wire shape stable.
        user_content = _render_profile_prompt(prompt_template, title, transcript)
        messages = [
            {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content[:_TRANSCRIPT_LIMIT]},
        ]
    else:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript[:_TRANSCRIPT_LIMIT]},
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
    return template.format(title=title or "", transcript=transcript)
