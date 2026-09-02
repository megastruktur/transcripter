"""Phase C API surface: "Correct the record" fix-preview / fix-apply.

Contract under test:
- POST /fix-preview: 409 graph off, 404 unknown tag, 400 short
  instruction, 202 + workflow_id; the per-tag gate 409s a second
  concurrent preview and 429s inside the cooldown.
- GET /fix-preview/{id}: poll shapes — running (unknown workflow),
  ready (proposal), failed.
- POST /fix-apply: 202 + workflow_id; 400 on empty ops.
- GET /fix-apply/{id}: ok / stale (per-op rejections) / running.

Temporal mocked at the temporal_client level (Phase A pattern); the
poll endpoints mock the workflow-handle result path."""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import temporal_client


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    monkeypatch.setattr(temporal_client, "start_pipeline", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "regenerate_stage", AsyncMock(return_value="wf-regen"))
    monkeypatch.setattr(temporal_client, "start_export", AsyncMock(return_value="wf-export"))
    monkeypatch.setattr(
        temporal_client, "start_apply_graph_edit", AsyncMock(return_value="wf-edit")
    )
    monkeypatch.setattr(
        temporal_client,
        "start_fix_preview",
        AsyncMock(return_value="graph-fix-preview-quest-abc12345"),
    )
    monkeypatch.setattr(
        temporal_client,
        "start_fix_apply",
        AsyncMock(return_value="graph-fix-apply-quest-abc12345"),
    )
    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _enable_graph(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.graph, "uri", "bolt://n:7687")


def _seed_tag(client: TestClient) -> str:
    from app.db import Recording, RecordingState, get_session

    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    gen = get_session()
    session = next(gen)
    try:
        rec = session.get(Recording, rid)
        assert rec is not None
        rec.state = RecordingState.done
        session.commit()
    finally:
        gen.close()
    return rid


# ---------- POST /fix-preview ----------


def test_fix_preview_409_when_graph_off(client: TestClient) -> None:
    r = client.post("/tags/quest/fix-preview", json={"instruction": "fix the attribution"})
    assert r.status_code == 409


def test_fix_preview_404_unknown_tag(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.post("/tags/nosuch/fix-preview", json={"instruction": "fix the attribution"})
    assert r.status_code == 404


def test_fix_preview_400_short_instruction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    r = client.post("/tags/quest/fix-preview", json={"instruction": "x"})
    assert r.status_code == 422  # Field(min_length=3)


def test_fix_preview_202_and_gate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    r = client.post(
        "/tags/quest/fix-preview",
        json={"instruction": "the operator built the agent network, not Glennis"},
    )
    assert r.status_code == 202
    assert r.json()["workflow_id"].startswith("graph-fix-preview-quest")

    # In-flight gate: a second preview 409s while the first is pending
    # (the gate holds a slot until the poll endpoint releases it).
    r2 = client.post("/tags/quest/fix-preview", json={"instruction": "second attempt"})
    assert r2.status_code == 409

    # Other tags are unaffected.
    other = client.post("/recordings", json={"tags": ["other"]}).json()["id"]
    from app.db import Recording, RecordingState, get_session

    gen = get_session()
    session = next(gen)
    try:
        rec = session.get(Recording, other)
        assert rec is not None
        rec.state = RecordingState.done
        session.commit()
    finally:
        gen.close()
    r3 = client.post("/tags/other/fix-preview", json={"instruction": "unrelated fix"})
    assert r3.status_code == 202


def test_fix_preview_cooldown_after_release(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    assert (
        client.post("/tags/quest/fix-preview", json={"instruction": "first attempt"}).status_code
        == 202
    )
    # Release the slot by polling the (mocked running) result.
    handle = MagicMock()
    handle.result = AsyncMock(side_effect=_rpc_error())
    with patch.object(temporal_client, "get_client", AsyncMock(return_value=_client_with(handle))):
        client.get("/tags/quest/fix-preview/graph-fix-preview-quest-abc12345")
    # Immediately after release the cooldown window applies.
    r = client.post("/tags/quest/fix-preview", json={"instruction": "too soon"})
    assert r.status_code == 429


# ---------- GET /fix-preview/{id} ----------


def test_fix_preview_poll_shapes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    # Unknown id → RpcError from the handle → running.
    handle = MagicMock()
    handle.result = AsyncMock(side_effect=_rpc_error())
    with patch.object(
        temporal_client, "get_client", AsyncMock(return_value=_client_with(handle, running=True))
    ):
        r = client.get("/tags/quest/fix-preview/graph-fix-preview-quest-abc12345")
    assert r.status_code == 200
    assert r.json()["state"] == "running"

    # Settled ready.
    handle2 = MagicMock()
    handle2.result = AsyncMock(
        return_value={
            "ok": True,
            "proposal": {"ops": [{"op": "entity_delete", "slug": "dupe"}], "rationale": ["d"]},
            "context": {"recording_id": "r1", "title": "t"},
        }
    )
    with patch.object(temporal_client, "get_client", AsyncMock(return_value=_client_with(handle2))):
        r = client.get("/tags/quest/fix-preview/graph-fix-preview-quest-abc12345")
    body = r.json()
    assert body["state"] == "ready"
    assert body["proposal"]["ops"][0]["slug"] == "dupe"

    # Busy result from the activity (LLM timeout).
    handle3 = MagicMock()
    handle3.result = AsyncMock(
        return_value={"ok": False, "reason": "busy", "detail": "read timeout"}
    )
    with patch.object(temporal_client, "get_client", AsyncMock(return_value=_client_with(handle3))):
        r = client.get("/tags/quest/fix-preview/graph-fix-preview-quest-abc12345")
    assert r.json()["state"] == "busy"


def test_fix_preview_poll_rejects_foreign_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    r = client.get("/tags/quest/fix-preview/some-other-workflow")
    assert r.status_code == 400


# ---------- POST /fix-apply + poll ----------


def test_fix_apply_202_and_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    r = client.post(
        "/tags/quest/fix-apply",
        json={"proposal": {"ops": [{"op": "entity_delete", "slug": "dupe"}], "rationale": ["d"]}},
    )
    assert r.status_code == 202
    assert r.json()["workflow_id"].startswith("graph-fix-apply-quest-")

    r2 = client.post("/tags/quest/fix-apply", json={"proposal": {"ops": []}})
    assert r2.status_code == 400


def test_fix_apply_poll_stale_rejections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    handle = MagicMock()
    handle.result = AsyncMock(
        return_value={
            "ok": False,
            "reason": "stale",
            "rejections": [
                {
                    "index": 0,
                    "op": {"op": "event_delete", "event_key": "gone"},
                    "reason": "event gone not found (regenerated?)",
                }
            ],
        }
    )
    with patch.object(temporal_client, "get_client", AsyncMock(return_value=_client_with(handle))):
        r = client.get("/tags/quest/fix-apply/graph-fix-apply-quest-abc12345")
    body = r.json()
    assert body["state"] == "stale"
    assert body["rejections"][0]["index"] == 0

    # ok path
    handle2 = MagicMock()
    handle2.result = AsyncMock(return_value={"ok": True, "applied": 2, "edit_ids": [11, 12]})
    with patch.object(temporal_client, "get_client", AsyncMock(return_value=_client_with(handle2))):
        r = client.get("/tags/quest/fix-apply/graph-fix-apply-quest-abc12345")
    assert r.json()["state"] == "ok"


# ---------- helpers ----------


def _client_with(handle: MagicMock, running: bool = False) -> MagicMock:
    """describe() mirrors the running flag; a settled handle (result
    mocked) reports COMPLETED so the endpoint takes the result path."""
    from temporalio.client import WorkflowExecutionStatus

    desc = MagicMock()
    desc.status = WorkflowExecutionStatus.RUNNING if running else WorkflowExecutionStatus.COMPLETED
    handle.describe = AsyncMock(return_value=desc)
    c = MagicMock()
    c.get_workflow_handle.return_value = handle
    return c


def _rpc_error() -> Exception:
    from temporalio.service import RPCError

    try:
        raise RPCError("not found", 0, 0)
    except RPCError as e:
        return e
