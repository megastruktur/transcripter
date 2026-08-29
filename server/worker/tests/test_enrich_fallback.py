"""Phase 2 enrich_all fallback: no matching profile + graph.enrich_all →
the built-in fallback prompt (profile_id 'builtin-fallback'); enrich_all
off or a matched profile without an enrich section still skips."""

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
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    return cfg


@pytest.fixture()
def recording_id(tmp_path: Path) -> str:
    import uuid

    rid = str(uuid.uuid4())
    with session() as s:
        # type=None → no profile can match (routing is by recording.type).
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
    cfg = activities._cfg
    meta = cfg.recordings_root / rid / "meta"
    meta.mkdir(parents=True)
    (meta / "transcript.md").write_text("the meeting transcript", encoding="utf-8")
    return rid


def _cfg(graph_enabled: bool, tmp_path: Path, enrich_all: bool = True) -> Any:
    cfg = _make_cfg(tmp_path)
    cfg.graph.enabled = graph_enabled
    cfg.graph.uri = "bolt://n" if graph_enabled else ""
    cfg.graph.enrich_all = enrich_all
    cfg.graph.auto_digest = False
    cfg.graph.auto_digest_window_sec = 3600
    return cfg


def _run(coro):
    return asyncio.run(coro)


def test_fallback_used_when_no_profile_and_enrich_all(
    recording_id: str, tmp_path: Path
) -> None:
    """No profile matches + enrich_all → extraction runs with the built-in
    fallback prompt, profile_id 'builtin-fallback', and the fallback's
    namespaces rule (tags → namespaces, empty → untagged) holds."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    captured: dict[str, Any] = {}

    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []

    def fake_extract(path, title, prompt_template, c, known_entities=""):
        captured["prompt"] = prompt_template
        captured["known_entities"] = known_entities
        return graph

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch(
            "worker.enrich.extract_from_transcript", side_effect=fake_extract
        ),
        patch("worker.enrich.resolve_slugs", return_value=graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))

    assert result["profile_id"] == "builtin-fallback"
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done
        assert st.details["profile_id"] == "builtin-fallback"
        assert st.details["namespaces"] == ["campaign"]  # tag → namespace
    # The fallback prompt is what got sent, and it carries the mandatory
    # placeholders; known-entities was rendered for it (enabled by default
    # in the fallback spec).
    from worker.enrich import _FALLBACK_ENRICH_PROMPT

    assert captured["prompt"] is _FALLBACK_ENRICH_PROMPT
    for ph in ("{title}", "{transcript}", "{known_entities}"):
        assert ph in captured["prompt"]
    assert captured["known_entities"]  # rendered block (not empty)


def test_fallback_renders_known_entities_block(
    recording_id: str, tmp_path: Path
) -> None:
    """The fallback spec enables known_entities: the block for an EMPTY
    namespace is the literal '(none)'."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    captured: dict[str, Any] = {}

    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []

    def fake_extract(path, title, prompt_template, c, known_entities=""):
        captured["known_entities"] = known_entities
        return graph

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch(
            "worker.enrich.extract_from_transcript", side_effect=fake_extract
        ),
        patch("worker.enrich.resolve_slugs", return_value=graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        # The known-entities snapshot dies (flakey neo4j) — best-effort:
        # the activity must log, render the empty-namespace literal and
        # keep going.
        patch(
            "worker.enrich.list_known_entities",
            side_effect=RuntimeError("no graph here"),
        ),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        _run(activities.enrich(recording_id))

    # Lookup died (best-effort) → the empty-namespace literal.
    assert captured["known_entities"] == "(none)"


def test_no_match_enrich_all_false_skips(recording_id: str, tmp_path: Path) -> None:
    """enrich_all=False restores the pre-phase-2 skip for no-match."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path, enrich_all=False)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
    ):
        result = _run(activities.enrich(recording_id))
    assert result == {"skipped": "no profile with enrich"}
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.skipped


def test_profile_without_enrich_still_skips(recording_id: str, tmp_path: Path) -> None:
    """A MATCHED profile without an enrich section = opted out, even with
    enrich_all on: domain steering exists, enrich absence is deliberate."""
    from worker.profiles import Profile, SummarizeSpec

    profile = Profile(
        id="meeting",
        version="1.0.0",
        min_host_version="0.10.0",
        display_name="Meeting",
        description="d",
        type="meeting",
        summarize=SummarizeSpec(prompt="s {transcript}"),
        enrich=None,
    )
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path, enrich_all=True)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
    ):
        result = _run(activities.enrich(recording_id))
    assert result == {"skipped": "no profile with enrich"}
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.skipped


def test_fallback_extraction_failure_fails_stage(
    recording_id: str, tmp_path: Path
) -> None:
    """The fallback path is a real extraction: an LLM failure marks the
    stage failed (the stage stays best-effort for the workflow)."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch(
            "worker.enrich.extract_from_transcript",
            side_effect=ValueError("LLM gave up"),
        ),
        pytest.raises(ValueError, match="LLM gave up"),
    ):
        _run(activities.enrich(recording_id))
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.failed
        assert "LLM gave up" in st.last_error
