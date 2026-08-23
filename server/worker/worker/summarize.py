"""Summarization via OpenAI-compatible chat endpoint (opt-in)."""

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


def summarize_transcript(meta: Path, cfg) -> str:
    transcript = (meta / "transcript.md").read_text(encoding="utf-8")
    api_key = os.environ.get(cfg.summarize.api_key_env, "")
    # Keyless local endpoints reject (and httpx forbids) an empty "Bearer ".
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.post(
        cfg.summarize.base_url.rstrip("/") + "/chat/completions",
        headers=headers,
        json={
            "model": cfg.summarize.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript[:100_000]},
            ],
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
