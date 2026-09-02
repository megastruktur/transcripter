"""Phase B unit tests: the {corrections} feedback block.

Covers the render contract (empty in → empty out, capped fetch), the
Postgres filter (active/applied only, most-recent first, per-tag
scope, retired/orphaned excluded), and the enrich-activity wiring
(corrections block reaches extract_from_transcript alongside
known_entities)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.db import (
    Base,
    EditOp,
    EditStatus,
    EditTarget,
    GraphEdit,
    Recording,
    RecordingState,
    Stage,
    session,
)
from worker.enrich import (
    _CORRECTIONS_MAX_CHARS,
    _CORRECTIONS_MAX_ITEMS,
    active_corrections_for_tags,
    render_corrections,
)


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    import worker.enrich as _enrich_mod

    monkeypatch.setattr(_enrich_mod, "dedup_llm_gate", lambda cfg: True)


def _edit(
    tag: str = "campaign",
    feedback: str | None = "fix the attribution",
    status: EditStatus = EditStatus.applied,
    created_at: datetime | None = None,
) -> GraphEdit:
    return GraphEdit(
        tag=tag,
        target=EditTarget.event,
        op=EditOp.update,
        obj_key="k",
        anchor={},
        before={},
        after={},
        feedback_text=feedback,
        source="user",
        status=status,
        created_at=created_at or datetime.now(UTC),
    )


# ---------- render ----------


def test_render_empty_is_empty_string() -> None:
    """Same rule as known_entities: empty input must read exactly like
    a disabled block (2026-08-30 '(none)' JSON-break lesson)."""
    assert render_corrections([]) == ""


def test_render_lines_are_dashed_items() -> None:
    assert render_corrections(["a", "b"]) == "- a\n- b"


# ---------- fetch ----------


def test_fetch_returns_applied_feedback_most_recent_first() -> None:
    now = datetime.now(UTC)
    with session() as s:
        s.add(_edit(feedback="old", created_at=now - timedelta(hours=2)))
        s.add(_edit(feedback="new", created_at=now))
        s.commit()
    assert active_corrections_for_tags(["campaign"]) == ["new", "old"]


def test_fetch_excludes_retired_orphaned_and_textless() -> None:
    with session() as s:
        s.add(_edit(feedback="applied one"))
        s.add(_edit(feedback="retired", status=EditStatus.retired))
        s.add(_edit(feedback="orphaned", status=EditStatus.orphaned))
        s.add(_edit(feedback=None))
        s.commit()
    assert active_corrections_for_tags(["campaign"]) == ["applied one"]


def test_fetch_scoped_to_requested_tags() -> None:
    now = datetime.now(UTC)
    with session() as s:
        s.add(_edit(tag="campaign", feedback="mine", created_at=now - timedelta(minutes=1)))
        s.add(_edit(tag="other", feedback="not mine", created_at=now))
        s.commit()
    assert active_corrections_for_tags(["campaign"]) == ["mine"]
    assert active_corrections_for_tags(["campaign", "other"]) == [
        "not mine",
        "mine",
    ]


def test_fetch_caps_item_count() -> None:
    now = datetime.now(UTC)
    with session() as s:
        for i in range(_CORRECTIONS_MAX_ITEMS + 5):
            s.add(
                _edit(
                    feedback=f"c{i}",
                    created_at=now - timedelta(minutes=_CORRECTIONS_MAX_ITEMS + 5 - i),
                )
            )
        s.commit()
    got = active_corrections_for_tags(["campaign"])
    assert len(got) == _CORRECTIONS_MAX_ITEMS
    assert got[0] == f"c{_CORRECTIONS_MAX_ITEMS + 4}"  # newest survived


def test_fetch_caps_total_chars() -> None:
    """A wall of long items stops before blowing the ~2000-char cap
    (at least one item always renders — the cap never yields an empty
    block when feedback exists)."""
    filler = "x" * 800
    now = datetime.now(UTC)
    with session() as s:
        for i in range(5):
            s.add(
                _edit(
                    feedback=f"{filler}#{i}",
                    created_at=now - timedelta(minutes=5 - i),
                )
            )
        s.commit()
    got = active_corrections_for_tags(["campaign"])
    assert 0 < len(got) < 5
    assert sum(len(t) for t in got) <= _CORRECTIONS_MAX_CHARS + len(got) * 2


def test_fetch_db_failure_returns_empty() -> None:
    """Best-effort contract: corrections must NEVER fail the stage."""
    with patch("worker.db.session", side_effect=RuntimeError("db gone")):
        assert active_corrections_for_tags(["campaign"]) == []


# ---------- activity wiring ----------


def _make_cfg(tmp_path: Path) -> Any:
    cfg = MagicMock()
    cfg.graph.enabled = True
    cfg.graph.uri = "bolt://n:7687"
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.graph.enrich_all = True
    cfg.graph.auto_digest = False
    cfg.graph.auto_digest_window_sec = 3600
    cfg.profiles.path = tmp_path / "profiles"
    cfg.storage.path = tmp_path / "storage"
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    return cfg


@pytest.fixture()
def recording_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    import uuid

    rid = str(uuid.uuid4())
    with session() as s:
        s.add(
            Recording(
                id=rid,
                title="Session 1",
                tags=["campaign"],
                state=RecordingState.done,
                sha256="x" * 64,
                committed_bytes=100,
                total_bytes=100,
                duration_sec=60.0,
            )
        )
        s.commit()
    for kind in ("chunk", "transcribe", "diarize", "merge_speakers", "summarize", "enrich"):
        with session() as s:
            s.add(Stage(recording_id=rid, kind=kind))
            s.commit()
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(activities, "_cfg", cfg)
    meta = cfg.recordings_root / rid / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "transcript.md").write_text("the meeting transcript", encoding="utf-8")
    return rid


def test_activity_passes_corrections_block_to_extraction(recording_id: str, tmp_path: Path) -> None:
    """The fallback path (no profile, enrich_all) renders corrections
    from Postgres and hands them to extract_from_transcript as the 6th
    argument, after known_entities."""
    cfg = _make_cfg(tmp_path)
    captured: dict[str, Any] = {}
    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []

    with session() as s:
        s.add(_edit(tag="campaign", feedback="the operator built it, not Glennis"))
        s.commit()

    def fake_extract(path, title, prompt_template, c, known_entities="", corrections=""):
        captured["known_entities"] = known_entities
        captured["corrections"] = corrections
        return graph

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch("worker.enrich.extract_from_transcript", side_effect=fake_extract),
        patch("worker.enrich.resolve_slugs", return_value=graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        asyncio.run(activities.enrich(recording_id))

    assert captured["corrections"] == "- the operator built it, not Glennis"
    # The fallback prompt carries the placeholder the block fills.
    from worker.enrich import _FALLBACK_ENRICH_PROMPT

    assert "{corrections}" in _FALLBACK_ENRICH_PROMPT


def test_activity_without_corrections_sends_empty_block(recording_id: str, tmp_path: Path) -> None:
    """No edits in the store → the block is the empty string, not None
    (the render contract: empty reads like disabled)."""
    cfg = _make_cfg(tmp_path)
    captured: dict[str, Any] = {}
    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []

    def fake_extract(path, title, prompt_template, c, known_entities="", corrections=""):
        captured["corrections"] = corrections
        return graph

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch("worker.enrich.extract_from_transcript", side_effect=fake_extract),
        patch("worker.enrich.resolve_slugs", return_value=graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        asyncio.run(activities.enrich(recording_id))

    assert captured["corrections"] == ""
