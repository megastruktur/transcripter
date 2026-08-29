"""Profile loader + matcher: validation, warn+skip, re-scan per call."""

import logging
from pathlib import Path

from worker.profiles import (
    HOST_VERSION,
    Profile,
    SummarizeSpec,
    artifacts_for_export,
    load_profiles,
    match_profile_by_type,
)


def _valid_yaml() -> str:
    return """\
id: test-profile
version: 1.0.0
min_host_version: 0.10.0
display_name: Test
description: Test profile
type: meeting
summarize:
  prompt: |
    Hello {title}. {transcript}
  output_artifact: session-log.md
"""


def _write_profile(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# --- Profile model ----------------------------------------------------------


class TestProfileModel:
    def test_minimal_valid(self):
        prof = Profile.model_validate(
            {
                "id": "foo",
                "version": "1.0.0",
                "min_host_version": "0.10.0",
                "display_name": "Foo",
                "description": "x",
                "type": "meeting",
                "summarize": {
                    "prompt": "before {transcript} after",
                    "output_artifact": "x.md",
                },
            }
        )
        assert prof.id == "foo"
        assert prof.type == "meeting"

    def test_min_host_version_too_new_warns(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "future.yaml",
            """\
id: future
version: 1.0.0
min_host_version: 99.0.0
display_name: F
description: d
type: meeting
summarize:
  prompt: 'p {transcript}'
""",
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []

    def test_prompt_missing_transcript_skipped(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "no-transcript.yaml",
            """\
id: notp
version: 1.0.0
display_name: T
description: d
type: meeting
summarize:
  prompt: 'no transcript placeholder here'
""",
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []

    def test_unsafe_artifact_skipped(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "unsafe-art.yaml",
            """\
id: unsafe
version: 1.0.0
display_name: T
description: d
type: meeting
summarize:
  prompt: 'p {transcript}'
  output_artifact: '../evil.md'
""",
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []


    def test_unknown_field_logs_warning(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "unknown.yaml",
            """\
id: un
version: 1.0.0
display_name: U
description: d
type: meeting
unknown_field: hi
summarize:
  prompt: 'p {transcript}'
""",
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        # Loaded (unknown field is forward-compat, not fatal).
        assert len(profiles) == 1
        assert any("unknown_field" in r.message for r in caplog.records)

    def test_broken_yaml_skipped(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "broken.yaml",
            "id: x\n: : : bad: yaml: :\n  - [\n",
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []

    def test_non_mapping_root_skipped(self, tmp_path, caplog):
        _write_profile(tmp_path, "list.yaml", "- one\n- two\n")
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []


# --- load_profiles ----------------------------------------------------------


class TestLoadProfiles:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_profiles(tmp_path / "does-not-exist") == []

    def test_empty_dir_returns_empty(self, tmp_path):
        assert load_profiles(tmp_path) == []

    def test_only_non_yaml_files_ignored(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not a profile")
        (tmp_path / "config.json").write_text("{}")
        assert load_profiles(tmp_path) == []

    def test_loads_yml_extension(self, tmp_path):
        _write_profile(tmp_path, "a.yml", _valid_yaml().replace("test-profile", "a"))
        profiles = load_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].id == "a"

    def test_one_bad_file_does_not_block_others(self, tmp_path, caplog):
        _write_profile(tmp_path, "broken.yaml", ": : :")
        _write_profile(tmp_path, "good.yaml", _valid_yaml().replace("test-profile", "good"))
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert [p.id for p in profiles] == ["good"]

    def test_sorted_by_id(self, tmp_path):
        for i in ("zebra", "alpha", "mango"):
            _write_profile(tmp_path, f"{i}.yaml", _valid_yaml().replace("test-profile", i))
        profiles = load_profiles(tmp_path)
        assert [p.id for p in profiles] == ["alpha", "mango", "zebra"]

    def test_rescan_picks_up_new_file(self, tmp_path):
        assert load_profiles(tmp_path) == []
        _write_profile(tmp_path, "late.yaml", _valid_yaml().replace("test-profile", "late"))
        # No cache: a fresh call must see the new file.
        profiles = load_profiles(tmp_path)
        assert [p.id for p in profiles] == ["late"]


# --- match_profile_by_type (Phase 0) ----------------------------------------


class TestMatchProfileByType:
    def test_no_profiles_dir_returns_none(self, tmp_path):
        assert match_profile_by_type("meeting", tmp_path / "absent") is None

    def test_none_recording_type_returns_none(self, tmp_path):
        """Untyped recording → default pipeline (no profile)."""
        _write_profile(tmp_path, "p.yaml", _valid_yaml())
        assert match_profile_by_type(None, tmp_path) is None

    def test_empty_recording_type_returns_none(self, tmp_path):
        _write_profile(tmp_path, "p.yaml", _valid_yaml())
        assert match_profile_by_type("", tmp_path) is None

    def test_unknown_type_returns_none(self, tmp_path):
        """Unknown types are stored as-is; they simply match no profile."""
        _write_profile(tmp_path, "p.yaml", _valid_yaml())
        assert match_profile_by_type("lecture", tmp_path) is None

    def test_type_match_returns_profile(self, tmp_path):
        _write_profile(tmp_path, "p.yaml", _valid_yaml())
        prof = match_profile_by_type("meeting", tmp_path)
        assert prof is not None
        assert prof.id == "test-profile"

    def test_multi_match_picks_sorted_first_with_warning(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "zeta.yaml",
            _valid_yaml().replace("test-profile", "zeta"),
        )
        _write_profile(
            tmp_path,
            "alpha.yaml",
            _valid_yaml().replace("test-profile", "alpha"),
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            prof = match_profile_by_type("meeting", tmp_path)
        assert prof is not None
        assert prof.id == "alpha"
        assert any(
            "sorted id" in r.message or "match" in r.message.lower()
            for r in caplog.records
        )

    def test_summary_spec_prompt_includes_transcript(self):
        spec = SummarizeSpec(prompt="Hello {title}, transcript: {transcript}")
        assert "{transcript}" in spec.prompt

    def test_host_version_constant(self):
        # Phase 0 bumped the floor: profiles declare min_host_version 0.10.0.
        assert HOST_VERSION == "0.10.0"


class TestLegacyTagsRemoved:
    """Phase 0 cutover: a profile carrying the REMOVED ``tags:`` key is
    warn+skipped as invalid — not loaded with default routing."""

    def test_tags_field_skips_profile(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "legacy.yaml",
            """\
id: legacy
version: 1.0.0
display_name: L
description: d
tags: [alpha]
summarize:
  prompt: 'p {transcript}'
""",
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []

    def test_type_field_is_valid(self):
        prof = Profile.model_validate(
            {
                "id": "foo",
                "version": "1.0.0",
                "display_name": "Foo",
                "description": "x",
                "type": "ttrpg",
                "summarize": {"prompt": "p {transcript}"},
            }
        )
        assert prof.type == "ttrpg"

    def test_bad_type_slug_skips_profile(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "badtype.yaml",
            _valid_yaml().replace("type: meeting", "type: 'Bad Type!'"),
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []

    def test_missing_type_skips_profile(self, tmp_path, caplog):
        _write_profile(
            tmp_path,
            "notype.yaml",
            _valid_yaml().replace("type: meeting\n", ""),
        )
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(tmp_path)
        assert profiles == []



# --- artifacts_for_export ---------------------------------------------------


class TestArtifactsForExport:
    def test_static_three_when_dir_missing(self, tmp_path):
        wl = artifacts_for_export(tmp_path / "absent")
        assert wl == frozenset({"transcript.md", "diarized-transcript.md", "summary.md"})

    def test_includes_profile_output_artifact(self, tmp_path):
        _write_profile(tmp_path, "p.yaml", _valid_yaml())
        wl = artifacts_for_export(tmp_path)
        assert "session-log.md" in wl
        # Static base still present.
        assert {"transcript.md", "diarized-transcript.md", "summary.md"} <= wl

    def test_union_across_multiple_profiles(self, tmp_path):
        _write_profile(tmp_path, "a.yaml", _valid_yaml().replace("test-profile", "a"))
        _write_profile(
            tmp_path,
            "b.yaml",
            _valid_yaml().replace("test-profile", "b").replace("session-log.md", "notes.md"),
        )
        wl = artifacts_for_export(tmp_path)
        assert {"session-log.md", "notes.md"} <= wl


# --- Profile.enrich (wave B) ------------------------------------------------


class TestProfileEnrich:
    """The enrich section is OPTIONAL — wave-A profiles still validate
    with no enrich block. A present block must (a) contain
    ``{transcript}`` in the prompt and (b) accept an optional
    ``node_labels`` override."""

    def test_no_enrich_block_is_valid(self, tmp_path):
        _write_profile(tmp_path, "no-enrich.yaml", _valid_yaml())
        profiles = load_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].enrich is None

    def test_enrich_block_loads(self, tmp_path):
        body = (
            _valid_yaml()
            + "\nenrich:\n  prompt: |\n    extract. {transcript}\n"
        )
        _write_profile(tmp_path, "with-enrich.yaml", body)
        profiles = load_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].enrich is not None
        assert "{transcript}" in profiles[0].enrich.prompt
        # Default node_labels match the contract.
        assert profiles[0].enrich.node_labels.event == "Event"
        assert profiles[0].enrich.node_labels.entity == "Entity"

    def test_enrich_prompt_without_transcript_skipped(self, tmp_path):
        body = (
            _valid_yaml()
            + "\nenrich:\n  prompt: |\n    no placeholder here\n"
        )
        _write_profile(tmp_path, "bad-enrich.yaml", body)
        profiles = load_profiles(tmp_path)
        assert profiles == []

    def test_enrich_node_labels_override(self, tmp_path):
        body = (
            _valid_yaml()
            + "\nenrich:\n  prompt: |\n    p {transcript}\n  node_labels:\n    event: CampaignEvent\n    entity: Thing\n"
        )
        _write_profile(tmp_path, "with-labels.yaml", body)
        profiles = load_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].enrich.node_labels.event == "CampaignEvent"
        assert profiles[0].enrich.node_labels.entity == "Thing"


# --- EnrichSpec.known_entities (Phase 2) -------------------------------------


class TestEnrichKnownEntities:
    def test_default_off(self, tmp_path):
        _write_profile(
            tmp_path, "p.yaml", _valid_yaml() + "\nenrich:\n  prompt: |\n    e {transcript}\n"
        )
        profiles = load_profiles(tmp_path)
        assert profiles[0].enrich.known_entities is False

    def test_enabled_requires_placeholder(self, tmp_path):
        """known_entities: true without {known_entities} in the prompt →
        the file is invalid and warn+skipped."""
        _write_profile(
            tmp_path,
            "p.yaml",
            _valid_yaml()
            + "\nenrich:\n  prompt: |\n    e {transcript}\n  known_entities: true\n",
        )
        assert load_profiles(tmp_path) == []

    def test_enabled_with_placeholder_loads(self, tmp_path):
        _write_profile(
            tmp_path,
            "p.yaml",
            _valid_yaml()
            + "\nenrich:\n  prompt: |\n    e {transcript}\n    {known_entities}\n  known_entities: true\n",
        )
        profiles = load_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].enrich.known_entities is True
        assert "{known_entities}" in profiles[0].enrich.prompt

    def test_integer_n_loads(self, tmp_path):
        _write_profile(
            tmp_path,
            "p.yaml",
            _valid_yaml()
            + "\nenrich:\n  prompt: |\n    e {transcript}\n    {known_entities}\n  known_entities: 10\n",
        )
        profiles = load_profiles(tmp_path)
        assert profiles[0].enrich.known_entities == 10

    def test_non_positive_integer_invalid(self, tmp_path):
        _write_profile(
            tmp_path,
            "p.yaml",
            _valid_yaml()
            + "\nenrich:\n  prompt: |\n    e {transcript}\n    {known_entities}\n  known_entities: 0\n",
        )
        assert load_profiles(tmp_path) == []

    def test_placeholder_without_enabled_renders_empty(self):
        """A prompt carrying {known_entities} with the lookup off is LEGAL
        (the host renders '' — zero lookup cost); only the enabled-without-
        placeholder direction is an author error."""
        from worker.profiles import EnrichSpec

        spec = EnrichSpec(prompt="e {transcript} {known_entities}")
        assert spec.known_entities is False
