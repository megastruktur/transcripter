"""Phase 3.5 API surface: GET /tags/{tag}/search.

Contract: the query is embedded through the configured backend and KNN'd
over the worker-built per-tag sqlite-vec index
(``<transcripts>/indexes/<tag-slug>.sqlite``). Response:
``{tag, query, hits: [{recording_id, session_title, ts_start, ts_end,
speaker, snippet, distance}]}``.

- 404 when no DONE recording carries the tag (same rule as timeline);
- 503 ``{available: false, reason}`` when the embedding backend is
  unavailable (missing model / http backend down), the tag has no index
  yet, or the index meta {backend, model, dimensions} does not match the
  active config (model switch → run backfill);
- 400 for tags outside _TAG_RE; ``q`` non-empty, ``k`` 1..50.
"""

from __future__ import annotations

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


import importlib


def _force_state(rid: str, state: str) -> None:
    from app.db import Recording, RecordingState, get_session

    gen = get_session()
    s = next(gen)
    rec = s.get(Recording, rid)
    rec.state = RecordingState(state)
    s.commit()
    gen.close()


def _make_recording(client: TestClient, tags: list[str], title: str) -> str:
    return client.post("/recordings", json={"title": title, "tags": tags}).json()["id"]


def _build_index(
    transcripts: Path,
    tag: str,
    *,
    rows: list[tuple[str, str, float, float, str, str, list[float]]],
    meta: dict[str, str] | None = None,
) -> Path:
    """Write a REAL vec0 index the way worker.semantic_index does (same
    schema + meta) so the route's read path is exercised for real."""
    import sqlite_vec

    idx = transcripts / "indexes"
    idx.mkdir(parents=True, exist_ok=True)
    slug = tag.replace(" ", "-")
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
        (meta or {"backend": "local", "model": "onnx:bge-m3-int8", "dimensions": str(_DIM)}).items(),
    )
    db.commit()
    db.close()
    return path


@pytest.fixture()
def search_env(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Tag `quest` with one DONE recording + a matching 2-row index; the
    embed client is monkeypatched to return the deterministic query vec,
    and expected_index_meta is pinned to the index's meta."""

    rid = _make_recording(client, ["quest"], "Boss fight")
    _force_state(rid, "done")
    transcripts = tmp_path / "transcripts"
    monkeypatch.setattr(client.app.state.config.transcripts, "path", transcripts)

    near = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]  # == query → distance ~0
    far = [-0.8, -0.7, -0.6, -0.5, -0.4, -0.3, -0.2, -0.1]
    other = _make_recording(client, ["quest"], "Second session")
    _force_state(other, "done")
    _build_index(
        transcripts,
        "quest",
        rows=[
            (rid, "Boss fight", 65.0, 71.0, "spk_1", "Галахад бросает вызов", near),
            (other, "Second session", 0.0, 4.0, "spk_0", "unrelated filler", far),
        ],
    )

    from app.routes import tags as tags_route

    monkeypatch.setattr(tags_route, "embed_query", lambda q, cfg: list(_QVEC))
    monkeypatch.setattr(
        tags_route,
        "expected_index_meta",
        lambda cfg: {"backend": "local", "model": "onnx:bge-m3-int8", "dimensions": str(_DIM)},
    )
    return {"rid": rid, "other": other, "transcripts": transcripts}


# ---------- happy path ----------


def test_search_returns_nearest_hit_first(client: TestClient, search_env) -> None:
    r = client.get("/tags/quest/search", params={"q": "вызов"})
    assert r.status_code == 200
    body = r.json()
    assert body["tag"] == "quest"
    assert body["query"] == "вызов"
    (first, second) = body["hits"]
    assert first["recording_id"] == search_env["rid"]
    assert first["session_title"] == "Boss fight"
    assert first["ts_start"] == 65.0
    assert first["ts_end"] == 71.0
    assert first["speaker"] == "spk_1"
    assert first["snippet"] == "Галахад бросает вызов"
    assert first["distance"] == pytest.approx(0.0, abs=1e-5)
    assert second["recording_id"] == search_env["other"]
    assert first["distance"] < second["distance"]


def test_search_k_limits_hits(client: TestClient, search_env) -> None:
    body = client.get("/tags/quest/search", params={"q": "x", "k": 1}).json()
    assert len(body["hits"]) == 1


# ---------- 503s ----------


def test_search_backend_unavailable_503(client: TestClient, search_env, monkeypatch) -> None:
    from app.routes import tags as tags_route

    monkeypatch.setattr(tags_route, "embed_query", lambda q, cfg: None)
    r = client.get("/tags/quest/search", params={"q": "x"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["available"] is False
    assert "embedding backend unavailable" in detail["reason"]


def test_search_backend_raises_503(client: TestClient, search_env, monkeypatch) -> None:
    from app.routes import tags as tags_route

    def boom(q, cfg):
        raise RuntimeError("http backend down")

    monkeypatch.setattr(tags_route, "embed_query", boom)
    r = client.get("/tags/quest/search", params={"q": "x"})
    assert r.status_code == 503
    assert r.json()["detail"]["available"] is False


def test_search_no_index_503_with_backfill_hint(client: TestClient, search_env) -> None:
    """Tag exists (done recordings) but was never indexed."""
    rid = _make_recording(client, ["fresh"], "Fresh")
    _force_state(rid, "done")
    r = client.get("/tags/fresh/search", params={"q": "x"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["available"] is False
    assert "backfill_index" in detail["reason"]


def test_search_meta_mismatch_503(client: TestClient, search_env, monkeypatch) -> None:
    """Index built by a different model → 503 + re-index hint, never a
    cross-vector-space KNN."""
    from app.routes import tags as tags_route

    monkeypatch.setattr(
        tags_route,
        "expected_index_meta",
        lambda cfg: {"backend": "http", "model": "other-model", "dimensions": str(_DIM)},
    )
    r = client.get("/tags/quest/search", params={"q": "x"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["available"] is False
    assert "backfill_index" in detail["reason"]
    assert "bge-m3-int8" in detail["reason"]  # the index's model is echoed


def test_search_dimensions_mismatch_503(client: TestClient, search_env, monkeypatch) -> None:
    from app.routes import tags as tags_route

    monkeypatch.setattr(
        tags_route,
        "expected_index_meta",
        lambda cfg: {"backend": "local", "model": "onnx:bge-m3-int8", "dimensions": "1024"},
    )
    r = client.get("/tags/quest/search", params={"q": "x"})
    assert r.status_code == 503
    assert r.json()["detail"]["available"] is False


# ---------- 404 / 400 / validation ----------


def test_search_unknown_tag_404(client: TestClient, search_env) -> None:
    """No DONE recording carries the tag → unknown tag, same rule as
    timeline (an existing but unindexed tag is 503, not 404)."""
    r = client.get("/tags/no-such-tag/search", params={"q": "x"})
    assert r.status_code == 404


def test_search_uploads_do_not_make_tag_known(client: TestClient, search_env) -> None:
    _make_recording(client, ["uploading-tag"], "still uploading")
    r = client.get("/tags/uploading-tag/search", params={"q": "x"})
    assert r.status_code == 404


def test_search_bad_tag_400(client: TestClient, search_env) -> None:
    assert client.get("/tags/bad%21tag/search", params={"q": "x"}).status_code == 400
    assert client.get("/tags/%20%20/search", params={"q": "x"}).status_code == 400


def test_search_query_validation(client: TestClient, search_env) -> None:
    assert client.get("/tags/quest/search").status_code == 422  # q required
    assert client.get("/tags/quest/search", params={"q": ""}).status_code == 422
    assert client.get("/tags/quest/search", params={"q": "x", "k": 0}).status_code == 422
    assert client.get("/tags/quest/search", params={"q": "x", "k": 51}).status_code == 422


def test_search_tag_normalized_and_slug_path(
    client: TestClient, search_env, monkeypatch
) -> None:
    """Free-form tag with a space: normalization lowercases, the index
    filename comes from the Unicode slug (space → dash)."""
    rid = _make_recording(client, ["Dark Castle"], "Siege")
    _force_state(rid, "done")
    transcripts = search_env["transcripts"]
    _build_index(
        transcripts,
        "dark castle",
        rows=[
            (rid, "Siege", 10.0, 20.0, "spk_1", "siege text", list(_QVEC)),
        ],
    )
    body = client.get("/tags/Dark%20Castle/search", params={"q": "siege"}).json()
    assert body["tag"] == "dark castle"
    assert body["hits"][0]["snippet"] == "siege text"


# ---------- real sqlite-vec roundtrip (no mocks on the read side) ----------


def test_knn_read_side_matches_writer_schema(client: TestClient, search_env) -> None:
    """The api's own knn_search/index_status open the worker-written file
    shape read-only — guard the rowid-join + meta read against schema
    drift (the writer twin is worker/semantic_index.py)."""
    from app.semantic_index import index_status, knn_search

    status = index_status(search_env["transcripts"], "quest")
    assert status is not None
    assert status["segments"] == 2
    assert status["meta"]["backend"] == "local"

    hits = knn_search(search_env["transcripts"], "quest", list(_QVEC), k=2)
    assert [h["recording_id"] for h in hits] == [
        search_env["rid"],
        search_env["other"],
    ]
    assert hits[0]["distance"] < hits[1]["distance"]

    assert index_status(search_env["transcripts"], "missing-tag") is None
    assert knn_search(search_env["transcripts"], "missing-tag", list(_QVEC)) == []
