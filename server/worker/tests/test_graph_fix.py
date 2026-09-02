"""Phase C unit tests: the "Correct the record" fix agent.

Parser (proposal shapes, caps), lenient ts parsing, transcript
windowing, and the LLM-call failure contract (busy → structured
result, never a raise). API contract tests live in test_graph_edits.py
neighbourhood: 202 + gate 409/429 + poll shapes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from worker.graph_fix import (
    _MAX_PROPOSAL_OPS,
    _parse_proposal,
    _parse_ts,
    _transcript_window,
    run_fix_preview,
)

# ---------- lenient ts ----------


def test_parse_ts_formats() -> None:
    assert _parse_ts("00:42:13") == 42 * 60 + 13
    assert _parse_ts("12:34") == 12 * 60 + 34
    assert _parse_ts("1:02:03.5") == 3600 + 120 + 3
    assert _parse_ts("") is None
    assert _parse_ts("banana") is None
    assert _parse_ts("99:99") is None  # minutes/seconds out of range


# ---------- transcript window ----------


def test_transcript_window_caps_and_stamps(tmp_path: Path) -> None:
    t = tmp_path / "transcript.md"
    t.write_text(
        "[00:00:01] cold open\n"
        "[00:40:00] the agent network discussion\n"
        "[00:41:00] still on topic\n"
        "[02:00:00] much later\n",
        encoding="utf-8",
    )
    window = _transcript_window(t, "00:40:30")
    assert "agent network" in window
    assert "cold open" not in window
    assert "much later" not in window
    # No ts → head excerpt, still capped.
    head = _transcript_window(t, None)
    assert head.startswith("[00:00:01]")
    # Missing file → empty string, no raise.
    assert _transcript_window(tmp_path / "gone.md", None) == ""


def test_transcript_window_char_cap(tmp_path: Path) -> None:
    t = tmp_path / "transcript.md"
    t.write_text("x" * 10_000, encoding="utf-8")
    assert len(_transcript_window(t, None)) <= 6000


# ---------- proposal parser ----------


def _payload(ops: list[dict], rationale: list[str] | None = None) -> dict:
    return {"ops": ops, "rationale": rationale if rationale is not None else ["r"] * len(ops)}


def test_parser_accepts_all_op_shapes() -> None:
    out = _parse_proposal(
        _payload(
            [
                {"op": "event_update", "event_key": "k1", "after": {"summary": "s"}},
                {"op": "event_delete", "event_key": "k2"},
                {"op": "relation_create", "from": "a", "to": "b", "type": "works_on"},
                {"op": "relation_delete", "from": "a", "to": "b", "type": "works_on"},
                {"op": "entity_merge", "source": "x", "target": "y"},
                {"op": "entity_delete", "slug": "z"},
            ],
            ["a", "b", "c", "d", "e", "f"],
        )
    )
    assert len(out["ops"]) == 6
    assert out["rationale"] == ["a", "b", "c", "d", "e", "f"]


def test_parser_empty_ops_is_valid() -> None:
    out = _parse_proposal({"ops": [], "rationale": []})
    assert out == {"ops": [], "rationale": []}


def test_parser_rejects_garbage() -> None:
    import pytest

    with pytest.raises(TypeError):
        _parse_proposal("nope")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _parse_proposal({"ops": "x", "rationale": []})
    with pytest.raises(ValueError):
        _parse_proposal(_payload([{"op": "event_delete"}]))  # no key
    with pytest.raises(ValueError):
        _parse_proposal(_payload([{"op": "explode_everything", "x": 1}]))
    with pytest.raises(ValueError):
        _parse_proposal(_payload([{"op": "relation_create", "from": "", "to": "b", "type": "t"}]))


def test_parser_caps_op_count() -> None:
    ops = [{"op": "entity_delete", "slug": f"s{i}"} for i in range(_MAX_PROPOSAL_OPS + 1)]
    import pytest

    with pytest.raises(ValueError, match="max"):
        _parse_proposal(_payload(ops))


# ---------- LLM call contract ----------


def _cfg() -> Any:
    cfg = MagicMock()
    cfg.summarize.base_url = "http://llm"
    cfg.summarize.model = "test-model"
    cfg.summarize.api_key_env = "LLM_KEY"
    cfg.graph.enabled = True
    return cfg


def test_preview_timeout_is_structured_busy() -> None:
    """httpx timeout/5xx → {'ok': False, 'reason': 'busy'} — never a
    raise (the UI shows 'Summarizer busy — retry shortly')."""
    import httpx

    with (
        patch("worker.graph_fix.build_fix_context", return_value=([], [], [], "t", "", "r1")),
        patch("worker.graph_fix.httpx.post", side_effect=httpx.ReadTimeout("slow")),
    ):
        out = run_fix_preview(_cfg(), "tag", "fix it", None)
    assert out["ok"] is False
    assert out["reason"] == "busy"


def test_preview_unparseable_output_is_structured() -> None:

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"choices": [{"message": {"content": "```json\n{oops"}}]}
    with (
        patch("worker.graph_fix.build_fix_context", return_value=([], [], [], "t", "", "r1")),
        patch("worker.graph_fix.httpx.post", return_value=resp),
    ):
        out = run_fix_preview(_cfg(), "tag", "fix it", None)
    assert out["ok"] is False
    assert out["reason"] in ("unparseable", "invalid")


def test_preview_happy_path_returns_proposal() -> None:

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '```json\n{"ops": [{"op": "entity_delete", "slug": "dupe"}], "rationale": ["dedupe"]}\n```'
                }
            }
        ]
    }
    with (
        patch("worker.graph_fix.build_fix_context", return_value=([], [], [], "t", "", "r1")),
        patch("worker.graph_fix.httpx.post", return_value=resp),
    ):
        out = run_fix_preview(_cfg(), "tag", "remove the duplicate entity", None)
    assert out["ok"] is True
    assert out["proposal"]["ops"][0]["slug"] == "dupe"
    assert out["context"]["recording_id"] == "r1"
