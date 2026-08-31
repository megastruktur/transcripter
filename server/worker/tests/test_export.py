"""Export module: deterministic naming, safe frontmatter, atomic writes."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml

from worker.export import (
    ARTIFACTS,
    ExportError,
    Rec,
    build_artifact,
    configured_zone,
    export_recording,
    folder_name,
    folder_path,
    run,
    write_note_atomic,
)

MOSCOW = ZoneInfo("Europe/Moscow")
REC_ID = "a1b2c3d4-1111-2222-3333-444444444444"
CREATED = datetime(2026, 8, 23, 18, 45, tzinfo=UTC)


def rec(title: str = "", duration: float | None = None, state: str = "done") -> Rec:
    return Rec(REC_ID, title, CREATED, duration, state)


class TestFolderName:
    def test_title(self):
        assert folder_name("Standup", REC_ID, CREATED, UTC) == "2026-08-23_18-45 Standup a1b2c3d4"

    def test_empty_title_becomes_call(self):
        assert folder_name("", REC_ID, CREATED, UTC) == "2026-08-23_18-45 call a1b2c3d4"

    def test_whitespace_only_title(self):
        assert folder_name("   ", REC_ID, CREATED, UTC) == "2026-08-23_18-45 call a1b2c3d4"

    def test_dot_only_title_is_not_a_dotfile(self):
        name = folder_name(". ", REC_ID, CREATED, UTC)
        assert name == "2026-08-23_18-45 call a1b2c3d4"
        assert not Path(name).name.startswith(".")

    def test_illegal_chars_replaced(self):
        name = folder_name('a/b\\c:d*e?f"g<h>i|j#k[l]m^n', REC_ID, CREATED, UTC)
        assert "/" not in name and ":" not in name
        assert "#" not in name and "[" not in name and "]" not in name and "^" not in name
        assert name.endswith(" a1b2c3d4")
        assert ".md" not in name  # folder, not file

    def test_control_chars_stripped(self):
        name = folder_name("a\tb\nc\x00d", REC_ID, CREATED, UTC)
        assert "\t" not in name and "\n" not in name and "\x00" not in name

    def test_leading_trailing_dots_and_spaces_stripped(self):
        assert folder_name("  .call. ", REC_ID, CREATED, UTC) == "2026-08-23_18-45 call a1b2c3d4"

    def test_unique_for_same_minute_same_title(self):
        # id8 differs => different names (the real anti-collision property)
        other = "ffff0000-9999-8888-7777-666666666666"
        assert folder_name("Standup", REC_ID, CREATED, UTC) != folder_name(
            "Standup", other, CREATED, UTC
        )
        # Same recording: deterministic (regenerate overwrites, never forks)
        assert folder_name("Standup", REC_ID, CREATED, UTC) == folder_name(
            "Standup", REC_ID, CREATED, UTC
        )

    def test_timezone_converted(self):
        # 18:45 UTC == 21:45 Moscow
        assert folder_name("t", REC_ID, CREATED, MOSCOW).startswith("2026-08-23_21-45 ")

    def test_long_cyrillic_title_byte_capped(self):
        title = "Д" * 300  # 2 bytes/char
        name = folder_name(title, REC_ID, CREATED, UTC)
        encoded = name.encode()
        assert len(encoded) <= 240, len(encoded)
        assert encoded.endswith(b" a1b2c3d4")
        assert b".md" not in encoded  # folder, not file
        # truncated on a char boundary: decodable without loss
        encoded.decode()  # no UnicodeDecodeError
        assert name.count("Д") >= 100  # kept most of the title


class TestZone:
    def test_default_utc(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIPTER_TZ", raising=False)
        assert configured_zone() == ZoneInfo("UTC")

    def test_env_zone(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIPTER_TZ", "Europe/Moscow")
        assert configured_zone() == MOSCOW

    def test_invalid_zone_raises(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIPTER_TZ", "Not/AZone")
        with pytest.raises(ExportError, match="TRANSCRIPTER_TZ"):
            configured_zone()


class TestBuildArtifact:
    def write_meta(self, tmp_path: Path, *, diarized=False, summary=False, transcript=True):
        meta = tmp_path / "meta"
        meta.mkdir()
        if transcript:
            (meta / "transcript.md").write_text(
                "# Transcript (ru)\n\nplain text", encoding="utf-8"
            )
        if diarized:
            (meta / "diarized-transcript.md").write_text(
                "# Diarized transcript\n\n**SPEAKER_00:** hi", encoding="utf-8"
            )
        if summary:
            (meta / "summary.md").write_text("key points", encoding="utf-8")
        return meta

    def parse_fm(self, note: str) -> dict:
        assert note.startswith("---\n")
        fm = note.split("---\n", 2)[1]
        return yaml.safe_load(fm)

    def test_frontmatter_fields(self, tmp_path):
        meta = self.write_meta(tmp_path)
        r = rec("Разговор", 123.5)
        artifact = meta / "transcript.md"
        note = build_artifact(artifact, r, MOSCOW)
        fm = self.parse_fm(note)
        assert fm["recording_id"] == REC_ID
        assert fm["title"] == "Разговор"
        assert fm["created"] == "2026-08-23T21:45:00+03:00"
        assert fm["date"] == "2026-08-23"
        assert fm["duration_sec"] == 123.5
        assert fm["tags"] == ["transcripter/call"]

    def test_duration_null_omitted(self, tmp_path):
        meta = self.write_meta(tmp_path)
        artifact = meta / "transcript.md"
        fm = self.parse_fm(build_artifact(artifact, rec("t"), UTC))
        assert "duration_sec" not in fm

    def test_hostile_title_safe_yaml(self, tmp_path):
        meta = self.write_meta(tmp_path)
        hostile = 'a: b "[c] {d} #e'
        artifact = meta / "transcript.md"
        fm = self.parse_fm(build_artifact(artifact, rec(hostile), UTC))
        assert fm["title"] == hostile  # round-trips through YAML intact

    def test_body_is_raw_artifact_no_section_headers(self, tmp_path):
        """Each artifact file is its own note: frontmatter + the raw artifact
        body, with no cross-cutting `## Summary` / `## Transcript` headings."""
        meta = self.write_meta(tmp_path, summary=True)
        artifact = meta / "summary.md"
        note = build_artifact(artifact, rec("t"), UTC)
        # Body is verbatim what was in the source artifact.
        body = note.split("---\n", 2)[2]
        assert body.lstrip("\n").startswith("key points")
        assert "## Summary" not in note
        assert "## Transcript" not in note

    def test_per_artifact_body_matches_source(self, tmp_path):
        """Each artifact file gets only its own content, never concatenated."""
        meta = self.write_meta(tmp_path, transcript=True, summary=True)
        diarized_note = build_artifact(meta / "transcript.md", rec("t"), UTC)
        summary_note = build_artifact(meta / "summary.md", rec("t"), UTC)
        assert "plain text" in diarized_note
        assert "key points" in summary_note
        assert "plain text" not in summary_note
        assert "key points" not in diarized_note


class TestWriteAtomic:
    def test_double_run_one_file(self, tmp_path):
        path = tmp_path / "note.md"
        write_note_atomic(path, "v1")
        write_note_atomic(path, "v2")
        assert path.read_text() == "v2"
        visible = [p.name for p in tmp_path.iterdir() if not p.name.startswith(".")]
        assert visible == ["note.md"]

    def test_no_tmp_left_behind_on_error(self, tmp_path, monkeypatch):
        path = tmp_path / "note.md"

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", boom)
        with pytest.raises(OSError):
            write_note_atomic(path, "x")
        monkeypatch.undo()
        leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert not leftovers


class TestExportRecording:
    def test_no_artifacts_noop(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        assert export_recording(tmp_path / "out", meta, rec(), UTC) is None
    def test_fresh_root_created_on_first_export(self, tmp_path):
        """TRANSCRIPTS_DIR unset → ./storage/transcripts may not exist yet;
        the first export ever must not FileNotFoundError on the rename-scan."""
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        root = tmp_path / "out"  # deliberately never mkdir'd
        path = export_recording(root, meta, rec("Meet"), UTC)
        assert path is not None and path.is_dir()

    def test_exports_folder_with_only_existing_artifacts(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        root = tmp_path / "out"
        path = export_recording(root, meta, rec("Meet"), UTC)
        expected = folder_path(root, rec("Meet"), UTC)
        assert path == expected
        assert path.is_dir()
        # Only transcript.md (no diarized-transcript.md / summary.md / extras).
        # .transcript.md.lock lives beside it but is hidden (dotfile).
        visible = sorted(p.name for p in path.iterdir() if not p.name.startswith("."))

        assert visible == ["transcript.md"]
        # Mirror-side lockfile lives next to it.
        assert (path / ".transcript.md.lock").exists()
        assert "body" in (path / "transcript.md").read_text(encoding="utf-8")
        # ARTIFACTS that were absent from meta must not appear in the folder.
        for missing in ARTIFACTS:
            if missing == "transcript.md":
                continue
            assert not (path / missing).exists()

    def test_sentinel_missing_refuses(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        root = tmp_path / "out"
        root.mkdir()
        with pytest.raises(ExportError, match="sentinel"):
            export_recording(root, meta, rec(), UTC, sentinel=".obsidian")

    def test_sentinel_present_allows(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        root = tmp_path / "out"
        root.mkdir()
        (root / ".obsidian").mkdir()
        assert export_recording(root, meta, rec(), UTC, sentinel=".obsidian") is not None


class TestFolderRename:
    def test_old_title_folder_renamed_user_files_preserved(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("new body", encoding="utf-8")

        root = tmp_path / "out"
        old = root / folder_name("Old title", REC_ID, CREATED, UTC)
        old.mkdir(parents=True)
        # Stale artifact from prior export under the old title.
        (old / "transcript.md").write_text("old body", encoding="utf-8")
        (old / ".transcript.md.lock").write_text("", encoding="utf-8")
        # A user-authored file must survive the rename.
        user = old / "user-notes.md"
        user.write_text("mine", encoding="utf-8")
        user_lock = old / ".user-notes.md.lock"
        user_lock.write_text("", encoding="utf-8")

        path = export_recording(root, meta, rec("New title"), UTC)

        new = folder_path(root, rec("New title"), UTC)
        assert path == new
        assert path.is_dir()
        # Old folder name no longer present at root.
        assert not old.exists()
        # New folder has refreshed artifact content.
        assert (new / "transcript.md").read_text(encoding="utf-8").startswith("---")
        assert "new body" in (new / "transcript.md").read_text(encoding="utf-8")
        # User file survived intact under the renamed folder.
        assert (new / "user-notes.md").read_text(encoding="utf-8") == "mine"
        assert (new / ".user-notes.md.lock").exists()


class TestMirrorDelete:
    def test_dropped_artifact_removed_user_files_kept(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        # Prior export wrote summary.md to the folder; this regenerate only
        # has transcript.md in meta. summary.md must be cleaned up.
        folder = folder_path(tmp_path / "out", rec("Meet"), UTC)
        folder.mkdir(parents=True)
        (folder / "transcript.md").write_text("stale body", encoding="utf-8")
        (folder / ".transcript.md.lock").write_text("", encoding="utf-8")
        (folder / "summary.md").write_text("stale summary", encoding="utf-8")
        (folder / ".summary.md.lock").write_text("", encoding="utf-8")
        # An unknown user file must NOT be touched by mirror-delete.
        user = folder / "scratch.md"
        user.write_text("mine", encoding="utf-8")

        # meta now has only transcript.md
        (meta / "transcript.md").write_text("fresh body", encoding="utf-8")

        path = export_recording(tmp_path / "out", meta, rec("Meet"), UTC)
        assert path == folder
        assert (folder / "transcript.md").exists()
        assert (folder / ".transcript.md.lock").exists()
        assert not (folder / "summary.md").exists()
        # The lockfile stays — write_note_atomic's permanence invariant:
        # unlinking a locked inode would let two writers flock different inodes.
        assert (folder / ".summary.md.lock").exists()
        # Unknown user files untouched.
        assert (folder / "scratch.md").read_text(encoding="utf-8") == "mine"


class TestFlockConcurrency:
    def test_parallel_writes_produce_one_valid_file(self, tmp_path):
        import concurrent.futures

        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        root = tmp_path / "out"
        root.mkdir()
        path = folder_path(root, rec("Meet"), UTC) / "transcript.md"
        path.parent.mkdir(parents=True)

        def write(i):
            write_note_atomic(path, f"content-{i}")
            return i

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            assert sorted(ex.map(write, range(16))) == list(range(16))
        # Inside the folder: exactly the artifact + its lock sibling (no .tmp).
        files = sorted(p.name for p in path.parent.iterdir() if not p.name.startswith("."))
        assert files == ["transcript.md"]
        assert path.read_text().startswith("content-")


class TestLegacyMigration:
    """run() removes stale app-scheme flat notes (old titles) but never user notes."""

    def stub_run(self, tmp_path, monkeypatch, r: Rec) -> Path:
        """Point run()'s config/DB at tmp_path; returns the transcripts root."""
        root = tmp_path / "notes"
        root.mkdir()
        meta = tmp_path / "recordings" / r.id / "meta"
        meta.mkdir(parents=True)
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        cfg = SimpleNamespace(
            database=SimpleNamespace(url="sqlite://"),
            recordings_root=tmp_path / "recordings",
            vault=SimpleNamespace(path=root, sentinel=""),
        )
        monkeypatch.setattr("worker.config.load_config", lambda: cfg)
        monkeypatch.setattr("worker.db.init_engine", lambda url: None)
        monkeypatch.setattr("worker.export.load_recording", lambda rec_id: r)
        monkeypatch.delenv("TRANSCRIPTER_TZ", raising=False)  # deterministic UTC
        return root

    def test_stale_app_scheme_flat_note_and_lock_removed(self, tmp_path, monkeypatch):
        root = self.stub_run(tmp_path, monkeypatch, rec("New title"))
        stale = root / "2026-01-01_10-00 Old title a1b2c3d4.md"
        stale.write_text("old", encoding="utf-8")
        lock = root / f".{stale.name}.lock"
        lock.write_text("", encoding="utf-8")

        path = run(REC_ID)

        assert path is not None
        assert not stale.exists()
        assert not lock.exists()
        # Folder path is the deterministic folder (no .md).
        assert path == folder_path(root, rec("New title"), UTC)
        assert path.is_dir()
        # Folder was populated by export_recording before the sweep ran.
        assert (path / "transcript.md").exists()

    def test_user_flat_note_without_timestamp_prefix_survives(self, tmp_path, monkeypatch):
        root = self.stub_run(tmp_path, monkeypatch, rec("New title"))
        user = root / "My standup a1b2c3d4.md"
        user.write_text("mine", encoding="utf-8")

        run(REC_ID)

        assert user.read_text(encoding="utf-8") == "mine"

class TestRenameOnly:
    """run(rename_only=True): folder renamed, files NOT rewritten."""

    def stub_run(self, tmp_path, monkeypatch, r: Rec) -> Path:
        root = tmp_path / "notes"
        root.mkdir()
        meta = tmp_path / "recordings" / r.id / "meta"
        meta.mkdir(parents=True)
        (meta / "transcript.md").write_text("fresh body", encoding="utf-8")
        cfg = SimpleNamespace(
            database=SimpleNamespace(url="sqlite://"),
            recordings_root=tmp_path / "recordings",
            vault=SimpleNamespace(path=root, sentinel=""),
        )
        monkeypatch.setattr("worker.config.load_config", lambda: cfg)
        monkeypatch.setattr("worker.db.init_engine", lambda url: None)
        monkeypatch.setattr("worker.export.load_recording", lambda rec_id: r)
        monkeypatch.delenv("TRANSCRIPTER_TZ", raising=False)
        return root

    def test_folder_renamed_files_untouched(self, tmp_path, monkeypatch):
        root = self.stub_run(tmp_path, monkeypatch, rec("New title"))
        old = root / folder_name("Old title", REC_ID, CREATED, UTC)
        old.mkdir()
        edited = old / "transcript.md"
        edited.write_text("USER EDIT", encoding="utf-8")

        path = run(REC_ID, rename_only=True)

        assert path == folder_path(root, rec("New title"), UTC)
        # File content NOT refreshed from meta ("fresh body") — edit survives.
        assert (path / "transcript.md").read_text(encoding="utf-8") == "USER EDIT"
        assert not old.exists()

    def test_no_folder_noop(self, tmp_path, monkeypatch):
        self.stub_run(tmp_path, monkeypatch, rec("New title"))
        assert run(REC_ID, rename_only=True) is None


class TestOrphanFolderSweep:
    """Old-title folders orphaned by a double-rename race are swept — but
    only when they contain nothing user-authored."""

    def stub_run(self, tmp_path, monkeypatch, r: Rec) -> Path:
        root = tmp_path / "notes"
        root.mkdir()
        meta = tmp_path / "recordings" / r.id / "meta"
        meta.mkdir(parents=True)
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        cfg = SimpleNamespace(
            database=SimpleNamespace(url="sqlite://"),
            recordings_root=tmp_path / "recordings",
            vault=SimpleNamespace(path=root, sentinel=""),
        )
        monkeypatch.setattr("worker.config.load_config", lambda: cfg)
        monkeypatch.setattr("worker.db.init_engine", lambda url: None)
        monkeypatch.setattr("worker.export.load_recording", lambda rec_id: r)
        monkeypatch.delenv("TRANSCRIPTER_TZ", raising=False)
        return root

    def test_app_only_orphan_folder_removed(self, tmp_path, monkeypatch):
        root = self.stub_run(tmp_path, monkeypatch, rec("New title"))
        # Two stale folders (double-rename race): the move-scan moves the
        # first match; the sweep must remove the remaining app-only one.
        first = root / folder_name("First title", REC_ID, CREATED, UTC)
        first.mkdir()
        (first / "transcript.md").write_text("stale", encoding="utf-8")
        orphan = root / folder_name("Race loser", REC_ID, CREATED, UTC)
        orphan.mkdir()
        (orphan / "transcript.md").write_text("stale", encoding="utf-8")
        (orphan / ".transcript.md.lock").write_text("", encoding="utf-8")

        path = run(REC_ID)

        assert path is not None and path.is_dir()
        # Exactly one folder for this recording survives — the moved one.
        from worker.export import scan_recording_folders

        assert scan_recording_folders(root, rec("New title")) == [path]
        assert not first.exists() and not orphan.exists()

    def test_orphan_folder_with_user_file_survives(self, tmp_path, monkeypatch, caplog):
        root = self.stub_run(tmp_path, monkeypatch, rec("New title"))
        # Double-rename race leftover: title2 lost the move-scan race to
        # title3 and was created fresh by the second exporter.
        loser = root / folder_name("Race loser", REC_ID, CREATED, UTC)
        loser.mkdir()
        (loser / "transcript.md").write_text("stale", encoding="utf-8")
        # A second, older folder WITH user content: the move-scan must pick
        # THIS one (move-first preserves user edits), leaving the app-only
        # loser for the sweep.
        edited = root / folder_name("Edited title", REC_ID, CREATED, UTC)
        edited.mkdir()
        (edited / "my-notes.md").write_text("mine", encoding="utf-8")

        with caplog.at_level("WARNING", logger="transcripter.export"):
            path = run(REC_ID)

        assert path is not None and path.is_dir()
        # The user-content folder was moved in place — edits preserved.
        assert (path / "my-notes.md").read_text(encoding="utf-8") == "mine"
        # The app-only race loser was swept.
        assert not loser.exists()
