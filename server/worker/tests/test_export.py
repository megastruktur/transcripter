"""Export module: deterministic naming, safe frontmatter, atomic writes."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from worker.export import (
    ExportError,
    Rec,
    build_note,
    configured_zone,
    export_recording,
    note_name,
    note_path,
    write_note_atomic,
)

MOSCOW = ZoneInfo("Europe/Moscow")
REC_ID = "a1b2c3d4-1111-2222-3333-444444444444"
CREATED = datetime(2026, 8, 23, 18, 45, tzinfo=UTC)


def rec(title: str = "", duration: float | None = None, state: str = "done") -> Rec:
    return Rec(REC_ID, title, CREATED, duration, state)


class TestNoteName:
    def test_title(self):
        assert note_name("Standup", REC_ID, CREATED, UTC) == "2026-08-23_18-45 Standup a1b2c3d4.md"

    def test_empty_title_becomes_call(self):
        assert note_name("", REC_ID, CREATED, UTC) == "2026-08-23_18-45 call a1b2c3d4.md"

    def test_whitespace_only_title(self):
        assert note_name("   ", REC_ID, CREATED, UTC) == "2026-08-23_18-45 call a1b2c3d4.md"

    def test_dot_only_title_is_not_a_dotfile(self):
        name = note_name(". ", REC_ID, CREATED, UTC)
        assert name == "2026-08-23_18-45 call a1b2c3d4.md"
        assert not Path(name).name.startswith(".")

    def test_illegal_chars_replaced(self):
        name = note_name('a/b\\c:d*e?f"g<h>i|j#k[l]m^n', REC_ID, CREATED, UTC)
        assert "/" not in name and ":" not in name
        assert "#" not in name and "[" not in name and "]" not in name and "^" not in name
        assert name.endswith(" a1b2c3d4.md")

    def test_control_chars_stripped(self):
        name = note_name("a\tb\nc\x00d", REC_ID, CREATED, UTC)
        assert "\t" not in name and "\n" not in name and "\x00" not in name

    def test_leading_trailing_dots_and_spaces_stripped(self):
        assert note_name("  .call. ", REC_ID, CREATED, UTC) == "2026-08-23_18-45 call a1b2c3d4.md"

    def test_unique_for_same_minute_same_title(self):
        # id8 differs => different names (the real anti-collision property)
        other = "ffff0000-9999-8888-7777-666666666666"
        assert note_name("Standup", REC_ID, CREATED, UTC) != note_name("Standup", other, CREATED, UTC)
        # Same recording: deterministic (regenerate overwrites, never forks)
        assert note_name("Standup", REC_ID, CREATED, UTC) == note_name("Standup", REC_ID, CREATED, UTC)

    def test_timezone_converted(self):
        # 18:45 UTC == 21:45 Moscow
        assert note_name("t", REC_ID, CREATED, MOSCOW).startswith("2026-08-23_21-45 ")

    def test_long_cyrillic_title_byte_capped(self):
        title = "Д" * 300  # 2 bytes/char
        name = note_name(title, REC_ID, CREATED, UTC)
        encoded = name.encode()
        assert len(encoded) <= 240, len(encoded)
        assert encoded.endswith(b" a1b2c3d4.md")
        # truncated on a char boundary: decodable without loss
        name.encode().decode()  # no UnicodeDecodeError
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


class TestBuildNote:
    def make_meta(self, tmp_path: Path, *, diarized=False, summary=False, transcript=True):
        meta = tmp_path / "meta"
        meta.mkdir()
        if transcript:
            (meta / "transcript.md").write_text("# Transcript (ru)\n\nplain text", encoding="utf-8")
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
        meta = self.make_meta(tmp_path)
        note = build_note(meta, rec("Разговор", 123.5), MOSCOW)
        fm = self.parse_fm(note)
        assert fm["recording_id"] == REC_ID
        assert fm["title"] == "Разговор"
        assert fm["created"] == "2026-08-23T21:45:00+03:00"
        assert fm["date"] == "2026-08-23"
        assert fm["duration_sec"] == 123.5
        assert fm["tags"] == ["transcripter/call"]

    def test_duration_null_omitted(self, tmp_path):
        meta = self.make_meta(tmp_path)
        fm = self.parse_fm(build_note(meta, rec("t"), UTC))
        assert "duration_sec" not in fm

    def test_hostile_title_safe_yaml(self, tmp_path):
        meta = self.make_meta(tmp_path)
        hostile = 'a: b "[c] {d} #e'
        fm = self.parse_fm(build_note(meta, rec(hostile), UTC))
        assert fm["title"] == hostile  # round-trips through YAML intact

    def test_summary_and_transcript_sections(self, tmp_path):
        meta = self.make_meta(tmp_path, summary=True)
        note = build_note(meta, rec("t"), UTC)
        assert "## Summary" in note and "key points" in note
        assert "## Transcript" in note and "plain text" in note

    def test_diarized_preferred(self, tmp_path):
        meta = self.make_meta(tmp_path, diarized=True, summary=True)
        note = build_note(meta, rec("t"), UTC)
        assert "SPEAKER_00" in note and "plain text" not in note


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

    def test_exports_note(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        root = tmp_path / "out"
        path = export_recording(root, meta, rec("Meet"), UTC)
        assert path is not None and path.is_file()
        assert path.parent == root
        assert "body" in path.read_text(encoding="utf-8")

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


class TestFlockConcurrency:
    def test_parallel_writes_produce_one_valid_file(self, tmp_path):
        import concurrent.futures

        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        root = tmp_path / "out"
        root.mkdir()
        path = note_path(root, rec("Meet"), UTC)

        def write(i):
            write_note_atomic(path, f"content-{i}")
            return i

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            assert sorted(ex.map(write, range(16))) == list(range(16))
        files = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
        assert files == [path.name]
        assert path.read_text().startswith("content-")
