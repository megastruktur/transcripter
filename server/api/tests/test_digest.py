"""Wave C API surface: POST /tags/{tag}/digest.

Tag normalization + regex validation, last_n bounds, graph-disabled 409,
workflow_id round-trip. Temporal is mocked at the temporal_client level
(the same way regenerate / rename tests do it).
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    from app import temporal_client

    monkeypatch.setattr(
        temporal_client, "start_digest", AsyncMock(return_value="wf-digest-abc")
    )
    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _enable_graph(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutate the config object the running app captured at import time.

    Default conftest graph.uri=""; tests that need the happy path flip
    it in place. ``enabled`` is a derived property, so the assignment is
    enough — the very next read sees True.
    """
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.graph, "uri", "bolt://n:7687")
    assert cfg.graph.enabled is True


# ---------- 202 happy path ----------


def test_digest_happy_path_returns_202(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/pathfinder/digest", json={"last_n": 3})
    assert r.status_code == 202
    body = r.json()
    assert body["workflow_id"] == "wf-digest-abc"
    assert body["tag"] == "pathfinder"
    assert body["last_n"] == 3


def test_digest_default_last_n(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/pathfinder/digest", json={})
    assert r.status_code == 202
    assert r.json()["last_n"] == 5


def test_digest_normalizes_tag_uppercase(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/PATHFINDER/digest", json={"last_n": 2})
    assert r.status_code == 202
    body = r.json()
    assert body["tag"] == "pathfinder"
    from app import temporal_client

    temporal_client.start_digest.assert_awaited_once_with("pathfinder", 2)


def test_digest_normalizes_tag_trim_whitespace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    # %20 = url-encoded space; the path arrives already decoded by Starlette.
    r = client.post("/tags/%20%20pathfinder%20%20/digest", json={"last_n": 2})
    assert r.status_code == 202
    assert r.json()["tag"] == "pathfinder"


def test_digest_last_n_upper_bound(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/pathfinder/digest", json={"last_n": 51})
    assert r.status_code == 422  # pydantic validation (le=50)


def test_digest_last_n_lower_bound(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/pathfinder/digest", json={"last_n": 0})
    assert r.status_code == 422  # pydantic validation (ge=1)


def test_digest_last_n_negative(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/pathfinder/digest", json={"last_n": -1})
    assert r.status_code == 422


def test_digest_tag_with_mixed_case_normalizes_and_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``HasUpper`` → normalized to ``hasupper``, which is valid."""
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/HasUpper/digest", json={"last_n": 2})
    assert r.status_code == 202
    assert r.json()["tag"] == "hasupper"


def test_digest_tag_starting_with_punctuation_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/-leading/digest", json={"last_n": 2})
    assert r.status_code == 400
    assert "match" in r.json()["detail"].lower()


def test_digest_tag_with_disallowed_chars_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    # %24 = url-encoded '$'; the regex forbids it.
    r = client.post("/tags/bad%24chars/digest", json={"last_n": 2})
    assert r.status_code == 400


def test_digest_tag_with_spaces_202(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 0: free tags may contain spaces — the API accepts them and
    the worker slugs the filename (frontmatter keeps the display tag)."""
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/morning%20standup/digest", json={"last_n": 2})
    assert r.status_code == 202, r.text
    assert r.json()["tag"] == "morning standup"


def test_digest_tag_too_long_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post(f"/tags/{'a' * 65}/digest", json={"last_n": 2})
    assert r.status_code == 400


# ---------- 409 when graph is disabled ----------


def test_digest_graph_disabled_409(client: TestClient) -> None:
    """Default test env has graph.uri="" — so the route rejects 409
    BEFORE reaching temporal_client."""
    r = client.post("/tags/pathfinder/digest", json={"last_n": 2})
    assert r.status_code == 409
    assert "graph" in r.json()["detail"].lower()


# ---------- 503 when Temporal is down ----------


def test_digest_temporal_down_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    from app import temporal_client

    monkeypatch.setattr(
        temporal_client,
        "start_digest",
        AsyncMock(side_effect=ConnectionError("temporal down")),
    )
    r = client.post("/tags/pathfinder/digest", json={"last_n": 2})
    assert r.status_code == 503
    assert "temporal" in r.json()["detail"].lower()


# ---------- workflow_id prefix contract ----------


def test_digest_workflow_id_prefix() -> None:
    """The worker side names the workflow TagDigest and the prefix
    "digest-"; this is the contract the activity dispatch relies on."""
    from app import temporal_client

    assert temporal_client.DIGEST_WORKFLOW_NAME == "TagDigest"
    assert temporal_client.DIGEST_WORKFLOW_ID_PREFIX == "digest-"


def test_digest_calls_temporal_client_with_normalized_tag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    from app import temporal_client

    r = client.post("/tags/%20pathfinder%20/digest", json={"last_n": 7})
    assert r.status_code == 202
    temporal_client.start_digest.assert_awaited_once_with("pathfinder", 7)


def test_digest_post_without_body_returns_202_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-04 regression: the client fired a bare POST (no JSON body)
    and FastAPI 422'd the required DigestRequest model before any handler
    code ran — the UI surfaced it as "Digest request failed:
    [object Object]". The body is now optional; no body = last_n 5."""
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/pathfinder/digest")
    assert r.status_code == 202
    assert r.json()["last_n"] == 5
    from app import temporal_client

    temporal_client.start_digest.assert_awaited_once_with("pathfinder", 5)