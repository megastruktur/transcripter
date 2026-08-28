"""API surface for wave B: STAGE_KINDS includes ``enrich``; GET /profiles
exposes ``has_enrich``; regenerate accepts ``stage: "enrich"``."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("TRANSCRIPTER_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("TRANSCRIPTER_STORAGE", str(tmp_path / "storage"))
    (tmp_path / "profiles").mkdir()
    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _force_state(rid: str, state: str) -> None:
    from app.db import Recording, get_session

    gen = get_session()
    s = next(gen)
    try:
        rec = s.get(Recording, rid)
        assert rec is not None
        rec.state = state
        s.commit()
    finally:
        gen.close()


def _write_profile(path: Path, body: str) -> Path:
    p = path / "profile.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_stage_kinds_includes_enrich() -> None:
    """The enum members that back STAGE_KINDS must list enrich so the
    idempotent migration adds it to Postgres on the next start."""
    from app.db import STAGE_KINDS

    assert "enrich" in STAGE_KINDS


def test_get_profiles_has_enrich_true(client: TestClient, tmp_path: Path) -> None:
    _write_profile(
        tmp_path / "profiles",
        """\
id: pathfinder
version: 1.0.0
display_name: Pathfinder
description: d
tags: [p]
summarize:
  prompt: 's {transcript}'
  output_artifact: summary.md
enrich:
  prompt: 'e {transcript}'
""",
    )
    r = client.get("/profiles")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["has_enrich"] is True


def test_get_profiles_has_enrich_false_without_enrich(client: TestClient, tmp_path: Path) -> None:
    _write_profile(
        tmp_path / "profiles",
        """\
id: meeting
version: 1.0.0
display_name: Meeting
description: d
tags: [m]
summarize:
  prompt: 's {transcript}'
""",
    )
    rows = client.get("/profiles").json()
    assert len(rows) == 1
    assert rows[0]["has_enrich"] is False


def test_get_profiles_has_enrich_false_when_prompt_lacks_transcript(
    client: TestClient, tmp_path: Path
) -> None:
    _write_profile(
        tmp_path / "profiles",
        """\
id: bad
version: 1.0.0
display_name: Bad
description: d
tags: [b]
summarize:
  prompt: 's {transcript}'
enrich:
  prompt: 'no placeholder'
""",
    )
    rows = client.get("/profiles").json()
    assert rows[0]["has_enrich"] is False


def test_regenerate_enrich_accepted(client: TestClient, tmp_path: Path) -> None:
    rid = client.post("/recordings", json={"title": "x"}).json()["id"]
    _force_state(rid, "done")
    with patch("app.temporal_client.regenerate_stage", new_callable=AsyncMock) as m:
        m.return_value = "wf-enrich"
        r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "enrich"})
    assert r.status_code == 200
    assert r.json() == {"workflow_id": "wf-enrich", "stage": "enrich"}
    m.assert_awaited_once()
    # Verify the worker's regenerate arg carried the kind.
    args = m.await_args
    assert args.args[1] == "enrich"


def test_regenerate_enrich_unknown_stage_400(client: TestClient) -> None:
    rid = client.post("/recordings", json={"title": "x"}).json()["id"]
    r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "enrichx"})
    assert r.status_code == 400


def test_idempotent_stage_kind_migration_runs_twice() -> None:
    """Re-running the migration with enrich already in STAGE_KINDS is a
    no-op: ``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is idempotent on
    Postgres; on SQLite the call short-circuits via dialect check.

    We re-execute the migration on the same engine and verify no
    exception is raised. (Postgres-only path is exercised in CI.)"""
    from sqlalchemy import create_engine

    from app.main import _migrate_stage_kind_enum

    engine = create_engine("sqlite:///:memory:")
    # Two passes — both must succeed.
    _migrate_stage_kind_enum.__globals__["engine"] = lambda: engine
    # The function guards on dialect, so SQLite no-ops; we just verify
    # the call shape accepts any number of repeats.
    for _ in range(2):
        _migrate_stage_kind_enum()
