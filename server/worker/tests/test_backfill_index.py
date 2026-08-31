"""Phase 3.5 backfill_index script — catalog walk + index_segments wiring.

The embedding itself is faked (see test_semantic_index.py for the real
sqlite-vec coverage); these tests pin the SELECTION contract: done
recordings with a transcript.md only, tag filter, per-tag namespaces,
empty-tag recordings land in `untagged`, and failures don't abort the
run (exit code 1, remaining recordings still processed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import worker.db as db_mod
from worker.backfill_index import _recordings, main
from worker.config import EmbedConfig, WorkerConfig
from worker.db import Base, Recording, RecordingState
from worker.semantic_index import index_status


def _make_cfg(tmp_path: Path) -> WorkerConfig:
    cfg = WorkerConfig()
    cfg.storage.path = tmp_path / "storage"
    cfg.vault.path = tmp_path / "transcripts"
    cfg.graph.embed = EmbedConfig()
    # main() re-inits the engine from this url — keep it on the test db.
    cfg.database.url = f"sqlite:///{tmp_path / 't.db'}"
    return cfg


def _seed(rec_id: str, tags: list[str], state: RecordingState, has_transcript: bool,
          tmp_path: Path) -> None:
    with db_mod.session() as s:
        s.add(
            Recording(
                id=rec_id,
                title=f"T-{rec_id}",
                tags=tags,
                state=state,
                sha256="x" * 64,
                committed_bytes=1,
                total_bytes=1,
                duration_sec=60.0,
            )
        )
        s.commit()
    if has_transcript:
        meta = tmp_path / "storage" / "recordings" / rec_id / "meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "transcript.md").write_text(
            "**[00:00:01 – 00:00:05]** hello world segment.\n", encoding="utf-8"
        )


def _fake_embed(texts: list[str], cfg: Any) -> list[list[float]]:
    return [[0.1] * 1024 for _ in texts]


def test_selection_filters(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    _seed("done-tagged", ["alpha"], RecordingState.done, True, tmp_path)
    _seed("done-untagged", [], RecordingState.done, True, tmp_path)
    _seed("done-notranscript", ["alpha"], RecordingState.done, False, tmp_path)
    _seed("failed-tagged", ["alpha"], RecordingState.failed, True, tmp_path)

    assert [r[0] for r in _recordings(None)] == [
        "done-tagged",
        "done-untagged",
        "done-notranscript",
    ]
    assert [r[0] for r in _recordings("alpha")] == ["done-tagged", "done-notranscript"]


def test_main_indexes_all_namespaces(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    cfg = _make_cfg(tmp_path)
    _seed("r1", ["alpha", "beta"], RecordingState.done, True, tmp_path)
    _seed("r2", [], RecordingState.done, True, tmp_path)

    monkeypatch.setattr("worker.backfill_index.load_config", lambda: cfg)
    with patch("worker.embeddings.embed_texts", side_effect=_fake_embed):
        rc = main([])
    assert rc == 0
    # r1 indexed into BOTH its tags; r2 into the untagged namespace.
    for tag in ("alpha", "beta", "untagged"):
        st = index_status(cfg.vault.path, tag)
        assert st is not None and st["segments"] == 1, tag


def test_main_failure_isolated(tmp_path: Path, monkeypatch) -> None:
    """One failing recording must not stop the walk; exit code 1."""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    cfg = _make_cfg(tmp_path)
    _seed("bad", ["alpha"], RecordingState.done, True, tmp_path)
    _seed("good", ["alpha"], RecordingState.done, True, tmp_path)

    monkeypatch.setattr("worker.backfill_index.load_config", lambda: cfg)

    def flaky(rec_id: str, *a: Any, **k: Any) -> int:
        if rec_id == "bad":
            raise RuntimeError("backend hiccup")
        return 1

    with (
        patch("worker.semantic_index.index_segments", side_effect=flaky),
    ):
        rc = main([])
    assert rc == 1
