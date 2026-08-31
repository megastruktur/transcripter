"""Vault export: nested YYYY/MM layout, audio move, manifest, dashboard.

The vault feature (2026-08-31): finished recordings export their note folder
nested by capture date, the audio FLAC is MOVED from storage into the
folder's hidden ``.transcripter/`` subdir (copy-verify-unlink), a manifest
lands beside it, and the Dashboard MOC regenerates after every export.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from worker.export import (
    DASHBOARD_NAME,
    HIDDEN_DIR,
    ExportError,
    Rec,
    export_recording,
    folder_name,
    folder_path,
    move_audio_to_vault,
    scan_recording_folders,
    vault_audio_path,
    write_dashboard,
    write_manifest,
)

MOSCOW = ZoneInfo("Europe/Moscow")
REC_ID = "a1b2c3d4-1111-2222-3333-444444444444"
CREATED = datetime(2026, 8, 23, 18, 45, tzinfo=UTC)
RECORDED = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def rec(
    title: str = "",
    duration: float | None = None,
    state: str = "done",
    *,
    recorded_at: datetime | None = None,
    sha256: str | None = None,
    tags: list[str] | None = None,
) -> Rec:
    return Rec(
        REC_ID,
        title,
        CREATED,
        duration,
        state,
        tags=tags or [],
        recorded_at=recorded_at,
        sha256=sha256,
    )


def write_meta(tmp_path: Path, *, transcript: str = "body") -> Path:
    meta = tmp_path / "meta"
    meta.mkdir(exist_ok=True)
    (meta / "transcript.md").write_text(transcript, encoding="utf-8")
    return meta


# ---------- nested layout ----------


class TestNestedLayout:
    def test_folder_path_is_year_month_nested(self, tmp_path):
        path = folder_path(tmp_path, rec("Meet"), UTC)
        assert path == tmp_path / "2026" / "08" / "2026-08-23_18-45 Meet a1b2c3d4"

    def test_recorded_at_groups_and_names(self, tmp_path):
        """An import backdate wins over capture time for BOTH the YYYY/MM
        group and the folder timestamp."""
        path = folder_path(tmp_path, rec("Old call", recorded_at=RECORDED), UTC)
        assert path == tmp_path / "2026" / "07" / "2026-07-01_09-00 Old call a1b2c3d4"

    def test_timezone_applies_to_grouping(self, tmp_path):
        # 18:45 UTC == 21:45 Moscow, next month boundary safe here
        path = folder_path(tmp_path, rec("t"), MOSCOW)
        assert path == tmp_path / "2026" / "08" / "2026-08-23_21-45 t a1b2c3d4"

    def test_export_creates_nested_folder(self, tmp_path):
        meta = write_meta(tmp_path)
        root = tmp_path / "out"
        path = export_recording(root, meta, rec("Meet"), UTC)
        assert path == root / "2026" / "08" / "2026-08-23_18-45 Meet a1b2c3d4"
        assert (path / "transcript.md").is_file()

    def test_legacy_flat_folder_migrated_into_nested(self, tmp_path):
        """A pre-vault root-level folder is found and moved into YYYY/MM."""
        meta = write_meta(tmp_path)
        root = tmp_path / "out"
        legacy = root / "2026-08-23_18-45 Meet a1b2c3d4"
        legacy.mkdir(parents=True)
        (legacy / "user-notes.md").write_text("mine", encoding="utf-8")

        path = export_recording(root, meta, rec("Meet"), UTC)

        assert path == root / "2026" / "08" / "2026-08-23_18-45 Meet a1b2c3d4"
        assert not legacy.exists()
        # User content travelled with the folder.
        assert (path / "user-notes.md").read_text(encoding="utf-8") == "mine"


class TestScanRecordingFolders:
    def test_finds_nested_and_flat(self, tmp_path):
        nested = tmp_path / "2026" / "08" / "2026-08-23_18-45 Meet a1b2c3d4"
        nested.mkdir(parents=True)
        flat = tmp_path / "2026-08-23_18-45 Other title a1b2c3d4"
        flat.mkdir()
        decoy = tmp_path / "2025" / "09" / "2025-09-09_09-09 Meet ffff0000"
        decoy.mkdir(parents=True)

        found = scan_recording_folders(tmp_path, rec())

        # Path ordering is component-wise: '…/2026/…' sorts before
        # '…/2026-08-23…' (dir name '2026' is a prefix of the flat name).
        assert found == [nested, flat]

    def test_empty_vault_root(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        assert scan_recording_folders(tmp_path / "nowhere", rec()) == []

    def test_id8_only_match_ignores_title_and_date(self, tmp_path):
        """recorded_at edits move the folder across YYYY/MM — the scan must
        still find the old-location folder by id8."""
        old = tmp_path / "2026" / "07" / "2026-07-01_09-00 Old title a1b2c3d4"
        old.mkdir(parents=True)
        found = scan_recording_folders(tmp_path, rec(recorded_at=RECORDED))
        assert found == [old]


# ---------- audio move ----------


class TestMoveAudio:
    def _flac(self, path: Path, payload: bytes = b"fLaC-fake-audio") -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def test_moves_and_verifies(self, tmp_path):
        storage = tmp_path / "storage" / REC_ID / "audio.flac"
        sha = self._flac(storage)
        folder = tmp_path / "out" / "2026" / "08" / "2026-08-23_18-45 Meet a1b2c3d4"

        dst = move_audio_to_vault(storage, folder, rec("Meet", sha256=sha))

        assert dst.is_file()
        assert dst.read_bytes() == b"fLaC-fake-audio"
        assert not storage.exists()  # the move, not a copy
        assert not list(dst.parent.glob("*.tmp"))  # no staging leftovers

    def test_idempotent_when_already_moved(self, tmp_path):
        storage = tmp_path / "storage" / REC_ID / "audio.flac"
        sha = self._flac(storage)
        folder = tmp_path / "f"
        move_audio_to_vault(storage, folder, rec(sha256=sha))

        second = move_audio_to_vault(storage, folder, rec(sha256=sha))

        assert second == vault_audio_path(folder)
        assert vault_audio_path(folder).read_bytes() == b"fLaC-fake-audio"

    def test_no_storage_copy_returns_none(self, tmp_path):
        assert move_audio_to_vault(tmp_path / "gone.flac", tmp_path / "f", rec()) is None

    def test_sha_mismatch_keeps_storage_copy(self, tmp_path):
        storage = tmp_path / "storage" / REC_ID / "audio.flac"
        self._flac(storage, b"actual-bytes")
        folder = tmp_path / "f"
        bad = "0" * 64

        with pytest.raises(ExportError, match="sha256"):
            move_audio_to_vault(storage, folder, rec(sha256=bad))

        assert storage.read_bytes() == b"actual-bytes"  # untouched
        assert not vault_audio_path(folder).exists()  # nothing landed

    def test_no_hash_falls_back_to_size_check(self, tmp_path):
        storage = tmp_path / "storage" / REC_ID / "audio.flac"
        self._flac(storage)
        folder = tmp_path / "f"
        assert move_audio_to_vault(storage, folder, rec()) == vault_audio_path(folder)

    def test_export_recording_moves_audio_and_writes_manifest(self, tmp_path):
        meta = write_meta(tmp_path)
        storage = tmp_path / "recordings" / REC_ID / "audio.flac"
        sha = self._flac(storage)
        root = tmp_path / "out"

        path = export_recording(
            root, meta, rec("Meet", 120.0, sha256=sha, tags=["standup"]), UTC,
            audio_src=storage,
        )

        hidden = path / HIDDEN_DIR
        audio = hidden / "audio.flac"
        manifest = hidden / "manifest.json"
        assert audio.is_file() and not storage.exists()
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        assert doc["id"] == REC_ID
        assert doc["sha256"] == sha
        assert doc["title"] == "Meet"
        assert doc["duration_sec"] == 120.0
        assert doc["tags"] == ["standup"]
        assert doc["audio"] == "audio.flac"

    def test_export_without_audio_src_leaves_folder_plain(self, tmp_path):
        meta = write_meta(tmp_path)
        path = export_recording(tmp_path / "out", meta, rec("Meet"), UTC)
        assert not (path / HIDDEN_DIR).exists()


# ---------- manifest ----------


class TestManifest:
    def test_refreshed_on_every_write(self, tmp_path):
        folder = tmp_path / "f"
        write_manifest(folder, rec("First", tags=["a"]), UTC)
        write_manifest(folder, rec("Second", tags=["b"]), UTC)
        doc = json.loads((folder / HIDDEN_DIR / "manifest.json").read_text(encoding="utf-8"))
        assert doc["title"] == "Second"
        assert doc["tags"] == ["b"]
        assert doc["recorded_at"] is None

    def test_recorded_at_stamped(self, tmp_path):
        folder = tmp_path / "f"
        write_manifest(folder, rec("Back", recorded_at=RECORDED), UTC)
        doc = json.loads((folder / HIDDEN_DIR / "manifest.json").read_text(encoding="utf-8"))
        assert doc["recorded_at"].startswith("2026-07-01T09:00:00")


# ---------- dashboard ----------


class TestDashboard:
    def test_months_and_tags_sections(self, tmp_path):
        recs = [
            rec("August call", tags=["work"]),
            rec("July call", recorded_at=RECORDED, tags=["work", "fun"]),
        ]
        path = write_dashboard(tmp_path, recs, UTC)
        assert path == tmp_path / DASHBOARD_NAME
        body = path.read_text(encoding="utf-8")
        assert "## 2026/08" in body and "## 2026/07" in body
        assert "### #work" in body and "### #fun" in body
        august = folder_name("August call", REC_ID, CREATED, UTC)
        assert f"[[{august}/{august}|August call]]" in body

    def test_empty_recs_noop(self, tmp_path):
        assert write_dashboard(tmp_path, [], UTC) is None
        assert not (tmp_path / DASHBOARD_NAME).exists()

    def test_untagged_grouped(self, tmp_path):
        path = write_dashboard(tmp_path, [rec("Bare")], UTC)
        assert "### #untagged" in path.read_text(encoding="utf-8")
