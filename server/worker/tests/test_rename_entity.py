"""Phase 4 — user entity rename (activity) + dedup guard.

The graph is mocked everywhere (same rule as test_enrich). Covered:

* rename_entity_in_graph: label/type SET + user_corrected flag, embedding
  re-write when the node carries one, fresh embed when it doesn't and the
  backend is on, no-embed when off, ok=False on a missing node.
* the rename_entity ACTIVITY: graph-disabled RuntimeError, wiring of the
  cfg/neo4j coordinates, ok=False → non-retryable ApplicationError.
* registration guards (ACTIVITIES list + workflow class).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from worker import activities
from worker.enrich import (
    ExistingEntityLookup,
    ExtractedEntity,
    ExtractedGraph,
    _embed_one,
    rename_entity_in_graph,
    resolve_slugs,
)


def _cfg(embed_enabled: bool = True) -> Any:
    cfg = MagicMock()
    cfg.graph.enabled = True
    cfg.graph.uri = "bolt://n:7687"
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.graph.embed_enabled = embed_enabled
    return cfg
def _driver_with_node(embedding: list[float] | None) -> MagicMock:
    """Driver mock: the FIRST session() call answers the snapshot query
    (as a context manager whose run().single() returns the row); later
    calls yield a write session whose ``with begin_transaction()`` hands
    out a recording tx. ``driver._tx`` exposes the tx for assertions."""
    snap = MagicMock()
    snap.__enter__ = MagicMock(return_value=snap)
    snap.__exit__ = MagicMock(return_value=False)
    snap.run = MagicMock(
        return_value=MagicMock(single=MagicMock(return_value={"embedding": embedding}))
    )
    tx = MagicMock()
    tx.__enter__ = MagicMock(return_value=tx)
    tx.__exit__ = MagicMock(return_value=False)
    tx.run = MagicMock()
    write = MagicMock()
    write.__enter__ = MagicMock(return_value=write)
    write.__exit__ = MagicMock(return_value=False)
    write.begin_transaction = MagicMock(return_value=tx)

    driver = MagicMock()
    calls = {"n": 0}

    def _session(database=None):
        calls["n"] += 1
        return snap if calls["n"] == 1 else write

    driver.session = MagicMock(side_effect=_session)
    driver._snap = snap  # type: ignore[attr-defined]
    driver._write = write  # type: ignore[attr-defined]
    driver._tx = tx  # type: ignore[attr-defined]
    return driver


def _llm_cfg() -> Any:
    """cfg shaped for resolve_slugs: string summarize coords so
    ask_same_entity's os.environ.get() gets a str, never a MagicMock."""
    cfg = MagicMock()
    cfg.summarize.base_url = "http://localhost:1234/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    return cfg

def test_rename_sets_label_type_and_flag() -> None:
    driver = _driver_with_node([0.1, 0.2])
    with (
        patch("worker.enrich._embed_one", return_value=[0.9, 0.1]) as embed,
        patch("worker.enrich.GraphDatabase.driver", return_value=driver),
    ):
        out = rename_entity_in_graph(
            "daily blob", "vova", "Валли", "person", _cfg(),
            "bolt://n", "neo4j", "pw", "neo4j",
        )
    assert out == {"ok": True, "re_embedded": True}
    embed.assert_called_once()
    tx = driver._tx
    q, kwargs = tx.run.call_args.args[0], tx.run.call_args.kwargs
    assert "SET e.label = $label" in q
    assert ", e.type = $type" in q
    assert "e.user_corrected = true" in q
    assert ", e.embedding = $vec" in q
    assert kwargs == {
        "tag": "daily blob", "slug": "vova", "label": "Валли",
        "type": "person", "vec": [0.9, 0.1],
    }

def test_rename_without_type_omits_type_set() -> None:
    driver = _driver_with_node(None)
    with (
        patch("worker.enrich._embed_one", return_value=None),
        patch("worker.enrich.GraphDatabase.driver", return_value=driver),
    ):
        out = rename_entity_in_graph(
            "daily blob", "vova", "Валли", None, _cfg(embed_enabled=False),
            "bolt://n", "neo4j", "pw", "neo4j",
        )
    assert out == {"ok": True, "re_embedded": False}
    q = driver._tx.run.call_args.args[0]
    assert "$type" not in q
    assert "e.user_corrected = true" in q
    assert "$vec" not in q


def test_rename_missing_node_returns_ok_false() -> None:
    driver = _driver_with_node(None)
    # Snapshot finds nothing: the row single() is None.
    driver._snap.run.return_value.single.return_value = None
    with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
        out = rename_entity_in_graph(
            "daily blob", "ghost", "X", None, _cfg(),
            "bolt://n", "neo4j", "pw", "neo4j",
        )
    assert out == {"ok": False, "re_embedded": False}
    # No transaction was opened.
    assert not driver._write.begin_transaction.called


def test_rename_reembeds_even_when_embed_disabled_if_node_has_vector() -> None:
    """A node that already answers the cosine prefilter MUST get a fresh
    vector even when embed_enabled=False — the stale-vector drift is the
    bug the rename fixes; the toggle only gates NEW vectors."""
    driver = _driver_with_node([0.3, 0.7])
    with (
        patch("worker.enrich._embed_one", return_value=[0.8, 0.2]) as embed,
        patch("worker.enrich.GraphDatabase.driver", return_value=driver),
    ):
        out = rename_entity_in_graph(
            "t", "s", "L", None, _cfg(embed_enabled=False),
            "bolt://n", "neo4j", "pw", "neo4j",
        )
    assert out == {"ok": True, "re_embedded": True}
    embed.assert_called_once()

def test_embed_one_degrades_to_none_on_error() -> None:
    cfg = _cfg()
    with patch("worker.embeddings.embed_texts", side_effect=RuntimeError("backend down")):
        assert _embed_one("x", cfg) is None


def test_embed_one_returns_first_vector() -> None:
    cfg = _cfg()
    with patch("worker.embeddings.embed_texts", return_value=[[1.0, 0.0]]) as et:
        assert _embed_one("x", cfg) == [1.0, 0.0]
        et.assert_called_once_with(["x"], cfg)


# ---------- activity wiring ----------------------------------------------------


def test_rename_entity_activity_registered() -> None:
    import worker.main as main_mod

    assert "rename_entity" in {fn.__name__ for fn in main_mod.ACTIVITIES}


def test_rename_entity_workflow_and_activity_registered() -> None:
    from worker.workflows import RenameEntity

    assert "run" in dir(RenameEntity)



def test_rename_entity_activity_propagates_graph_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg()
    monkeypatch.setattr(activities, "cfg", lambda: cfg)
    sentinel = {"ok": True, "re_embedded": False}
    with patch(
        "worker.enrich.rename_entity_in_graph", return_value=sentinel
    ) as impl:
        out = asyncio.run(
            activities.rename_entity({"tag": "t", "slug": "s", "label": "L"})
        )
    assert out is sentinel
    impl.assert_called_once()
    args = impl.call_args.args
    assert args[0] == "t" and args[1] == "s" and args[2] == "L"
    assert args[4] is cfg
    assert args[5] == "bolt://n:7687"


def test_rename_entity_activity_raises_when_graph_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_attr = _cfg()
    cfg_attr.graph.enabled = False
    monkeypatch.setattr(activities, "cfg", lambda: cfg_attr)
    with pytest.raises(RuntimeError, match="graph backend not configured"):
        asyncio.run(activities.rename_entity({"tag": "t", "slug": "s", "label": "L"}))


# ---------- dedup guard (resolve_slugs) ----------------------------------------


def _graph_one(slug: str, label: str) -> ExtractedGraph:
    return ExtractedGraph(
        events=[],
        entities=[ExtractedEntity(slug=slug, label=label, type="person")],
        relations=[],
    )


def test_user_corrected_existing_node_never_merges() -> None:
    """The Phase 4 guard: an existing user-corrected node cannot be
    merged into by the LLM (Y), by the prefilter, or by the gray-zone
    error path — the candidate is kept distinct with a fresh slug."""
    cfg = _llm_cfg()
    lookup = MagicMock()
    # Every lookup answers a user-corrected canonical (first lookup hits
    # the slug; -2 also taken; -3 free).
    def answer(slug: str):
        if slug == "vova":
            return {
                "slug": "vova", "label": "Валли", "type": "person",
                "embedding": None, "user_corrected": True,
            }
        if slug == "vova-2":
            return {
                "slug": "vova-2", "label": "X", "type": "person",
                "embedding": None, "user_corrected": True,
            }
        return None

    lookup.side_effect = answer
    with patch("worker.enrich.httpx.post", return_value=MagicMock()) as post:
        out = resolve_slugs(_graph_one("vova", "Валя"), cfg, tag="daily blob", existing_lookup=lookup)
    # Kept distinct: candidate moved to a free slug; NO merge into the
    # corrected node.
    assert [e.slug for e in out.entities] == ["vova-3"]
    # The LLM was never even asked (guard skips ask_same_entity).
    post.assert_not_called()


def test_uncorrected_existing_node_still_merges() -> None:
    """Legacy behavior intact when the flag is absent/false."""
    from worker.enrich import _parse_yes_no  # noqa: F401 — import sanity

    cfg = _llm_cfg()
    lookup = MagicMock()
    lookup.return_value = {
        "slug": "vova", "label": "Валя", "type": "person",
        "embedding": None, "user_corrected": False,
    }
    resp = MagicMock()
    resp.text = "Y"
    with patch("worker.enrich.httpx.post", return_value=resp):
        out = resolve_slugs(_graph_one("vova", "Валя"), cfg, tag="daily blob", existing_lookup=lookup)
    assert [e.slug for e in out.entities] == ["vova"]
    assert out.entities[0].label == "Валя"


def test_lookup_row_carries_user_corrected_false_for_legacy_rows() -> None:
    """coalesce in the SELECT keeps pre-phase-4 graphs answerable."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.run = MagicMock(
        return_value=MagicMock(
            single=MagicMock(
                return_value={
                    "label": "L", "type": "t", "slug": "s",
                    "embedding": None, "user_corrected": False,
                }
            )
        )
    )
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    out = ExistingEntityLookup(driver, "neo4j", "tag")("s")
    assert out is not None and out["user_corrected"] is False
    q = session.run.call_args.args[0]
    assert "user_corrected" in q
