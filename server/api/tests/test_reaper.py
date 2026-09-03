"""Abandoned-upload reaper: stale `uploading` rows flip to `failed`."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import reaper
from app.db import Recording, engine


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    import importlib

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _create_uploading(client: TestClient, title: str) -> str:
    r = client.post("/recordings", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _backdate(rid: str, hours: float) -> None:
    """Move updated_at into the past via a Core UPDATE — explicit values
    bypass the ORM `onupdate` default that would otherwise stamp now()."""
    with Session(engine()) as s:
        s.execute(
            update(Recording)
            .where(Recording.id == rid)
            .values(updated_at=datetime.now(UTC) - timedelta(hours=hours))
        )
        s.commit()


def test_stale_uploading_marked_failed_fresh_untouched(client: TestClient) -> None:
    stale = _create_uploading(client, "abandoned")
    fresh = _create_uploading(client, "in-flight")
    _backdate(stale, hours=48)

    with Session(engine()) as s:
        reaped = list(reaper.sweep_abandoned_uploads(s, ttl_hours=24))

    assert reaped == [stale]
    assert client.get(f"/recordings/{stale}").json()["state"] == "failed"
    assert client.get(f"/recordings/{fresh}").json()["state"] == "uploading"


def test_reaped_recording_is_deletable(client: TestClient) -> None:
    rid = _create_uploading(client, "wedged")
    _backdate(rid, hours=72)
    with Session(engine()) as s:
        reaper.sweep_abandoned_uploads(s, ttl_hours=24)

    assert client.delete(f"/recordings/{rid}").status_code == 204
    assert client.get(f"/recordings/{rid}").status_code == 404
