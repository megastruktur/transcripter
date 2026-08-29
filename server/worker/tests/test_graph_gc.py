"""Phase 1 graph GC: activity + module unit tests.

Covers: graph-disabled skip, deleted-count against a fake Neo4j driver,
batch loop termination, and workflow/activity registration parity.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker import main as main_mod
from worker.db import Base, Recording, RecordingState, session
from worker.graph_gc import run_graph_gc
from worker.workflows import GraphGc


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)


def _cfg(graph_enabled: bool) -> Any:
    cfg = MagicMock()
    cfg.graph.enabled = graph_enabled
    cfg.graph.uri = "bolt://n" if graph_enabled else ""
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    return cfg


def _seed(ids: list[str]) -> None:
    for rid in ids:
        with session() as s:
            s.add(
                Recording(
                    id=rid,
                    title="t",
                    tags=[],
                    state=RecordingState.done,
                    sha256="x" * 64,
                )
            )
            s.commit()


class _FakeResult:
    """Minimal neo4j result stand-in: result.single()["deleted"] -> n."""

    def __init__(self, deleted: int) -> None:
        self._deleted = deleted

    def single(self, strict: bool = False) -> dict:
        return {"deleted": self._deleted}


def _fake_driver(deleted_per_run: list[int]) -> tuple[Any, list[dict]]:
    """Mock neo4j driver whose session.run reports the given per-run counts."""
    runs: list[dict] = []
    results = iter(deleted_per_run)

    def run(_query: str, **params: Any) -> Any:
        runs.append(params)
        return _FakeResult(next(results, 0))

    session_like = MagicMock()
    session_like.run = run
    driver = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_like)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    driver.close = MagicMock()
    return driver, runs


def test_graph_gc_skipped_when_graph_disabled() -> None:
    result = run_graph_gc(_cfg(graph_enabled=False))
    assert result == {"skipped": "graph disabled"}


def test_graph_gc_deletes_stale_nodes() -> None:
    _seed(["keep-1", "keep-2"])
    driver, runs = _fake_driver(deleted_per_run=[5])
    with (
        patch("worker.graph_gc.GraphDatabase.driver", return_value=driver),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = run_graph_gc(_cfg(graph_enabled=True))
    # Phase 3.5: the payload also reports dropped index files.
    assert result == {"deleted": 5, "dropped_indexes": 0}
    driver.close.assert_called_once()
    # The sweep received the LIVE catalog ids as the keep-list.
    assert runs[0]["ids"] == ["keep-1", "keep-2"]


def test_graph_gc_sweeps_query_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Cypher must guard on origin_recording_id being absent from the
    catalog and must DETACH DELETE (orphaned events carry edges)."""
    _seed(["a"])
    driver, _runs = _fake_driver(deleted_per_run=[0])
    captured: dict[str, Any] = {}

    def run(query: str, **params: Any) -> Any:
        captured["query"] = query
        return MagicMock(single=MagicMock(return_value={"deleted": 0}))

    driver.session.return_value.__enter__.return_value.run = run
    with (
        patch("worker.graph_gc.GraphDatabase.driver", return_value=driver),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        # Phase 3.5: payload carries the index-file drop count too.
            assert run_graph_gc(_cfg(graph_enabled=True)) == {
                "deleted": 0,
                "dropped_indexes": 0,
            }
    assert "origin_recording_id IS NOT NULL" in captured["query"]
    assert "DETACH DELETE" in captured["query"]
    assert "$ids" in captured["query"]


def test_graph_gc_activity_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Temporal activity returns the skip payload when graph is off."""
    from unittest.mock import patch as _p

    with _p.object(activities, "_cfg", _cfg(graph_enabled=False)):
        result = asyncio.run(activities.graph_gc({}))
    assert result == {"skipped": "graph disabled"}


def test_graph_gc_activity_returns_count(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch.object(activities, "_cfg", _cfg(graph_enabled=True)), patch(
        "worker.activities.run_graph_gc_impl", return_value={"deleted": 3}
    ) as impl:
        result = asyncio.run(activities.graph_gc({}))
    assert result == {"deleted": 3}
    impl.assert_called_once()


def test_graph_gc_workflow_and_activity_registered() -> None:
    """GraphGc workflow exists and the graph_gc activity is registered in
    main.ACTIVITIES (the parity test covers the full set; this restates
    the workflow-level binding so a refactor can't quietly drop it)."""
    assert GraphGc is not None
    assert "run" in dir(GraphGc)
    assert "graph_gc" in {fn.__name__ for fn in main_mod.ACTIVITIES}
    # The workflow actually executes the graph_gc activity.
    import inspect

    src = inspect.getsource(GraphGc.run)
    assert '"graph_gc"' in src
