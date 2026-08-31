"""Phase 3.75 API surface: GET /search (global cross-tag).

Contract: the query is embedded through the configured backend and
KNN'd over EVERY worker-built per-tag index under
``<transcripts>/indexes/``, merged by distance ascending. Response:
``{query, k, hits: [{tag, recording_id, session_title, ts_start,
ts_end, speaker, snippet, distance}]}`` capped at k hits.

- ``tag`` is the index filename slug (the worker's Unicode tag slug —
  files carry no raw-tag column);
- 503 ``{available: false, reason}`` — same shape as the tag search —
  when the embedding backend is unavailable or NO index files exist;
- per-tag meta mismatch or a corrupt/unreadable file skips THAT tag
  with a warning, never a 500 — one bad index must not kill the search;
- ``q`` non-empty, ``k`` 1..50 (422, like the tag search).
"""

from __future__ import annotations

import importlib
import sqlite3
import struct
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

# Deterministic fake query vector — dimension must match the index files
# these tests build (8). Never touches a real model.
_DIM = 8
_QVEC = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
_META = {"backend": "local", "model": "onnx:bge-m3-int8", "dimensions": str(_DIM)}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    from app import temporal_client

    monkeypatch.setattr(temporal_client, "start_pipeline", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "regenerate_stage", AsyncMock(return_value="wf-regen"))
    monkeypatch.setattr(temporal_client, "start_export", AsyncMock(return_value="wf-export"))

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _build_index(
    transcripts: Path,
    slug: str,
    *,
    rows: list[tuple[str, str, float, float, str, str, list[float]]],
    meta: dict[str, str] | None = None,
) -> Path:
    """Write a REAL vec0 index the way worker.semantic_index does (same
    schema + meta) so the route's read path is exercised for real."""
    import sqlite_vec

    idx = transcripts / "indexes"
    idx.mkdir(parents=True, exist_ok=True)
    path = idx / f"{slug}.sqlite"
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute(f"CREATE VIRTUAL TABLE segments USING vec0(embedding float[{_DIM}])")
    db.execute(
        "CREATE TABLE segments_meta (recording_id TEXT NOT NULL, "
        "session_title TEXT NOT NULL DEFAULT '', ts_start REAL NOT NULL, "
        "ts_end REAL NOT NULL, speaker TEXT NOT NULL DEFAULT '', "
        "text TEXT NOT NULL, indexed_at TEXT NOT NULL)"
    )
    db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for rid, title, ts0, ts1, speaker, text, vec in rows:
        cur = db.execute(
            "INSERT INTO segments_meta (recording_id, session_title, ts_start, "
            "ts_end, speaker, text, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, title, ts0, ts1, speaker, text, "2026-08-29T00:00:00"),
        )
        blob = struct.pack(f"{len(vec)}f", *vec)
        db.execute(
            "INSERT INTO segments (rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, blob),
        )
    db.executemany(
        "INSERT INTO index_meta (key, value) VALUES (?, ?)",
        (meta or _META).items(),
    )
    db.commit()
    db.close()
    return path


@pytest.fixture()
def search_env(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Two tags (`quest` near, `goblin-far` far) + a mismatched third
    (`stale-model`) that must be skipped by the meta guard. The embed
    client is monkeypatched to return the deterministic query vec, and
    expected_index_meta is pinned to the indexes' meta."""

    transcripts = tmp_path / "transcripts"
    monkeypatch.setattr(client.app.state.config.vault, "path", transcripts)

    near = list(_QVEC)  # == query → distance ~0
    far = [-0.8, -0.7, -0.6, -0.5, -0.4, -0.3, -0.2, -0.1]
    _build_index(
        transcripts,
        "quest",
        rows=[("rid-a", "Boss fight", 65.0, 71.0, "spk_1", "Галахад бросает вызов", near)],
    )
    _build_index(
        transcripts,
        "goblin-far",
        rows=[("rid-b", "Camp watch", 10.0, 20.0, "spk_0", "filler talk", far)],
    )
    _build_index(
        transcripts,
        "stale-model",
        rows=[("rid-c", "Old model session", 1.0, 2.0, "spk_0", "stale text", near)],
        meta={"backend": "local", "model": "onnx:older-model", "dimensions": str(_DIM)},
    )

    from app.routes import search as search_route

    monkeypatch.setattr(search_route, "embed_query", lambda q, cfg: list(_QVEC))
    monkeypatch.setattr(search_route, "expected_index_meta", lambda cfg: dict(_META))
    return {"transcripts": transcripts}


# ---------- happy path ----------


def test_search_merges_tags_by_distance(client: TestClient, search_env) -> None:
    """The near hit (quest) outranks the far one (goblin-far) regardless
    of which file it came from; each hit names its source tag."""
    r = client.get("/search", params={"q": "вызов"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "вызов"
    assert body["k"] == 20
    (first, second) = body["hits"]
    assert first["tag"] == "quest"
    assert first["recording_id"] == "rid-a"
    assert first["session_title"] == "Boss fight"
    assert first["ts_start"] == 65.0
    assert first["ts_end"] == 71.0
    assert first["speaker"] == "spk_1"
    assert first["snippet"] == "Галахад бросает вызов"
    assert first["distance"] == pytest.approx(0.0, abs=1e-5)
    assert second["tag"] == "goblin-far"
    assert first["distance"] < second["distance"]


def test_search_meta_mismatch_tag_skipped_not_500(client: TestClient, search_env) -> None:
    """stale-model's meta does not match the active config → that tag is
    silently dropped from the union; the rest still answers 200."""
    body = client.get("/search", params={"q": "x"}).json()
    tags = {h["tag"] for h in body["hits"]}
    assert "stale-model" not in tags
    assert "quest" in tags


def test_search_k_caps_merged_hits(client: TestClient, search_env) -> None:
    """k applies to the MERGED list, not per file."""
    body = client.get("/search", params={"q": "x", "k": 1}).json()
    assert len(body["hits"]) == 1
    assert body["k"] == 1


# ---------- 503s ----------


def test_search_backend_unavailable_503(client: TestClient, search_env, monkeypatch) -> None:
    from app.routes import search as search_route

    monkeypatch.setattr(search_route, "embed_query", lambda q, cfg: None)
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["available"] is False
    assert "embedding backend unavailable" in detail["reason"]


def test_search_backend_raises_503(client: TestClient, search_env, monkeypatch) -> None:
    from app.routes import search as search_route

    def boom(q, cfg):
        raise RuntimeError("http backend down")

    monkeypatch.setattr(search_route, "embed_query", boom)
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 503
    assert r.json()["detail"]["available"] is False


def test_search_no_indexes_503(client: TestClient, search_env) -> None:
    """Empty vault (no index files at all) → 503 {available: false},
    same shape as the tag search, with a backfill hint."""
    (search_env["transcripts"] / "indexes").mkdir(parents=True, exist_ok=True)
    for f in (search_env["transcripts"] / "indexes").glob("*.sqlite"):
        f.unlink()
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["available"] is False
    assert "backfill_index" in detail["reason"]


# ---------- corrupt files skip, never 500 ----------


def test_search_corrupt_file_skips_tag(client: TestClient, search_env) -> None:
    """A garbage .sqlite in the indexes dir is skipped with a warning;
    the healthy tags still answer."""
    bad = search_env["transcripts"] / "indexes" / "corrupt.sqlite"
    bad.write_bytes(b"this is not a database")
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 200
    tags = {h["tag"] for h in r.json()["hits"]}
    assert "corrupt" not in tags
    assert "quest" in tags


def test_search_empty_file_skips_tag(client: TestClient, search_env) -> None:
    (search_env["transcripts"] / "indexes" / "empty.sqlite").touch()
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 200
    assert "empty" not in {h["tag"] for h in r.json()["hits"]}


def test_search_vec0_missing_skips_tag(client: TestClient, search_env) -> None:
    """A plain sqlite file WITH a meta table but no vec0 segments is a
    truncated index: meta guard passes, KNN raises → skip, not 500."""
    path = search_env["transcripts"] / "indexes" / "truncated.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.executemany("INSERT INTO index_meta (key, value) VALUES (?, ?)", _META.items())
    db.commit()
    db.close()
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 200
    assert "truncated" not in {h["tag"] for h in r.json()["hits"]}


def test_search_all_corrupt_still_200_with_empty_hits(client: TestClient, search_env) -> None:
    """Every index broken → the union is empty but the search itself
    succeeded (200 + no hits), not a 503/500."""
    idx = search_env["transcripts"] / "indexes"
    for f in idx.glob("*.sqlite"):
        f.write_bytes(b"garbage")
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 200
    assert r.json()["hits"] == []


# ---------- validation ----------


def test_search_query_validation(client: TestClient, search_env) -> None:
    assert client.get("/search").status_code == 422  # q required
    assert client.get("/search", params={"q": ""}).status_code == 422
    assert client.get("/search", params={"q": "x", "k": 0}).status_code == 422
    assert client.get("/search", params={"q": "x", "k": 51}).status_code == 422


def test_search_requires_auth(client: TestClient, search_env) -> None:
    r = client.get("/search", params={"q": "x"}, headers={"authorization": ""})
    assert r.status_code in (401, 403)


# ---------- reader helpers (no mocks on the read side) ----------


def test_iter_indexes_sorted_and_slugged(search_env) -> None:
    from app.semantic_index import iter_indexes, read_index_meta

    entries = iter_indexes(search_env["transcripts"])
    slugs = [s for s, _ in entries]
    assert slugs == sorted(slugs)
    assert set(slugs) >= {"quest", "goblin-far", "stale-model"}
    quest = next(p for s, p in entries if s == "quest")
    assert read_index_meta(quest) == _META


def test_read_index_meta_garbage_returns_empty(tmp_path: Path) -> None:
    from app.semantic_index import read_index_meta

    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database")
    assert read_index_meta(bad) == {}
