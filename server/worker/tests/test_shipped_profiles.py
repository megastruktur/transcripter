"""Shipped profiles: every yaml in server/profiles must load clean
(zero warn+skip), and the vet-consultation profile routes/enriches
as specified. Guards the files users actually ship, not the loader."""

import logging
from pathlib import Path

from worker.profiles import load_profiles, match_profile_by_type

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"

VET_ID = "vet-consultation"


# --- load_profiles over the real shipped dir --------------------------------


class TestShippedProfilesLoad:
    def test_every_shipped_yaml_loads_no_warn_skip(self, caplog):
        files = [
            p for p in sorted(PROFILES_DIR.iterdir()) if p.suffix in {".yaml", ".yml"}
        ]
        assert files, f"no profile files found in {PROFILES_DIR}"
        with caplog.at_level(logging.WARNING, logger="transcripter.profiles"):
            profiles = load_profiles(PROFILES_DIR)
        # A warn+skipped file would log and shrink the result below the
        # number of scanned yamls.
        assert len(profiles) == len(files)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- vet-consultation profile ------------------------------------------------


class TestVetConsultationProfile:
    def test_present_type_enrich_transcript(self):
        profiles = load_profiles(PROFILES_DIR)
        vet = next((p for p in profiles if p.id == VET_ID), None)
        assert vet is not None
        assert vet.type == "vet-consultation"
        assert vet.enrich is not None
        assert "{transcript}" in vet.summarize.prompt


# --- match_profile_by_type over the real shipped dir -------------------------


class TestMatchVetConsultation:
    def test_type_match_returns_vet_profile(self):
        prof = match_profile_by_type(VET_ID, PROFILES_DIR)
        assert prof is not None
        assert prof.id == VET_ID

    def test_none_recording_type_returns_none(self):
        # Untyped recording → default pipeline, no profile.
        assert match_profile_by_type(None, PROFILES_DIR) is None
