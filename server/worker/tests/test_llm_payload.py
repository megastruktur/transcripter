"""System-first message invariant for outgoing chat payloads.

llama-server's Jinja chat template for qwen3.8-27b-q4_k_m raises
"System message must be at the beginning" (→ HTTP 500 via LiteLLM,
observed live 2026-08-29) whenever a system turn is not strictly first.
These tests pin the invariant on ``system_first_messages`` — the single
choke point all four LLM payload builders route through (summarize,
digest, enrich extraction, enrich dedup) — for every disordered shape:
system after user, system mid-array, duplicate systems, and the
already-correct baseline.
"""

from worker.llm_payload import system_first_messages


def test_baseline_already_correct_passes_through() -> None:
    msgs = [
        {"role": "system", "content": "Follow the user's instructions."},
        {"role": "user", "content": "hello"},
    ]
    out = system_first_messages(msgs)
    # Byte-identical wire shape for compliant callers.
    assert out == msgs
    # Non-system entries are passed through untouched.
    assert out[1] is msgs[1]


def test_system_after_user_is_hoisted() -> None:
    out = system_first_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "s"},
        ]
    )
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == "s"
    assert out[1]["content"] == "hi"


def test_system_mid_array_is_hoisted() -> None:
    out = system_first_messages(
        [
            {"role": "user", "content": "q1"},
            {"role": "system", "content": "mid"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a"},
        ]
    )
    assert [m["role"] for m in out] == ["system", "user", "user", "assistant"]
    assert out[0]["content"] == "mid"
    # Non-system relative order preserved.
    assert [m["content"] for m in out[1:]] == ["q1", "q2", "a"]


def test_duplicate_systems_merge_into_one_leading_message() -> None:
    out = system_first_messages(
        [
            {"role": "system", "content": "first"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "second"},
            {"role": "system", "content": "third"},
        ]
    )
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == "first\n\nsecond\n\nthird"
    assert out[1]["content"] == "u"


def test_no_system_leaves_array_unchanged() -> None:
    msgs = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
    out = system_first_messages(msgs)
    assert out == msgs


def test_empty_system_entry_left_as_is() -> None:
    # An empty-content system message contributes nothing to hoist; the
    # array passes through unchanged (a degenerate input, not worth a
    # second code path).
    msgs = [
        {"role": "system", "content": ""},
        {"role": "user", "content": "u"},
    ]
    assert system_first_messages(msgs) == msgs


def test_empty_list_round_trips() -> None:
    assert system_first_messages([]) == []
