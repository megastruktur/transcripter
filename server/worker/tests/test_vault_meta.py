"""Vault-canonical meta: the export moves the WHOLE meta tree into the
vault mirror (``<folder>/.transcripter/meta/``) and storage becomes
scratch; ``resolve_meta_dir`` finds the live tree (storage first, vault
mirror second); ``rehydrate_meta`` pulls the tree back for a regenerate.

Companion to test_vault_export.py (layout/audio move) — this file pins
the v0.18.0 durable-home semantics.
"""
from datetime import UTC, datetime
from types import SimpleNamespace

from worker.export import (
    move_meta_to_vault,
    resolve_meta_dir,
    vault_meta_dir,
)

REC_ID = "a1b2c3d4-1111-2222-3333-444444444444"
CREATED = datetime(2026, 8, 26, 12, 24, tzinfo=UTC)


def _cfg(tmp_path, mode="vault"):
    """Worker-config-ish namespace with storage + vault roots."""
    return SimpleNamespace(
        recordings_root=tmp_path / "recordings",
        vault=SimpleNamespace(path=tmp_path / "vault", mode=mode),
    )


def _seed_storage_meta(c, files=("transcript.md", "summary.md", "events.json")):
    meta = c.recordings_root / REC_ID / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    for name in files:
        (meta / name).write_text(f"content of {name}", encoding="utf-8")
    return meta


def _seed_vault_folder(c, with_meta=None):
    """An exported folder (deterministic name shape) under the vault."""
    folder = c.vault.path / "2026" / "08" / "2026-08-26_12-24 Daily Blob a1b2c3d4"
    folder.mkdir(parents=True, exist_ok=True)
    if with_meta:
        mirror = vault_meta_dir(folder)
        for name, body in with_meta.items():
            dst = mirror / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(body, encoding="utf-8")
    return folder


class TestMoveMetaToVault:
    def test_moves_all_files_and_empties_storage(self, tmp_path):
        c = _cfg(tmp_path)
        meta = _seed_storage_meta(c)
        folder = _seed_vault_folder(c)
        moved = move_meta_to_vault(meta, folder)

        assert moved == 3
        mirror = vault_meta_dir(folder)
        for name in ("transcript.md", "summary.md", "events.json"):
            assert (mirror / name).read_text(encoding="utf-8") == f"content of {name}"
            assert not (meta / name).exists()
        # The emptied meta dir (and its parent with audio gone) is pruned.
        assert not meta.exists()

    def test_nested_subdirs_mirror(self, tmp_path):
        c = _cfg(tmp_path)
        meta = _seed_storage_meta(c, files=())
        (meta / "chunks" / "chunk_000.flac").parent.mkdir(parents=True)
        (meta / "chunks" / "chunk_000.flac").write_bytes(b"flac")
        folder = _seed_vault_folder(c)

        move_meta_to_vault(meta, folder)

        assert (vault_meta_dir(folder) / "chunks" / "chunk_000.flac").read_bytes() == b"flac"
        assert not meta.exists()

    def test_idempotent_partial_move_completes(self, tmp_path):
        """A previous run that copied but crashed before unlink: the pair
        (same size) completes the unlink without re-copying."""
        c = _cfg(tmp_path)
        meta = _seed_storage_meta(c, files=("transcript.md",))
        folder = _seed_vault_folder(c)
        mirror = vault_meta_dir(folder)
        mirror.mkdir(parents=True, exist_ok=True)
        (mirror / "transcript.md").write_text("content of transcript.md", encoding="utf-8")

        moved = move_meta_to_vault(meta, folder)

        assert moved == 0  # nothing NEW copied
        assert not (meta / "transcript.md").exists()

    def test_size_mismatch_recopies(self, tmp_path):
        """A stale/different vault copy is NOT trusted as already-moved —
        the storage copy wins and the mirror is refreshed."""
        c = _cfg(tmp_path)
        meta = _seed_storage_meta(c, files=("transcript.md",))
        folder = _seed_vault_folder(c)
        mirror = vault_meta_dir(folder)
        mirror.mkdir(parents=True, exist_ok=True)
        (mirror / "transcript.md").write_text("DIFFERENT stale body", encoding="utf-8")

        moved = move_meta_to_vault(meta, folder)

        assert moved == 1
        assert (mirror / "transcript.md").read_text(encoding="utf-8") == "content of transcript.md"


class TestResolveMetaDir:
    def test_storage_wins_when_populated(self, tmp_path):
        c = _cfg(tmp_path)
        meta = _seed_storage_meta(c)
        _seed_vault_folder(c, with_meta={"transcript.md": "vault copy"})

        assert resolve_meta_dir(c, REC_ID) == meta

    def test_vault_mirror_when_storage_empty(self, tmp_path):
        c = _cfg(tmp_path)
        _seed_storage_meta(c, files=())  # empty storage meta
        folder = _seed_vault_folder(c, with_meta={"transcript.md": "vault copy"})

        assert resolve_meta_dir(c, REC_ID) == vault_meta_dir(folder)

    def test_storage_returned_when_no_vault_folder(self, tmp_path):
        c = _cfg(tmp_path)
        meta = _seed_storage_meta(c, files=())  # neither side has content

        assert resolve_meta_dir(c, REC_ID) == meta

    def test_storage_mode_never_scans_vault(self, tmp_path):
        """mode='storage' (legacy / no VAULT_DIR): the resolver must not
        even look at the vault path — meta stays a storage concept."""
        c = _cfg(tmp_path, mode="storage")
        meta = _seed_storage_meta(c, files=())
        _seed_vault_folder(c, with_meta={"transcript.md": "vault copy"})

        assert resolve_meta_dir(c, REC_ID) == meta


class TestRehydrateMeta:
    def test_pulls_tree_from_vault_mirror(self, tmp_path, monkeypatch):
        from worker import activities

        c = _cfg(tmp_path)
        _seed_vault_folder(
            c, with_meta={"transcript.md": "t", "summary.md": "s", "events.json": "e"}
        )
        monkeypatch.setattr(activities, "_cfg", c)

        res = activities.rehydrate_meta(REC_ID)

        meta = c.recordings_root / REC_ID / "meta"
        assert res == {"rehydrated": 3}
        assert (meta / "transcript.md").read_text(encoding="utf-8") == "t"
        assert (meta / "summary.md").read_text(encoding="utf-8") == "s"
        # Vault mirror untouched — the vault stays the durable home.
        assert (c.vault.path / "2026" / "08").exists()

    def test_noop_in_storage_mode(self, tmp_path, monkeypatch):
        from worker import activities

        c = _cfg(tmp_path, mode="storage")
        monkeypatch.setattr(activities, "_cfg", c)

        assert activities.rehydrate_meta(REC_ID) == {"rehydrated": 0}

    def test_noop_when_storage_already_populated(self, tmp_path, monkeypatch):
        from worker import activities

        c = _cfg(tmp_path)
        meta = _seed_storage_meta(c)  # storage has the tree
        _seed_vault_folder(c, with_meta={"transcript.md": "vault copy"})
        monkeypatch.setattr(activities, "_cfg", c)

        assert activities.rehydrate_meta(REC_ID) == {"rehydrated": 0}
        # Storage copy NOT overwritten by the vault copy.
        assert (meta / "transcript.md").read_text(encoding="utf-8") == "content of transcript.md"
