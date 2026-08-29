"""Export: profile-driven renaming, computed whitelist, frontmatter fields."""

from datetime import UTC, datetime

import yaml

from worker.export import (
    ARTIFACTS,
    Rec,
    _whitelist,
    build_artifact,
    export_recording,
)

REC_ID = "a1b2c3d4-1111-2222-3333-444444444444"
CREATED = datetime(2026, 8, 23, 18, 45, tzinfo=UTC)


def _rec(tags=None, title="Meet", type_=None):
    return Rec(REC_ID, title, CREATED, None, "done", tags=list(tags or []), type=type_)


def _profile_yaml(name="p", type_="meeting", output_artifact="session-log.md") -> str:
    return f"""\
id: {name}
version: 1.0.0
min_host_version: 0.10.0
display_name: {name}
description: d
type: {type_}
summarize:
  prompt: 'p {{transcript}}'
  output_artifact: {output_artifact}
"""


def _setup_meta(meta, *, transcript=True, diarized=False, summary=False):
    meta.mkdir(parents=True, exist_ok=True)
    if transcript:
        (meta / "transcript.md").write_text("body", encoding="utf-8")
    if diarized:
        (meta / "diarized-transcript.md").write_text("d-body", encoding="utf-8")
    if summary:
        (meta / "summary.md").write_text("s-body", encoding="utf-8")


def _setup_profiles(profiles_dir, *yamls: str):
    profiles_dir.mkdir(parents=True, exist_ok=True)
    for i, body in enumerate(yamls):
        (profiles_dir / f"p{i}.yaml").write_text(body, encoding="utf-8")


def _parse_frontmatter(text: str) -> dict:
    """`---` opener + yaml + `---` closer → dict."""
    _, fm, _ = text.split("---", 2)
    return yaml.safe_load(fm)


# --- profile renaming -------------------------------------------------------


class TestProfileRenaming:
    def test_no_profile_writes_summary_md(self, tmp_path):
        root = tmp_path / "out"
        meta = tmp_path / "meta"
        _setup_meta(meta, transcript=True, summary=True)
        path = export_recording(
            root, meta, _rec(tags=["nothing"]), UTC, profiles_dir=tmp_path / "absent"
        )
        assert path is not None
        assert (path / "summary.md").exists()
        assert not (path / "session-log.md").exists()

    def test_profile_match_renames_summary_to_output_artifact(self, tmp_path):
        root = tmp_path / "out"
        meta = tmp_path / "meta"
        profiles = tmp_path / "profiles"
        _setup_meta(meta, transcript=True, summary=True)
        _setup_profiles(profiles, _profile_yaml("p", type_="meeting", output_artifact="session-log.md"))
        path = export_recording(root, meta, _rec(type_="meeting"), UTC, profiles_dir=profiles)
        assert (path / "session-log.md").exists()
        assert not (path / "summary.md").exists()

    def test_meta_path_stays_canonical_summary_md(self, tmp_path):
        """Export must NOT rename the file inside meta/ — only the note folder."""
        root = tmp_path / "out"
        meta = tmp_path / "meta"
        profiles = tmp_path / "profiles"
        _setup_meta(meta, transcript=True, summary=True)
        _setup_profiles(profiles, _profile_yaml(output_artifact="session-log.md"))
        export_recording(root, meta, _rec(type_="meeting"), UTC, profiles_dir=profiles)
        assert (meta / "summary.md").exists()
        assert not (meta / "session-log.md").exists()

    def test_multi_match_picks_sorted_first(self, tmp_path):
        root = tmp_path / "out"
        meta = tmp_path / "meta"
        profiles = tmp_path / "profiles"
        _setup_meta(meta, transcript=True, summary=True)
        _setup_profiles(
            profiles,
            _profile_yaml("zeta", type_="meeting", output_artifact="zeta.md"),
            _profile_yaml("alpha", type_="meeting", output_artifact="alpha.md"),
        )
        path = export_recording(root, meta, _rec(type_="meeting"), UTC, profiles_dir=profiles)
        assert (path / "alpha.md").exists()
        assert not (path / "zeta.md").exists()


# --- mirror-delete whitelist -----------------------------------------------


class TestComputedWhitelist:
    def test_no_profiles_dir_returns_static_three(self, tmp_path):
        assert _whitelist(tmp_path / "absent") == frozenset(ARTIFACTS)

    def test_static_only_dir_returns_static_three(self, tmp_path):
        assert _whitelist(tmp_path) == frozenset(ARTIFACTS)

    def test_dir_with_profile_extends_whitelist(self, tmp_path):
        profiles = tmp_path / "profiles"
        _setup_profiles(profiles, _profile_yaml(output_artifact="session-log.md"))
        wl = _whitelist(profiles)
        assert "session-log.md" in wl
        # Static base still present.
        assert {"transcript.md", "diarized-transcript.md", "summary.md"} <= wl

    def test_removed_profile_artifact_stays_as_user_content(self, tmp_path):
        """When a profile is removed from disk, its previous artifact in the
        vault is treated as user content (per the wave-A impl plan §1: a
        deleted-profile file is "остаётся сиротой — mirror-delete его и так
        не тронет"). The recording's tags no longer match any profile so the
        summary gets re-written as ``summary.md`` and the orphan
        ``session-log.md`` is left alone for the user to clean up.
        """
        root = tmp_path / "out"
        meta = tmp_path / "meta"
        profiles = tmp_path / "profiles"
        _setup_meta(meta, transcript=True, summary=True)
        _setup_profiles(profiles, _profile_yaml(output_artifact="session-log.md"))
        path = export_recording(root, meta, _rec(type_="meeting"), UTC, profiles_dir=profiles)
        assert (path / "session-log.md").exists()
        for f in profiles.iterdir():
            f.unlink()
        path2 = export_recording(root, meta, _rec(type_=None), UTC, profiles_dir=profiles)
        assert (path2 / "summary.md").exists()
        # Orphan stays (user content, per contract).
        assert (path2 / "session-log.md").exists()

    def test_user_files_outside_whitelist_are_preserved(self, tmp_path):
        root = tmp_path / "out"
        meta = tmp_path / "meta"
        profiles = tmp_path / "profiles"
        _setup_meta(meta, transcript=True, summary=True)
        _setup_profiles(profiles, _profile_yaml(output_artifact="session-log.md"))
        path = export_recording(root, meta, _rec(type_="meeting"), UTC, profiles_dir=profiles)
        user = path / "scratch.md"
        user.write_text("mine", encoding="utf-8")
        # Second export (regenerate) — user file MUST survive.
        export_recording(root, meta, _rec(type_="meeting"), UTC, profiles_dir=profiles)
        assert user.exists()
        assert user.read_text(encoding="utf-8") == "mine"


# --- frontmatter -----------------------------------------------------------


class TestFrontmatter:
    def test_frontmatter_tags_default_when_recording_has_none(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        rec = _rec(tags=[])
        text = build_artifact(meta / "transcript.md", rec, UTC)
        d = _parse_frontmatter(text)
        assert d["tags"] == ["transcripter/call"]
        assert "profile" not in d
        # artifact key only added when caller passes artifact_name.
        assert "artifact" not in d

    def test_frontmatter_tags_include_recording_tags(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        rec = _rec(tags=["pathfinder", "weekly"])
        text = build_artifact(meta / "transcript.md", rec, UTC)
        d = _parse_frontmatter(text)
        assert d["tags"] == ["transcripter/call", "pathfinder", "weekly"]

    def test_summary_frontmatter_carries_profile_id(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        rec = _rec(tags=["alpha"])
        summary_text = build_artifact(
            meta / "transcript.md",
            rec,
            UTC,
            profile_id="pathfinder-party-log",
            artifact_name="session-log.md",
        )
        d = _parse_frontmatter(summary_text)
        assert d["profile"] == "pathfinder-party-log"
        assert d["artifact"] == "session-log.md"

    def test_transcript_frontmatter_has_no_profile_id(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        rec = _rec(tags=["alpha"])
        transcript_text = build_artifact(meta / "transcript.md", rec, UTC)
        d = _parse_frontmatter(transcript_text)
        assert "profile" not in d

    def test_frontmatter_tags_dedupe_with_default(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "transcript.md").write_text("body", encoding="utf-8")
        rec = _rec(tags=["transcripter/call", "transcripter/call", "extra"])
        text = build_artifact(meta / "transcript.md", rec, UTC)
        d = _parse_frontmatter(text)
        assert d["tags"] == ["transcripter/call", "extra"]


# --- PROFILES_DIR env in config --------------------------------------------


class TestProfilesDirConfig:
    def test_default_profiles_dir(self):
        from worker.config import WorkerConfig

        assert str(WorkerConfig().profiles.path) == "/etc/transcripter/profiles"

    def test_load_config_honours_profiles_dir_env(self, tmp_path, monkeypatch):
        from pathlib import Path

        # Minimal config file; PROFILES_DIR env must override.
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("transcribe:\n  model: small\n")
        monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
        monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "custom"))
        from worker.config import load_config

        cfg = load_config()
        assert cfg.profiles.path == Path(tmp_path / "custom")

    def test_profiles_config_importable(self):
        from worker.config import ProfilesConfig

        p = ProfilesConfig()
        assert str(p.path) == "/etc/transcripter/profiles"
