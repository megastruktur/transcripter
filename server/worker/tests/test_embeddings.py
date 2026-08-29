"""embeddings.py unit tests (Phase 2.5).

No real model in CI: onnxruntime/tokenizers are faked via sys.modules
inside Embedder load tests, and the process singleton is exercised
against a tmp model directory so the failure latch is observable. The
vector-math (three-zone decision) tests use plain numpy — the same
library the production path uses.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from worker.config import GraphConfig
from worker.embeddings import (
    Decision,
    Embedder,
    _embedder,
    embedder_reset_for_tests,
    ensure_vector_index,
    entity_vectors,
    same_entity_decision,
    similar_in_namespace,
)

# --- fakes for the deferred heavy imports -------------------------------------


class _FakeEncoding:
    def __init__(self, ids: list[int], mask: list[int]) -> None:
        self.ids = ids
        self.attention_mask = mask


class _FakeTokenizer:
    instances: ClassVar[list[_FakeTokenizer]] = []

    def __init__(self) -> None:
        self.padding = False
        self.truncation = False
        _FakeTokenizer.instances.append(self)

    @classmethod
    def from_file(cls, path: str) -> _FakeTokenizer:
        assert path.endswith("tokenizer.json")
        return cls()

    def enable_padding(self, **kw: Any) -> None:
        self.padding = True

    def enable_truncation(self, max_length: int) -> None:
        self.truncation = True
        self.max_length = max_length

    def encode_batch(self, texts: list[str]) -> list[_FakeEncoding]:
        return [
            _FakeEncoding([101, len(t) % 1000, 102], [1, 1, 1]) for t in texts
        ]


class _FakeSession:
    instances: ClassVar[list[_FakeSession]] = []

    def __init__(self, model_path: str, providers: list[str]) -> None:
        assert model_path.endswith("model_int8.onnx")
        assert providers == ["CPUExecutionProvider"]
        self.run_calls = 0
        _FakeSession.instances.append(self)

    def run(self, _out: Any, feed: dict[str, Any]) -> list[Any]:
        self.run_calls += 1
        ids = feed["input_ids"]
        # (n, seq, dim) hidden states with the real 1024-d CLS plane;
        # row r's CLS is the unit basis vector e_r so pooling, L2 norm
        # and per-row variation are all observable.
        hidden = np.zeros((len(ids), 2, 1024), dtype=np.float32)
        for r in range(len(ids)):
            hidden[r, 0, r % 1024] = 1.0
        return [hidden]


@pytest.fixture()
def fake_ml(tmp_path: Path) -> Path:
    """Model dir with placeholder files + faked heavy imports."""
    model_dir = tmp_path / "bge-m3-int8"
    model_dir.mkdir()
    (model_dir / "model_int8.onnx").write_bytes(b"onnx")
    (model_dir / "tokenizer.json").write_text("{}")
    _FakeTokenizer.instances.clear()
    _FakeSession.instances.clear()
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "onnxruntime", MagicMock(InferenceSession=_FakeSession))
    monkey.setitem(sys.modules, "tokenizers", MagicMock(Tokenizer=_FakeTokenizer))
    yield model_dir
    monkey.undo()


# --- Embedder -----------------------------------------------------------------


class TestEmbedder:
    def test_load_and_embed_normalized(self, fake_ml: Path) -> None:
        emb = Embedder(fake_ml)
        assert emb.available is True
        # Second consult is memoized (no second session).
        assert emb.available is True
        assert len(_FakeSession.instances) == 1
        out = emb.embed(["Galahad", "Orc"])
        assert out.shape == (2, 1024)
        assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)
        # Padding + truncation configured on the tokenizer.
        tok = _FakeTokenizer.instances[0]
        assert tok.padding is True and tok.truncation is True

    def test_empty_batch_skips_session(self, fake_ml: Path) -> None:
        emb = Embedder(fake_ml)
        out = emb.embed([])
        assert out.shape == (0, 1024)
        assert _FakeSession.instances[0].run_calls == 0

    def test_missing_files_latch_off(self, tmp_path: Path) -> None:
        emb = Embedder(tmp_path / "nope")
        assert emb.available is False
        # The latch: repeated consults stay False without re-attempting.
        assert emb.available is False
        with pytest.raises(RuntimeError, match="unavailable"):
            emb.embed(["x"])

    def test_corrupt_session_latches_off(self, fake_ml: Path) -> None:
        def _boom(_path: str, _providers: list[str]) -> Any:
            raise RuntimeError("bad onnx")

        with patch.object(_FakeSession, "__init__", _boom):
            emb = Embedder(fake_ml)
            assert emb.available is False
            assert emb.available is False


# --- process singleton ---------------------------------------------------------


class TestEmbedderSingleton:
    def setup_method(self) -> None:
        embedder_reset_for_tests()

    def test_mock_cfg_takes_pure_llm_path(self) -> None:
        """MagicMock graph configs (every pre-2.5 test) must NEVER attempt
        a load — embed_enabled is truthy-but-not-True on them."""
        assert _embedder(MagicMock()) is None

    def test_disabled_cfg_returns_none(self) -> None:
        assert _embedder(_graph_cfg(embed_enabled=False)) is None

    def test_missing_model_latches_dead(self, tmp_path: Path) -> None:
        cfg = _graph_cfg(embed_model_path=tmp_path / "absent")
        assert _embedder(cfg) is None
        # Dead path: a fresh config object with the same path still
        # returns None WITHOUT constructing a second Embedder.
        with patch("worker.embeddings.Embedder") as ctor:
            assert _embedder(cfg) is None
            ctor.assert_not_called()

    def test_load_and_cache(self, fake_ml: Path) -> None:
        cfg = _graph_cfg(embed_model_path=fake_ml)
        e1 = _embedder(cfg)
        e2 = _embedder(cfg)
        assert e1 is not None and e1 is e2
        assert len(_FakeSession.instances) == 1


# --- three-zone decision --------------------------------------------------------


def _graph_cfg(**kw: Any) -> Any:
    """WorkerConfig-shaped namespace: the decision/singleton API reads
    ``cfg.graph.*`` (the activity passes the FULL config)."""
    return SimpleNamespace(graph=GraphConfig(**kw))


def _cfg_taus(high: float, low: float) -> Any:
    return _graph_cfg(embed_tau_high=high, embed_tau_low=low)


class TestSameEntityDecision:
    def test_above_high_is_same(self) -> None:
        a = [1.0, 0.0]
        b = [0.95, (1 - 0.95**2) ** 0.5]  # cosine 0.95
        d: Decision = same_entity_decision("A", "x", "B", "x", _cfg_taus(0.9, 0.75), a, b)
        assert d == "same"

    def test_below_low_is_distinct(self) -> None:
        d = same_entity_decision("A", "x", "B", "x", _cfg_taus(0.9, 0.75), [1.0, 0.0], [0.0, 1.0])
        assert d == "distinct"

    def test_gray_zone_asks(self) -> None:
        a = [1.0, 0.0]
        b = [0.85, (1 - 0.85**2) ** 0.5]  # cosine 0.85 — inside (0.75, 0.9)
        d = same_entity_decision("A", "x", "B", "x", _cfg_taus(0.9, 0.75), a, b)
        assert d == "ask"

    def test_missing_vector_asks(self) -> None:
        cfg = _cfg_taus(0.9, 0.75)
        assert same_entity_decision("A", "x", "B", "x", cfg, None, [1.0]) == "ask"
        assert same_entity_decision("A", "x", "B", "x", cfg, [1.0], None) == "ask"
        assert same_entity_decision("A", "x", "B", "x", cfg, None, None) == "ask"

    def test_zero_vector_asks(self) -> None:
        d = same_entity_decision("A", "x", "B", "x", _cfg_taus(0.9, 0.75), [0.0, 0.0], [1.0, 0.0])
        assert d == "ask"


# --- entity_vectors --------------------------------------------------------------


def _graph_of(*entities: Any) -> Any:
    from worker.enrich import ExtractedGraph

    return ExtractedGraph(entities=list(entities))


class TestEntityVectors:
    def test_none_embedder_returns_none(self) -> None:
        assert entity_vectors(None, _graph_of()) is None

    def test_empty_graph_returns_none(self) -> None:
        assert entity_vectors(MagicMock(), _graph_of()) is None

    def test_maps_final_slugs(self, fake_ml: Path) -> None:
        from worker.enrich import ExtractedEntity

        emb = Embedder(fake_ml)
        out = entity_vectors(
            emb, _graph_of(ExtractedEntity(slug="galahad", label="G", type="c"))
        )
        assert out is not None
        assert list(out) == ["galahad"]
        assert len(out["galahad"]) == 1024

    def test_embed_failure_returns_none(self) -> None:
        from worker.enrich import ExtractedEntity

        emb = MagicMock()
        emb.embed.side_effect = RuntimeError("gpu on fire")
        assert (
            entity_vectors(emb, _graph_of(ExtractedEntity(slug="a", label="A", type="t")))
            is None
        )


# --- graph-side helpers -----------------------------------------------------------


class TestEnsureVectorIndex:
    def test_statement_shape(self) -> None:
        session = MagicMock()
        driver = MagicMock()
        driver.session.return_value.__enter__ = MagicMock(return_value=session)
        driver.session.return_value.__exit__ = MagicMock(return_value=False)
        ensure_vector_index(driver, "neo4j")
        q = session.run.call_args.args[0]
        assert "CREATE VECTOR INDEX" in q and "IF NOT EXISTS" in q
        assert "`embedding_bge_m3`" in q
        assert "vector.dimensions`: 1024" in q
        assert "'cosine'" in q
        assert "(n:Entity) ON (n.embedding)" in q
        assert session.run.call_args.kwargs == {}


class TestSimilarInNamespace:
    def test_query_shape_and_rows(self) -> None:
        row = {"slug": "galahad", "label": "Galahad", "type": "character", "score": 0.93}
        session = MagicMock()
        session.run.return_value = [row]
        driver = MagicMock()
        driver.session.return_value.__enter__ = MagicMock(return_value=session)
        driver.session.return_value.__exit__ = MagicMock(return_value=False)
        out = similar_in_namespace(driver, "neo4j", "pathfinder", np.array([1.0, 0.0]), k=5)
        assert out == [row]
        q = session.run.call_args.args[0]
        assert "db.index.vector.queryNodes('embedding_bge_m3', $k, $vec)" in q
        # Namespace filter applied AFTER the YIELD (index is label-wide).
        assert q.index("YIELD node, score") < q.index("WHERE node.tag = $tag")
        params = session.run.call_args.kwargs
        assert params["k"] == 5 and params["tag"] == "pathfinder"
        assert isinstance(params["vec"], list)
