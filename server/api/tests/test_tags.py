"""Tags + profiles contract tests."""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")

    from app import temporal_client

    monkeypatch.setattr(temporal_client, "start_pipeline", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "regenerate_stage", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "start_export", AsyncMock(return_value="wf-export"))

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


# ---------- Recording creation ----------


def test_create_with_tags_persists(client: TestClient) -> None:
    r = client.post("/recordings", json={"title": "t", "tags": ["alpha", "Beta"]})
    assert r.status_code == 201
    rid = r.json()["id"]
    body = client.get(f"/recordings/{rid}").json()
    # Beta is lower-cased; ordering preserved
    assert body["tags"] == ["alpha", "beta"]


def test_tags_are_normalized_on_create(client: TestClient) -> None:
    """trim + lowercase + drop blanks; first-seen order wins."""
    r = client.post(
        "/recordings",
        json={"title": "t", "tags": ["  Foo  ", "foo", "BAR", "", "  ", "bar", "Baz"]},
    )
    rid = r.json()["id"]
    body = client.get(f"/recordings/{rid}").json()
    assert body["tags"] == ["foo", "bar", "baz"]


def test_default_tags_is_empty_list(client: TestClient) -> None:
    r = client.post("/recordings", json={"title": "t"})
    rid = r.json()["id"]
    assert client.get(f"/recordings/{rid}").json()["tags"] == []


def test_serialize_includes_tags(client: TestClient) -> None:
    r = client.post("/recordings", json={"title": "t", "tags": ["a", "b"]})
    rid = r.json()["id"]
    # Listed under /recordings also carries tags
    items = client.get("/recordings").json()["items"]
    row = next(i for i in items if i["id"] == rid)
    assert row["tags"] == ["a", "b"]


# ---------- PATCH tags ----------


def test_patch_tags_replaces_set(client: TestClient) -> None:
    rid = client.post("/recordings", json={"tags": ["orig"]}).json()["id"]
    r = client.patch(f"/recordings/{rid}", json={"tags": ["new", "ORIG"]})
    assert r.status_code == 200
    assert r.json()["tags"] == ["new", "orig"]
    # Round-trip through GET too
    assert client.get(f"/recordings/{rid}").json()["tags"] == ["new", "orig"]


def test_patch_tags_triggers_export(client: TestClient) -> None:
    from app import temporal_client

    rid = client.post("/recordings", json={"tags": []}).json()["id"]
    r = client.patch(f"/recordings/{rid}", json={"tags": ["foo"]})
    assert r.status_code == 200
    # Tags shift the summarize profile match: the artifact filename and
    # frontmatter must be re-emitted — a rename_only export cannot do that.
    temporal_client.start_export.assert_awaited_once_with(rid, rename_only=False)


def test_patch_title_only_triggers_rename_only_export(client: TestClient) -> None:
    from app import temporal_client

    temporal_client.start_export.reset_mock()
    rid = client.post("/recordings", json={"tags": []}).json()["id"]
    r = client.patch(f"/recordings/{rid}", json={"title": "renamed"})
    assert r.status_code == 200
    # Title-only: rename the folder, keep user-edited notes untouched.
    temporal_client.start_export.assert_awaited_once_with(rid, rename_only=True)


def test_patch_title_only_does_not_touch_tags(client: TestClient) -> None:
    rid = client.post("/recordings", json={"tags": ["keep"]}).json()["id"]
    r = client.patch(f"/recordings/{rid}", json={"title": "new title"})
    assert r.status_code == 200
    assert r.json()["tags"] == ["keep"]


def test_patch_empty_body_400(client: TestClient) -> None:
    rid = client.post("/recordings", json={}).json()["id"]
    assert client.patch(f"/recordings/{rid}", json={}).status_code == 400


# ---------- ?q= search by tag ----------


def test_query_matches_tag(client: TestClient) -> None:
    rid_match = client.post(
        "/recordings", json={"title": "L", "tags": ["morning-standup"]}
    ).json()["id"]
    client.post("/recordings", json={"title": "Evening sync", "tags": ["other"]})

    body = client.get("/recordings", params={"q": "morning"}).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == rid_match
    assert body["items"][0]["tags"] == ["morning-standup"]


def test_query_matches_title_or_tag(client: TestClient) -> None:
    client.post("/recordings", json={"title": "Standup notes", "tags": ["other"]})
    client.post("/recordings", json={"title": "Other", "tags": ["standup"]})

    body = client.get("/recordings", params={"q": "standup"}).json()
    assert body["total"] == 2


# ---------- Migration idempotence ----------


def test_migration_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Calling _migrate_tags_column twice on the active dialect must be a
    no-op the second time. SQLite takes the skip branch but exercising
    the function twice still proves the call is safe to repeat at every
    process startup."""
    from app import main

    main._migrate_tags_column()
    main._migrate_tags_column()
    # Reaching this line without raising is the contract.


# ---------- /profiles ----------


def test_profiles_returns_empty_when_dir_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.profiles, "path", tmp_path / "does-not-exist")
    r = client.get("/profiles")
    assert r.status_code == 200
    assert r.json() == []


def test_profiles_lists_valid_yaml(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "p.yaml").write_text(
        "id: standup\n"
        "version: '1'\n"
        "display_name: Standup\n"
        "description: Daily sync\n"
        "tags: [standup, sync]\n"
        "summarize: {prompt: 'Sum {transcript}'}\n"
    )
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.profiles, "path", profiles_dir)

    r = client.get("/profiles")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["id"] == "standup"
    assert entry["version"] == "1"
    assert entry["display_name"] == "Standup"
    assert entry["description"] == "Daily sync"
    assert entry["tags"] == ["standup", "sync"]


def test_profiles_skips_broken_yaml(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    # broken YAML
    (profiles_dir / "bad.yaml").write_text("id: [\n")
    # missing required key
    (profiles_dir / "incomplete.yaml").write_text("id: x\ndisplay_name: y\n")
    # good one
    (profiles_dir / "good.yaml").write_text(
        "id: g\nversion: '1'\ndisplay_name: G\ndescription: ok\ntags: [t]\n"
        "summarize: {prompt: 'Sum {transcript}'}\n"
    )

    cfg = client.app.state.config
    monkeypatch.setattr(cfg.profiles, "path", profiles_dir)

    r = client.get("/profiles")
    assert r.status_code == 200
    ids = sorted(p["id"] for p in r.json())
    assert ids == ["g"]


def test_profiles_skips_non_list_tags(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "bad_tags.yaml").write_text(
        "id: bt\ndisplay_name: BT\ndescription: d\ntags: not-a-list\n"
    )
    (profiles_dir / "good.yaml").write_text(
        "id: g\nversion: '1'\ndisplay_name: G\ndescription: ok\ntags: [t]\n"
        "summarize: {prompt: 'Sum {transcript}'}\n"
    )
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.profiles, "path", profiles_dir)

    r = client.get("/profiles")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == ["g"]


def test_profiles_re_scan_returns_new_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per contract: re-scan the directory on every call — no caching."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.profiles, "path", profiles_dir)

    assert client.get("/profiles").json() == []

    (profiles_dir / "late.yaml").write_text(
        "id: late\nversion: '1'\ndisplay_name: Late\ndescription: d\ntags: [t]\n"
        "summarize: {prompt: 'Sum {transcript}'}\n"
    )

    body = client.get("/profiles").json()
    assert [p["id"] for p in body] == ["late"]
