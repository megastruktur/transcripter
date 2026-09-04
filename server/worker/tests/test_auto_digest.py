"""Phase 2 auto-digest: after a successful enrich, each affected
namespace's digest is refreshed INLINE when its digests/<slug>.md is
older than the window (or missing); fresh files, skipped stages, and
auto_digest=False never trigger a run. Best-effort — a failing digest
never fails enrich."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.exceptions import ApplicationError

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
    # transcripts root for digest placement (real tmp dir).
    cfg.vault.path = tmp_path / "transcripts"
    # Phase 3-F F3: the dedup soft gate reads summarize.* for its probe.
    cfg.summarize.base_url = "http://llm:8080/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
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


def _cfg(tmp_path: Path, *, auto_digest: bool = True, window: int = 3600) -> Any:
    cfg = _make_cfg(tmp_path)
    cfg.graph.auto_digest = auto_digest
    cfg.graph.auto_digest_window_sec = window
    cfg.graph.enrich_all = True
    return cfg


def _happy_enrich(env: dict, graph: MagicMock) -> None:
    """Standard successful-enrich patch stack."""
    stack = [
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch("worker.enrich.dedup_llm_gate", return_value=True),
        patch("worker.enrich.extract_from_transcript", return_value=graph),
        patch("worker.enrich.resolve_slugs", return_value=graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.list_known_entities", side_effect=RuntimeError("n/a")),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ]
    for ctx in stack:
        env.append(ctx.__enter__())
    return stack  # type: ignore[return-value]


def _exit_all(stack: list) -> None:
    for ctx in reversed(stack):
        ctx.__exit__(None, None, None)


def _run(coro):
    return asyncio.run(coro)


def _write_digest(tmp_path: Path, slug: str, age_sec: float) -> Path:
    d = tmp_path / "transcripts" / "digests"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{slug}.md"
    f.write_text("digest", encoding="utf-8")
    stamp = time.time() - age_sec
    import os

    os.utime(f, (stamp, stamp))
    return f


def test_missing_digest_runs(recording_id: str, tmp_path: Path) -> None:
    """No digests/<slug>.md → run_digest called with the tag, last_n=5
    and the transcripts root; the enrich stage stays done."""
    cfg = _cfg(tmp_path)
    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []
    stack = _happy_enrich([], graph)
    try:
        with (
            patch("worker.activities.cfg", return_value=cfg),
            patch(
                "worker.digest.run_digest",
                return_value={"written": True, "path": "/x.md"},
            ) as rd,
        ):
            result = _run(activities.enrich(recording_id))
    finally:
        _exit_all(stack)
    assert result["profile_id"] == "builtin-fallback"
    rd.assert_called_once()
    args = rd.call_args.args
    assert args[0] == "campaign"  # tag
    assert args[1] == 5  # last_n
    assert args[3] == cfg.vault.path  # transcripts root
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done


def test_stale_digest_runs(recording_id: str, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, window=3600)
    _write_digest(tmp_path, "campaign", age_sec=7200)  # older than window
    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []
    stack = _happy_enrich([], graph)
    try:
        with (
            patch("worker.activities.cfg", return_value=cfg),
            patch(
                "worker.digest.run_digest",
                return_value={"written": True, "path": "/x.md"},
            ) as rd,
        ):
            _run(activities.enrich(recording_id))
    finally:
        _exit_all(stack)
    rd.assert_called_once()


def test_fresh_digest_skipped(recording_id: str, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, window=3600)
    # Inside the window AND newer than the enrich start (mtime now —
    # enrich_started_at is captured milliseconds earlier).
    _write_digest(tmp_path, "campaign", age_sec=0)
    import os
    import time as _time

    note = tmp_path / "transcripts" / "digests" / "campaign.md"
    future = _time.time() + 60
    os.utime(note, (future, future))
    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []
    stack = _happy_enrich([], graph)
    try:
        with (
            patch("worker.activities.cfg", return_value=cfg),
            patch(
                "worker.digest.run_digest",
                return_value={"written": True, "path": "/x.md"},
            ) as rd,
        ):
            _run(activities.enrich(recording_id))
    finally:
        _exit_all(stack)
    rd.assert_not_called()


def test_auto_digest_false_never_runs(recording_id: str, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, auto_digest=False)
    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []
    stack = _happy_enrich([], graph)
    try:
        with (
            patch("worker.activities.cfg", return_value=cfg),
            patch(
                "worker.digest.run_digest",
                return_value={"written": True, "path": "/x.md"},
            ) as rd,
        ):
            _run(activities.enrich(recording_id))
    finally:
        _exit_all(stack)
    rd.assert_not_called()


def test_digest_failure_never_fails_enrich(recording_id: str, tmp_path: Path) -> None:
    """Best-effort: run_digest raising leaves the enrich stage done."""
    cfg = _cfg(tmp_path)
    graph = MagicMock()
    graph.events = []
    graph.entities = []
    graph.relations = []
    stack = _happy_enrich([], graph)
    try:
        with (
            patch("worker.activities.cfg", return_value=cfg),
            patch(
                "worker.digest.run_digest",
                side_effect=RuntimeError("llm down"),
            ),
        ):
            result = _run(activities.enrich(recording_id))
    finally:
        _exit_all(stack)
    assert result["profile_id"] == "builtin-fallback"
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done


def test_skipped_stage_never_digests(recording_id: str, tmp_path: Path) -> None:
    """A skipped enrich (graph off) must never reach the digest call —
    the check happens only on the success path after set_stage(done)."""
    cfg = _cfg(tmp_path)
    cfg.graph.enabled = False
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch(
            "worker.digest.run_digest",
            return_value={"written": True, "path": "/x.md"},
        ) as rd,
        pytest.raises(ApplicationError, match="graph disabled"),
    ):
        _run(activities.enrich(recording_id))
    # Phase 3-F F2: skips raise; the digest call is still never reached.
    rd.assert_not_called()
