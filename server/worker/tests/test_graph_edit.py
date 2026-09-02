"""Phase A worker unit tests: graph_edit updaters.

Neo4j driver mocked at GraphDatabase.driver (write_to_graph test
pattern); events.json files are REAL under tmp_path so the
read-modify-write path (atomic replace, both copies) is exercised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from worker.graph_edit import (
    VaultPaths,
    _events_with_keys,
    apply_event_delete,
    apply_event_update,
    compute_event_key,
    reapply_overlay,
    rewrite_events_json,
    similarity,
)


class _Cfg:
    """Minimal cfg stand-in: graph coordinates + storage/vault roots."""

    class graph:
        uri = "bolt://n"
        user = "neo4j"
        password_env = "NEO4J_PASSWORD"
        database = "neo4j"

    def __init__(self, recordings_root: Path, vault_root: Path):
        self.recordings_root = recordings_root
        self.vault = MagicMock()
        self.vault.path = vault_root


def _paths(tmp_path: Path, vault_folders: list[Path] | None = None) -> VaultPaths:
    return VaultPaths(
        recordings_root=tmp_path / "recordings",
        vault_root=tmp_path / "vault",
        vault_folders=vault_folders or [],
    )


def _write_doc(root: Path, rid: str, doc: dict) -> Path:
    meta = root / rid / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    path = meta / "events.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _read_doc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class _Row(dict):
    """A dict that also answers .single() — what _find_event_node uses."""

    def single(self):
        return self


class _Iter(list):
    """A list that session.run(...) returns for multi-row queries."""

    def __iter__(self):
        return super().__iter__()


# ---------- pure helpers ----------


def test_compute_event_key_deterministic_and_suffixed() -> None:
    a = compute_event_key("r1", "00:01", "note", "hello")
    assert a == compute_event_key("r1", "00:01", "note", "hello")
    assert a != compute_event_key("r2", "00:01", "note", "hello")
    assert compute_event_key("r1", "00:01", "note", "hello", occurrence=2) == f"{a}-2"


def test_similarity_orders_reworded_summaries() -> None:
    hi = similarity("Glennis built the agent network", "Glennis created the agent network")
    lo = similarity("Glennis built the agent network", "The party looted the castle")
    assert hi > lo


def _mock_driver(rows_by_query: Any = None, single_row: dict | None = None):
    """Driver whose session.run returns ``single_row`` for .single()
    lookups and iterates ``rows_by_query`` otherwise. The driver's
    context manager returns ITSELF (production: ``with _driver(cfg) as
    driver`` — a bare MagicMock would return an unconfigured child)."""
    driver = MagicMock()
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)

    def run(q=None, *, query=None, **params):
        q = query if q is None else q
        if "$key" in q and "event_key" in q and "LIMIT 1" in q and single_row is not None:
            return _Row(single_row)
        if rows_by_query is not None and "origin_recording_id" in q:
            return _Iter(rows_by_query)
        return _Row({"n": 0})

    session.run = MagicMock(side_effect=run)
    driver.session = MagicMock(return_value=session)
    driver.__enter__ = MagicMock(return_value=driver)
    driver.__exit__ = MagicMock(return_value=False)
    return driver, session


def test_rewrite_updates_both_copies_atomically(tmp_path: Path) -> None:
    rid = "rec-1"
    doc = {
        "events": [{"event_key": "k1", "ts": "t", "kind": "k", "summary": "old", "mentions": []}]
    }
    storage = _write_doc(tmp_path / "recordings", rid, doc)
    vault_folder = tmp_path / "vault" / "2026" / "09" / "folder 00000000"
    mirror_meta = vault_folder / ".transcripter" / "meta"
    mirror_meta.mkdir(parents=True, exist_ok=True)
    mirror_path = mirror_meta / "events.json"
    mirror_path.write_text(json.dumps(doc), encoding="utf-8")

    def mutate(d: dict) -> bool:
        for ev in d.get("events", []):
            if ev.get("event_key") == "k1":
                ev["summary"] = "new"
                return True
        return False

    changed = rewrite_events_json(rid, _paths(tmp_path, [vault_folder]), mutate)
    assert changed
    assert _read_doc(storage)["events"][0]["summary"] == "new"
    assert _read_doc(mirror_path)["events"][0]["summary"] == "new"
    assert not list((tmp_path / "recordings" / rid / "meta").glob("*.tmp"))


def test_events_with_keys_backfills_legacy_and_dedupes() -> None:
    events = [
        {"ts": "t", "kind": "k", "summary": "s", "mentions": []},
        {"ts": "t", "kind": "k", "summary": "s", "mentions": []},
    ]
    out = _events_with_keys("r", events)
    assert out[0]["event_key"] == compute_event_key("r", "t", "k", "s")
    assert out[1]["event_key"] == f"{compute_event_key('r', 't', 'k', 's')}-1"


def test_rewrite_skips_when_mutator_finds_nothing(tmp_path: Path) -> None:
    rid = "rec-2"
    path = _write_doc(tmp_path / "recordings", rid, {"events": []})
    before = path.read_text(encoding="utf-8")
    assert not rewrite_events_json(rid, _paths(tmp_path), lambda d: False)
    assert path.read_text(encoding="utf-8") == before


# ---------- apply_event_update / delete (mocked driver, real files) ----------


def test_apply_event_update_patches_graph_and_artifact(tmp_path: Path) -> None:
    cfg = _Cfg(tmp_path / "recordings", tmp_path / "vault")
    rid = "rec-3"
    doc = {
        "events": [
            {
                "event_key": "k9",
                "ts": "00:10",
                "kind": "note",
                "summary": "Glennis built it",
                "mentions": [],
            }
        ]
    }
    path = _write_doc(tmp_path / "recordings", rid, doc)
    driver, session = _mock_driver(
        single_row={
            "id": "elem-1",
            "ts": "00:10",
            "kind": "note",
            "summary": "Glennis built it",
            "origin": rid,
            "title": "t",
        }
    )
    with patch("worker.graph_edit.GraphDatabase.driver", return_value=driver):
        out = apply_event_update(
            cfg,
            _paths(tmp_path),
            "quest",
            "k9",
            {"summary": "The operator built it"},
            {
                "origin_recording_id": rid,
                "kind": "note",
                "ts": "00:10",
                "before_summary": "Glennis built it",
            },
        )
    assert out["ok"] is True
    assert _read_doc(path)["events"][0]["summary"] == "The operator built it"

    def _query_text(c) -> str:
        return c.args[0] if c.args else c.kwargs.get("query", "")

    set_calls = [c for c in session.run.call_args_list if "SET" in _query_text(c)]
    assert set_calls and "$summary" in _query_text(set_calls[0])


def test_apply_event_delete_removes_from_graph_and_artifact(tmp_path: Path) -> None:
    cfg = _Cfg(tmp_path / "recordings", tmp_path / "vault")
    rid = "rec-4"
    doc = {
        "events": [
            {"event_key": "keep", "ts": "t1", "kind": "k", "summary": "keep me", "mentions": []},
            {"event_key": "kill", "ts": "t2", "kind": "k", "summary": "wrong fact", "mentions": []},
        ]
    }
    path = _write_doc(tmp_path / "recordings", rid, doc)
    driver, _ = _mock_driver(
        single_row={
            "id": "elem-2",
            "ts": "t2",
            "kind": "k",
            "summary": "wrong fact",
            "origin": rid,
            "title": "t",
        }
    )
    with patch("worker.graph_edit.GraphDatabase.driver", return_value=driver):
        out = apply_event_delete(cfg, _paths(tmp_path), "quest", "kill")
    assert out["ok"] is True
    events = _read_doc(path)["events"]
    assert [e["event_key"] for e in events] == ["keep"]


# ---------- reapply_overlay: re-anchor + orphan ----------


class _EditRow:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.tag = kw.get("tag", "quest")
        self.target = kw.get("target")
        self.op = kw.get("op")
        self.obj_key = kw.get("obj_key", "")
        self.before = kw.get("before", {})
        self.after = kw.get("after", {})
        self.anchor = kw.get("anchor", {})
        self.status = kw.get("status")


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Stand-in for worker.db.session() + SQLAlchemy Session."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, *_a, **_k):
        return _Query(self._rows)

    def get(self, _cls, _id):
        return next((r for r in self._rows if r.id == _id), None)

    def commit(self):
        pass


def _setup_overlay(monkeypatch, ge, tmp_path: Path, edit_rows: list) -> None:
    """Patch the db session + vault paths reapply_overlay depends on."""
    monkeypatch.setattr("worker.db._SessionLocal", _FakeSessionFactory(edit_rows), raising=False)
    monkeypatch.setattr(ge, "vault_paths_for", lambda c, r: _paths(tmp_path), raising=False)


class _FakeSessionFactory:
    """session() replacement: returns a context-managed fake bound to
    the given rows."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return _FakeSession(self.rows)


def test_reapply_overlay_reanchors_similar_event(tmp_path: Path, monkeypatch) -> None:
    """After a regenerate reworded the summary, the stored edit
    re-anchors onto the fresh node (similarity gate) and re-applies."""
    import worker.graph_edit as ge
    from worker.db import EditOp, EditStatus, EditTarget

    cfg = _Cfg(tmp_path / "recordings", tmp_path / "vault")
    rid = "rec-5"
    doc = {
        "events": [
            {
                "event_key": "fresh1",
                "ts": "00:10",
                "kind": "note",
                "summary": "Glennis created the agent network",
                "mentions": [],
            }
        ]
    }
    _write_doc(tmp_path / "recordings", rid, doc)

    edit = _EditRow(
        id=7,
        target=EditTarget.event,
        op=EditOp.update,
        obj_key="oldkey",
        after={"summary": "The operator created the agent network"},
        anchor={
            "origin_recording_id": rid,
            "kind": "note",
            "before_summary": "Glennis built the agent network",
        },
        status=EditStatus.applied,
    )
    # session lives in worker.db; reapply_overlay imports it lazily.
    monkeypatch.setattr("worker.db.session", _FakeSessionFactory([edit]), raising=False)
    monkeypatch.setattr(ge, "vault_paths_for", lambda c, r: _paths(tmp_path), raising=False)

    fresh_row = {
        "id": "elem-fresh",
        "tag": "quest",
        "event_key": "fresh1",
        "ts": "00:10",
        "kind": "note",
        "summary": "Glennis created the agent network",
    }
    driver, _ = _mock_driver(rows_by_query=[fresh_row])
    rekeyed: list = []
    with (
        patch("worker.graph_edit.GraphDatabase.driver", return_value=driver),
        patch(
            "worker.graph_edit._rekey_edit",
            lambda eid, key, summ: rekeyed.append((eid, key)),
        ),
    ):
        counts = reapply_overlay(cfg, "quest", rid)
    assert counts["reanchored"] == 1
    path = tmp_path / "recordings" / rid / "meta" / "events.json"
    assert _read_doc(path)["events"][0]["summary"] == "The operator created the agent network"
    assert rekeyed == [(7, "fresh1")]


def test_reapply_overlay_orphans_unmatched_edit(tmp_path: Path, monkeypatch) -> None:
    """No fresh event resembles the anchor → the edit flips orphaned,
    nothing is written."""
    import worker.graph_edit as ge
    from worker.db import EditOp, EditStatus, EditTarget

    cfg = _Cfg(tmp_path / "recordings", tmp_path / "vault")
    rid = "rec-6"
    _write_doc(tmp_path / "recordings", rid, {"events": []})

    edit = _EditRow(
        id=9,
        target=EditTarget.event,
        op=EditOp.update,
        obj_key="gone",
        after={"summary": "x"},
        anchor={
            "origin_recording_id": rid,
            "kind": "note",
            "before_summary": "completely different topic",
        },
        status=EditStatus.applied,
    )
    monkeypatch.setattr("worker.db.session", _FakeSessionFactory([edit]), raising=False)
    monkeypatch.setattr(ge, "vault_paths_for", lambda c, r: _paths(tmp_path), raising=False)
    statuses: list = []
    monkeypatch.setattr(ge, "_set_edit_status", lambda eid, st: statuses.append((eid, st)))

    driver, _ = _mock_driver(rows_by_query=[])
    with patch("worker.graph_edit.GraphDatabase.driver", return_value=driver):
        counts = reapply_overlay(cfg, "quest", rid)
    assert counts["orphaned"] == 1
    assert statuses == [(9, EditStatus.orphaned)]
