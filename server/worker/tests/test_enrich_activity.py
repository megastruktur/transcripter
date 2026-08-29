"""Enrich activity integration: skipped/done/failed paths.

The graph backend and the LLM are both mocked. The activity's own
branching (skipped without profile or graph, done on success, failed
on persistent errors) is what these tests cover — the unit-level
extraction/dedup/write logic lives in test_enrich.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.db import Base, Recording, RecordingState, Stage, StageStatus, session
from worker.profiles import EnrichNodeLabels, EnrichSpec, Profile, SummarizeSpec


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    monkeypatch.setattr(activities, "_cfg", _make_cfg(tmp_path))


def _make_cfg(tmp_path: Path) -> Any:
    cfg = MagicMock()
    cfg.graph.enabled = True
    cfg.graph.uri = "bolt://n:7687"
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.profiles.path = tmp_path / "profiles"
    cfg.storage.path = tmp_path / "storage"
    cfg.recordings_root = cfg.storage.path / "recordings"
    return cfg


@pytest.fixture()
def recording_id(tmp_path: Path) -> str:
    import uuid

    rid = str(uuid.uuid4())
    with session() as s:
        s.add(
            Recording(
                id=rid,
                title="Session 1",
                tags=["pathfinder"],
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
    # Transcript under the storage root the activity reads.
    cfg = activities._cfg
    meta = cfg.recordings_root / rid / "meta"
    meta.mkdir(parents=True)
    (meta / "transcript.md").write_text("the meeting transcript", encoding="utf-8")
    return rid


def _make_profile(has_enrich: bool) -> Profile:
    return Profile(
        id="pathfinder",
        version="1.0.0",
        min_host_version="0.10.0",
        display_name="Pathfinder",
        description="d",
        type="ttrpg",
        summarize=SummarizeSpec(prompt="sum {transcript}", output_artifact="session-log.md"),
        enrich=EnrichSpec(prompt="enr {transcript}", node_labels=EnrichNodeLabels())
        if has_enrich
        else None,
    )


def _cfg(graph_enabled: bool) -> Any:
    cfg = MagicMock()
    cfg.graph.enabled = graph_enabled
    cfg.graph.uri = "bolt://n" if graph_enabled else ""
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.profiles.path = Path("/nonexistent")
    return cfg


def _run(coro):
    return asyncio.run(coro)


def test_skipped_when_graph_disabled(recording_id: str) -> None:
    cfg = _cfg(graph_enabled=False)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=_make_profile(has_enrich=True)),
    ):
        result = _run(activities.enrich(recording_id))
    assert result == {"skipped": "graph disabled"}
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.skipped


def test_skipped_when_no_profile_with_enrich(recording_id: str) -> None:
    cfg = _cfg(graph_enabled=True)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=_make_profile(has_enrich=False)),
    ):
        result = _run(activities.enrich(recording_id))
    assert result == {"skipped": "no profile with enrich"}
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.skipped


def test_skipped_when_no_profile_at_all(recording_id: str) -> None:
    cfg = _cfg(graph_enabled=True)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
    ):
        result = _run(activities.enrich(recording_id))
    assert result == {"skipped": "no profile with enrich"}


def test_done_when_extraction_and_write_succeed(recording_id: str) -> None:
    cfg = _cfg(graph_enabled=True)
    profile = _make_profile(has_enrich=True)
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch(
            "worker.enrich.extract_from_transcript",
            return_value=extracted_graph,
        ),
        patch(
            "worker.enrich.resolve_slugs",
            return_value=extracted_graph,
        ),
        patch(
            "worker.enrich.pre_existing_lookup",
            return_value=MagicMock(),
        ),
        patch(
            "worker.enrich.write_to_graph",
            return_value=0,
        ),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))
    assert result["profile_id"] == "pathfinder"
    assert result["events"] == 0
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done
        assert st.details["profile_id"] == "pathfinder"


def test_failed_when_extraction_raises(recording_id: str) -> None:
    cfg = _cfg(graph_enabled=True)
    profile = _make_profile(has_enrich=True)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch(
            "worker.enrich.extract_from_transcript",
            side_effect=ValueError("LLM gave up"),
        ),pytest.raises(ValueError, match="LLM gave up")
    ):
        _run(activities.enrich(recording_id))
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.failed
        assert "LLM gave up" in st.last_error


def test_done_when_dedup_fails_falls_back_to_raw(recording_id: str) -> None:
    """Dedup is best-effort: a flakey LLM or lookup must never kill the
    stage. The activity falls back to the raw extraction graph and
    still marks the stage done."""
    cfg = _cfg(graph_enabled=True)
    profile = _make_profile(has_enrich=True)
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []

    lookup_mock = MagicMock()
    lookup_mock._driver.close = MagicMock()

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch(
            "worker.enrich.extract_from_transcript",
            return_value=extracted_graph,
        ),
        patch(
            "worker.enrich.resolve_slugs",
            side_effect=RuntimeError("lookup died"),
        ),
        patch(
            "worker.enrich.pre_existing_lookup",
            return_value=lookup_mock,
        ),
        patch(
            "worker.enrich.write_to_graph",
            return_value=0,
        ),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))
    assert result["profile_id"] == "pathfinder"
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done