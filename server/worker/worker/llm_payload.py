"""Outgoing chat-payload invariant: a ``system`` message MUST sit at
position 0.

llama-server renders the GGUF chat template with Jinja and this model's
template hard-fails when a ``system`` turn is anywhere but first —
``Error: Jinja Exception: System message must be at the beginning.`` —
which LiteLLM surfaces as HTTP 500 (observed live 2026-08-29). The repo
already has four independent builders of the ``messages`` array
(summarize, digest, enrich extraction, enrich dedup); each held the
invariant by convention only, so a recap-style injection, a
retry-with-continuation, or a history replay that inserts a second
``system`` entry anywhere reintroduces the 500.

``system_first_messages`` is the single choke point: it hoists/merges
system content to position 0 and returns a payload that is always
template-safe. Pure function; no I/O.
"""

from __future__ import annotations

from typing import Any

Message = dict[str, Any]


def system_first_messages(messages: list[Message]) -> list[Message]:
    """Return a reordered copy where all ``system`` content is merged into
    exactly one leading system message and every non-system message keeps
    its relative order and identity.

    Empty/None content and unknown roles are ignored for the merge; an
    array with no hoistable system content is returned unchanged. The
    merged system message is always rebuilt, so compliant callers see no
    value change on the wire.
    """
    system_parts = [
        m["content"] for m in messages if m.get("role") == "system" and m.get("content")
    ]
    rest = [m for m in messages if m.get("role") != "system"]
    if not system_parts:
        return list(messages)
    return [{"role": "system", "content": "\n\n".join(system_parts)}, *rest]
