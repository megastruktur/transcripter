"""Phase 2.5 — bge-m3 ONNX int8 sentence embedder (entity-dedup prefilter).

The dedup loop in ``enrich.resolve_slugs`` historically asked the LLM
"same entity? Y/N" for every slug collision. This module lets a local
ONNX export of bge-m3 (Xenova int8, CLS pooling, L2-normalized 1024-d
vectors) answer the obvious cases first:

- cosine >= ``graph.embed_tau_high`` → same entity (no LLM call),
- cosine <= ``graph.embed_tau_low``  → distinct (no LLM call),
- gray zone / missing vectors       → LLM Y/N exactly as before.

Everything here degrades gracefully: a missing model directory, a
corrupt ONNX file or a failed session build latches the embedder OFF
for the process (one warning) and the dedup path behaves exactly like
the pre-2.5 pure-LLM loop. Embeddings never crash an activity.

Inference recipe (verified live against /models/bge-m3-int8, ~0.08 s
per 32 texts on CPU): tokenizer padding to batch-max, truncation at
256 tokens, one ``session.run`` per batch, CLS token + L2 norm.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Literal

import numpy as np

log = logging.getLogger("transcripter.embeddings")

# Dimension of the bge-m3 dense CLS embedding (fixed by the export).
_EMBED_DIM = 1024

# Name of the Neo4j vector index over Entity.embedding. Main created it
# on the live 5.26.30 graph; ensure_vector_index() runs the same
# statement with IF NOT EXISTS so fresh installs self-provision.
_VECTOR_INDEX_NAME = "embedding_bge_m3"
_VECTOR_DIMENSIONS = 1024
# The OPTIONS braces are literal Cypher (the string after the f-prefix
# keeps single braces verbatim — same rule as enrich's event query).
_VECTOR_INDEX_STATEMENT = (
    f"CREATE VECTOR INDEX `{_VECTOR_INDEX_NAME}` IF NOT EXISTS "
    "FOR (n:Entity) ON (n.embedding) "
    "OPTIONS {indexConfig: {`vector.dimensions`: 1024, "
    "`vector.similarity_function`: 'cosine'}}"
)

# Cosine zones for same_entity_decision, re-exported as a type alias so
# the contract reads in one place.
Decision = Literal["same", "distinct", "ask"]


class Embedder:
    """Lazy ONNX bge-m3 embedder: CLS pooling, L2-normalized vectors.

    ``available`` triggers (and memoizes) the one-time load; a load
    failure latches ``_failed`` so repeated calls never retry and never
    log more than one warning. ``embed`` raises RuntimeError when
    unavailable — callers are expected to consult ``available`` first
    (``_embedder``/``entity_vectors`` do).
    """

    def __init__(self, model_path: str | Path) -> None:
        self._model_path = Path(model_path)
        self._lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None
        self._loaded = False
        self._failed = False

    @property
    def available(self) -> bool:
        return self._ensure_loaded()

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        if self._failed:
            return False
        with self._lock:
            if self._loaded:
                return True
            if self._failed:
                return False
            try:
                model_file = self._model_path / "model_int8.onnx"
                tokenizer_file = self._model_path / "tokenizer.json"
                if not model_file.is_file() or not tokenizer_file.is_file():
                    raise FileNotFoundError(
                        f"missing {model_file} or {tokenizer_file}"
                    )
                # Deferred heavy imports: numpy stays the only module-level
                # dependency so importing this package is always cheap.
                import onnxruntime as ort
                from tokenizers import Tokenizer

                tokenizer = Tokenizer.from_file(str(tokenizer_file))
                tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=None)
                tokenizer.enable_truncation(max_length=256)
                session = ort.InferenceSession(
                    str(model_file), providers=["CPUExecutionProvider"]
                )
                self._tokenizer = tokenizer
                self._session = session
                self._loaded = True
            except Exception as exc:  # noqa: BLE001 — ANY load failure must latch off
                log.warning(
                    "embeddings: model at %s unavailable (%s); "
                    "entity-dedup embedding prefilter disabled for this process",
                    self._model_path,
                    exc,
                )
                self._failed = True
            return self._loaded

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed ``texts`` → (n, 1024) float32 matrix, rows L2-normalized.

        One ``session.run`` for the whole batch (the extraction's entity
        labels are dozens, well under the verified ~32-text budget per
        0.08 s).
        """
        if not self._ensure_loaded():
            raise RuntimeError(
                f"embedder unavailable (model path {self._model_path})"
            )
        if not texts:
            return np.zeros((0, _EMBED_DIM), dtype=np.float32)
        encoded = self._tokenizer.encode_batch(list(texts))
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array(
            [e.attention_mask for e in encoded], dtype=np.int64
        )
        hidden = self._session.run(
            None,
            {"input_ids": input_ids, "attention_mask": attention_mask},
        )[0]
        vectors = hidden[:, 0]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return (vectors / norms).astype(np.float32)


# --- process-wide singleton (lazy, failure-latched) ---------------------------

_EMBEDDERS: dict[str, Embedder] = {}
_DEAD_PATHS: set[str] = set()
_SINGLETON_LOCK = threading.Lock()


def _embedder(cfg: Any) -> Embedder | None:
    """Singleton ``Embedder`` for ``cfg.graph.embed_model_path`` or None.

    The ``embed_enabled is True`` gate is deliberate: real configs carry
    a real bool (default True), while test configs are MagicMocks whose
    auto-created attribute is truthy-but-not-True — those must take the
    pure-LLM path, never attempt an ONNX load. First load failure latches
    the path dead for the whole process (one warning, then None forever).
    """
    graph = getattr(cfg, "graph", None)
    if graph is None or graph.embed_enabled is not True:
        return None
    path = str(graph.embed_model_path)
    with _SINGLETON_LOCK:
        if path in _DEAD_PATHS:
            return None
        embedder = _EMBEDDERS.get(path)
        if embedder is None:
            embedder = Embedder(path)
            if not embedder.available:
                _DEAD_PATHS.add(path)
                return None
            _EMBEDDERS[path] = embedder
        return embedder


def embedder_reset_for_tests() -> None:
    """Clear the singleton + failure latch. Tests only — production
    state is process-wide by design."""
    with _SINGLETON_LOCK:
        _EMBEDDERS.clear()
        _DEAD_PATHS.clear()


def same_entity_decision(
    new_label: str,
    new_type: str,
    existing_label: str,
    existing_type: str,
    cfg: Any,
    new_vec: np.ndarray | list[float] | None,
    existing_vec: np.ndarray | list[float] | None,
) -> Decision:
    """Three-zone prefilter ahead of the LLM Y/N call.

    Both vectors present: cosine >= tau_high → "same", cosine <= tau_low
    → "distinct", the gray zone in between → "ask". Any missing (or
    zero) vector → "ask" — the LLM path stays the universal fallback.
    Labels/types ride along in the signature for the future gray-zone
    cross-encoder; the vector zones never consult them.
    """
    if new_vec is None or existing_vec is None:
        return "ask"
    a = np.asarray(new_vec, dtype=np.float32)
    b = np.asarray(existing_vec, dtype=np.float32)
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return "ask"
    cosine = float(np.dot(a, b)) / (norm_a * norm_b)
    if cosine >= cfg.graph.embed_tau_high:
        return "same"
    if cosine <= cfg.graph.embed_tau_low:
        return "distinct"
    return "ask"


def entity_vectors(
    embedder: Embedder | None, graph: Any
) -> dict[str, list[float]] | None:
    """FINAL-slug → vector dict for ``write_to_graph``'s ``embeddings``.

    One batched ``embed`` over the RESOLVED entities' labels (labels
    survive dedup unchanged; only slugs move). None when the embedder is
    unavailable or the batch fails — never raises, the graph write must
    not hinge on embeddings.
    """
    if embedder is None or graph is None or not graph.entities:
        return None
    entities = graph.entities
    try:
        matrix = embedder.embed([e.label for e in entities])
        return {
            e.slug: row.tolist() if hasattr(row, "tolist") else list(row)
            for e, row in zip(entities, matrix, strict=True)
        }
    except Exception:
        log.exception(
            "embeddings: entity-vector batch failed; writing without embeddings"
        )
        return None


def ensure_vector_index(driver: Any, database: str) -> None:
    """CREATE VECTOR INDEX IF NOT EXISTS over Entity.embedding (cosine,
    1024-d) — idempotent, cheap enough to run before every graph write
    so fresh installs self-provision (Main already created it live)."""
    with driver.session(database=database) as session:
        session.run(_VECTOR_INDEX_STATEMENT)


def similar_in_namespace(
    driver: Any,
    database: str,
    tag: str,
    vec: np.ndarray,
    k: int = 5,
) -> list[dict[str, Any]]:
    """k nearest Entity nodes to ``vec`` within namespace ``tag``.

    The vector index is label-wide (no tag dimension), so the namespace
    filter applies AFTER ``YIELD node, score``; ``queryNodes`` returns
    rows already score-ordered and the explicit ORDER BY keeps that
    contract local. Callers get plain dicts; ``score`` is the cosine
    similarity reported by the index. Phase 3.5 will consume this for
    ANN-across-namespace dedup — resolve_slugs deliberately does not.
    """
    query = (
        f"CALL db.index.vector.queryNodes('{_VECTOR_INDEX_NAME}', $k, $vec) "
        "YIELD node, score "
        "WHERE node.tag = $tag "
        "RETURN node.slug AS slug, node.label AS label, node.type AS type, score "
        "ORDER BY score DESC"
    )
    with driver.session(database=database) as session:
        rows = session.run(query, k=k, vec=np.asarray(vec, dtype=np.float32).tolist(), tag=tag)
        return [
            {
                "slug": row["slug"],
                "label": row["label"],
                "type": row["type"],
                "score": row["score"],
            }
            for row in rows
        ]
