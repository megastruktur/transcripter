"""Phase 3.5 — search-side embedding client (twin of worker/embeddings.py).

The api and worker are separate uv projects (no shared package), so the
small client is copied here — the SAME dispatch shape the worker uses:

- ``local``: bge-m3 ONNX int8 in-process (lazy singleton, failure-latched;
  needs onnxruntime+tokenizers and the /models mount — compose adds both).
- ``http``: any OpenAI-compatible ``POST {base_url}/embeddings``.

The query vector must come from the SAME backend the worker indexed with
(the index file's meta catches a mismatch — search replies 503 then).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("transcripter.api.embeddings")

# Dimension of the bge-m3 dense CLS embedding (fixed by the export).
_EMBED_DIM = 1024


class Embedder:
    """Lazy ONNX bge-m3 embedder: CLS pooling, L2-normalized vectors.

    Same failure-latching contract as the worker's copy: a load failure
    latches OFF for the process (one warning) and ``embed`` raises — the
    search route turns that into a 503 ``available: false``.
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
                # Deferred heavy imports so importing this module is cheap.
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
                    "semantic search disabled for this process",
                    self._model_path,
                    exc,
                )
                self._failed = True
            return self._loaded

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` → list of L2-normalized vectors (one batch)."""
        if not self._ensure_loaded():
            raise RuntimeError(
                f"embedder unavailable (model path {self._model_path})"
            )
        if not texts:
            return []
        import numpy as np

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
        return ((vectors / norms).astype(np.float32)).tolist()


class HttpEmbedder:
    """OpenAI-compatible ``POST {base_url}/embeddings`` — stateless; every
    call carries its own 60 s timeout and failures propagate to the
    caller (the search route maps them to 503)."""

    def __init__(self, base_url: str, model: str, api_key_env: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key_env = api_key_env

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import httpx

        api_key = os.environ.get(self._api_key_env, "") if self._api_key_env else ""
        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.post(
            f"{self._base_url}/embeddings",
            headers=headers,
            json={"model": self._model, "input": texts},
            timeout=60.0,
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [row["embedding"] for row in data]


def embed_query(text: str, cfg: Any) -> list[float] | None:
    """Embed ONE search query through the configured backend.

    Returns the vector, or None when the local backend is off/unavailable
    (degrade — the route 503s with ``available: false``). http-backend
    failures RAISE (httpx errors) — the route catches and 503s.
    """
    embed_cfg = cfg.graph.embed
    if embed_cfg.backend == "http":
        return HttpEmbedder(
            embed_cfg.base_url, embed_cfg.model, embed_cfg.api_key_env
        ).embed([text])[0]
    embedder = _embedder(cfg)
    if embedder is None:
        return None
    return embedder.embed([text])[0]


def expected_index_meta(cfg: Any) -> dict[str, str]:
    """The {backend, model, dimensions} the ACTIVE config implies — the
    exact twin of worker semantic_index._expected_meta, so the search
    side compares against what the writer recorded. A mismatch means the
    index file was built by a DIFFERENT backend/model → 503 + backfill
    hint, never a silent cross-space KNN."""
    embed_cfg = cfg.graph.embed
    if embed_cfg.backend == "local":
        model = f"onnx:{Path(embed_cfg.model_path).name}"
    else:
        model = embed_cfg.model or "http"
    return {
        "backend": embed_cfg.backend,
        "model": model,
        "dimensions": str(embed_cfg.configured_dimensions),
    }


# --- process-wide singleton (lazy, failure-latched) ---------------------------

_EMBEDDERS: dict[str, Embedder] = {}
_DEAD_PATHS: set[str] = set()
_SINGLETON_LOCK = threading.Lock()


def _embedder(cfg: Any) -> Embedder | None:
    """Singleton ``Embedder`` for the configured model path, or None when
    the model is off/unavailable (latched — one warning per process)."""
    path = str(cfg.graph.embed.model_path)
    if path in _DEAD_PATHS:
        return None
    with _SINGLETON_LOCK:
        embedder = _EMBEDDERS.get(path)
        if embedder is None:
            embedder = Embedder(path)
            if not embedder.available:
                _DEAD_PATHS.add(path)
                return None
            _EMBEDDERS[path] = embedder
        return embedder


def embedder_reset_for_tests() -> None:
    """Clear the singleton + failure latch. Tests only."""
    with _SINGLETON_LOCK:
        _EMBEDDERS.clear()
        _DEAD_PATHS.clear()
