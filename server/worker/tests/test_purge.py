"""purge_tag_memory: the five-store wipe (graph namespace, graph_edits,
digest note, semantic index, per-recording events.json of single-tag
recordings) + the rebuild-id listing activity contract.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from worker.purge import purge_tag_memory


class _Cfg:
    """Minimal config shape the purge reads (graph + vault)."""

    class graph:
        uri = "bolt://x:7687"
        user = "neo4j"
        password_env = "NEO4J_PASSWORD"
        database = "neo4j"

    class vault:
        path = Path("/tmp/does-not-matter")

    recordings_root = Path("/tmp/does-not-matter-storage")


def _graph_driver(batch_deletes: list[int], tag: str = "quest"):
    """Driver mock whose session.run(_PURGE_CYPHER) returns successive
    counts (one .single() per batch call)."""
    driver = MagicMock()
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    calls = iter(batch_deletes)

    def run(query, **kwargs):
        assert "DETACH DELETE" in query
        assert kwargs == {"tag": tag, "limit": 10000}
        remaining = next(calls)
        return MagicMock(single=MagicMock(return_value={"deleted": remaining}))

    session.run = run
    driver.session.return_value = session
    return driver


def test_purges_all_five_stores(tmp_path, monkeypatch):
    cfg = _Cfg()
    cfg.vault.path = tmp_path
    storage = tmp_path / "storage" / "recordings"
    cfg.recordings_root = storage
    # graph: 10k batch + partial batch + empty confirm call
    driver = _graph_driver([10000, 3500, 0])
    # digest note + index on disk
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "quest.md").write_text("---\ntag: quest\n---\nbody", encoding="utf-8")
    idx_dir = tmp_path / "indexes"
    idx_dir.mkdir()
    (idx_dir / "quest.sqlite").write_bytes(b"sqlite")

    # catalog: r1 = done single-tag (artifact wiped); r2 = foreign tag;
    # r3 = done but MULTI-tag (shared artifact stays); r4 = quest but
    # not done (pipeline may still be writing it).
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from worker.db import Base, GraphEdit, Recording, RecordingState

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Recording(id="r1", tags=["quest"], state=RecordingState.done))
        s.add(Recording(id="r2", tags=["other"], state=RecordingState.done))
        s.add(
            Recording(id="r3", tags=["quest", "other"], state=RecordingState.done)
        )
        s.add(Recording(id="r4", tags=["quest"]))
        s.add(GraphEdit(tag="quest", target="entity", op="update", obj_key="a"))
        s.add(GraphEdit(tag="other", target="entity", op="update", obj_key="b"))
        s.commit()
    monkeypatch.setattr("worker.purge.session", lambda: Session(engine))

    for rid in ("r1", "r3"):
        meta = storage / rid / "meta"
        meta.mkdir(parents=True)
        (meta / "events.json").write_text("{}", encoding="utf-8")

    with (
        patch("worker.purge.GraphDatabase.driver", return_value=driver) as gd,
    ):
        counts = purge_tag_memory(cfg, "quest")

    gd.assert_called_once_with("bolt://x:7687", auth=("neo4j", ""))
    assert counts["graph_nodes"] == 13500
    assert counts["graph_edits"] == 1  # only the quest row
    assert counts["digest_files"] == 1
    assert counts["index_files"] == 1
    assert counts["events_json"] == 1  # r1 only (r3 is multi-tag)
    assert not (digests / "quest.md").exists()
    assert not (idx_dir / "quest.sqlite").exists()
    assert not (storage / "r1" / "meta" / "events.json").exists()
    assert (storage / "r3" / "meta" / "events.json").exists()  # shared

    with Session(engine) as s:
        remaining = [e.tag for e in s.query(GraphEdit).all()]
    assert remaining == ["other"]


def test_idempotent_on_empty_stores(tmp_path, monkeypatch):
    cfg = _Cfg()
    cfg.vault.path = tmp_path / "fresh"  # nothing exists
    driver = _graph_driver([0], tag="ghost")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from worker.db import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("worker.purge.session", lambda: Session(engine))

    with patch("worker.purge.GraphDatabase.driver", return_value=driver):
        counts = purge_tag_memory(cfg, "ghost")

    assert counts == {
        "graph_nodes": 0,
        "graph_edits": 0,
        "digest_files": 0,
        "index_files": 0,
        "events_json": 0,
    }


def test_digest_collision_variant_removed(tmp_path, monkeypatch):
    """A -N disambiguated digest file (slug collision with another tag)
    is found via frontmatter matching and removed too."""
    cfg = _Cfg()
    cfg.vault.path = tmp_path
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "quest-2.md").write_text("---\ntag: quest\n---\nbody", encoding="utf-8")
    (digests / "other.md").write_text("---\ntag: other\n---\nbody", encoding="utf-8")

    driver = _graph_driver([0])
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from worker.db import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("worker.purge.session", lambda: Session(engine))

    with patch("worker.purge.GraphDatabase.driver", return_value=driver):
        counts = purge_tag_memory(cfg, "quest")

    assert counts["digest_files"] == 1
    assert not (digests / "quest-2.md").exists()
    assert (digests / "other.md").exists()  # foreign digest untouched
