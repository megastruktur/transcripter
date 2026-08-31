"""Vault-side API behavior: audio fallback + DELETE vault sweep.

The export stage moves a done recording's FLAC into the vault
(``<vault>/YYYY/MM/<folder>/.transcripter/audio.flac``); GET /audio must
then serve the vault copy, and DELETE /recordings/{id} must remove the
exported folder (notes + hidden audio + manifest) alongside the catalog
row and storage dir.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

AUTH = {"authorization": "Bearer sekrit"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    import importlib

    from app import main

    importlib.reload(main)
    return TestClient(main.app)


def _make_recording(client: TestClient, title: str = "Meet") -> str:
    r = client.post("/recordings", json={"title": title}, headers=AUTH)
    return r.json()["id"]


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
def _seed_vault_folder(client: TestClient, rid: str, title: str = "Meet") -> Path:
    """Create the recording's vault folder (nested layout) with a fake
    .transcripter/audio.flac, and point cfg.vault at a tmp root."""
    vault = Path(os.environ["TRANSCRIPTER_STORAGE"]).parent / "vault"
    id8 = rid[:8]
    folder = vault / "2026" / "08" / f"2026-08-23_18-45 {title} {id8}"
    hidden = folder / ".transcripter"
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "audio.flac").write_bytes(b"fLaC-vault-copy")
    (folder / "transcript.md").write_text("---\nrecording_id: x\n---\nbody", encoding="utf-8")
    client.app.state.config.vault.path = vault
    return folder


AUTH = {"authorization": "Bearer sekrit"}


def test_audio_served_from_vault_after_move(client: TestClient) -> None:
    rid = _make_recording(client)
    _force_state(rid, "done")
    _seed_vault_folder(client, rid)

    r = client.get(f"/recordings/{rid}/audio", headers=AUTH)

    assert r.status_code == 200
    assert r.content == b"fLaC-vault-copy"


def test_audio_range_from_vault_copy(client: TestClient) -> None:
    rid = _make_recording(client)
    _force_state(rid, "done")
    _seed_vault_folder(client, rid)

    r = client.get(f"/recordings/{rid}/audio", headers={**AUTH, "range": "bytes=5-9"})

    assert r.status_code == 206
    assert r.content == b"vault"


def test_audio_404_when_storage_and_vault_empty(client: TestClient) -> None:
    rid = _make_recording(client)
    _force_state(rid, "done")
    vault = Path(os.environ["TRANSCRIPTER_STORAGE"]).parent / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    client.app.state.config.vault.path = vault

    r = client.get(f"/recordings/{rid}/audio", headers=AUTH)

    assert r.status_code == 404
    assert "vault" in r.json()["detail"]


def test_storage_copy_wins_over_vault(client: TestClient) -> None:
    """Storage first: while the FLAC still sits in /storage (pipeline
    running / move pending), GET must serve it, not the vault copy."""
    rid = _make_recording(client)
    _force_state(rid, "done")
    storage = Path(os.environ["TRANSCRIPTER_STORAGE"])
    audio = storage / "recordings" / rid / "audio.flac"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fLaC-storage-copy")
    _seed_vault_folder(client, rid)

    r = client.get(f"/recordings/{rid}/audio", headers=AUTH)

    assert r.status_code == 200
    assert r.content == b"fLaC-storage-copy"


def test_delete_removes_vault_folder_too(client: TestClient) -> None:
    rid = _make_recording(client)
    _force_state(rid, "done")
    folder = _seed_vault_folder(client, rid)
    storage = Path(os.environ["TRANSCRIPTER_STORAGE"])
    (storage / "recordings" / rid).mkdir(parents=True, exist_ok=True)

    r = client.delete(f"/recordings/{rid}", headers=AUTH)

    assert r.status_code == 204
    assert not folder.exists()
    assert not (storage / "recordings" / rid).exists()
    # The catalog row is gone → subsequent GET 404s.
    assert client.get(f"/recordings/{rid}", headers=AUTH).status_code == 404


def test_delete_sweeps_legacy_flat_folder(client: TestClient) -> None:
    """Pre-vault root-level folders are swept by the same id8 pattern."""
    rid = _make_recording(client)
    _force_state(rid, "done")
    vault = Path(os.environ["TRANSCRIPTER_STORAGE"]).parent / "vault"
    flat = vault / f"2026-08-20_10-00 Old title {rid[:8]}"
    (flat / ".transcripter").mkdir(parents=True)
    (flat / ".transcripter" / "audio.flac").write_bytes(b"old")
    client.app.state.config.vault.path = vault

    r = client.delete(f"/recordings/{rid}", headers=AUTH)

    assert r.status_code == 204
    assert not flat.exists()


def test_delete_never_touches_other_recordings(client: TestClient) -> None:
    rid = _make_recording(client)
    other = _make_recording(client, "Other")
    _force_state(rid, "done")
    folder = _seed_vault_folder(client, rid)
    other_folder = _seed_vault_folder(client, other, "Other")

    client.delete(f"/recordings/{rid}", headers=AUTH)

    assert not folder.exists()
    assert other_folder.exists()


def test_scan_finds_nested_and_legacy(client: TestClient) -> None:
    from app.vault import scan_recording_folders

    rid = _make_recording(client)
    vault = Path(os.environ["TRANSCRIPTER_STORAGE"]).parent / "vault"
    nested = vault / "2026" / "08" / f"2026-08-23_18-45 Meet {rid[:8]}"
    nested.mkdir(parents=True)
    flat = vault / f"2026-08-01_00-00 Old {rid[:8]}"
    flat.mkdir()
    decoy = vault / "2026" / "08" / f"2026-08-23_18-45 Meet {rid[:6]}xx"
    decoy.mkdir(parents=True)
    client.app.state.config.vault.path = vault

    found = scan_recording_folders(client.app.state.config, rid)

    assert sorted(found) == sorted([nested, flat])
