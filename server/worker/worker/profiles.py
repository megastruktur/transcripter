"""Profile loader + matcher (declarative yaml knowledge-graph profiles).

Profiles live under PROFILES_DIR (default /etc/transcripter/profiles) as
one yaml file each. The host re-scans the directory on EVERY call
(no cache between stage runs, by D11), validates each file against the
contract, and exposes two helpers used downstream:

- ``load_profiles(profiles_dir)`` — list[Profile] (warn+skip on bad files)
- ``match_profile(tags, profiles_dir)`` — first profile by sorted(id) whose
  tag set intersects ``tags``; logs a warning on multi-match.

A missing directory is treated as "no profiles configured" (return ``[]``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

log = logging.getLogger("transcripter.profiles")

# Host version this build accepts. Bumped per release; profiles declare a
# minimum compatible host in ``min_host_version`` and are rejected below it.
HOST_VERSION = "0.9.0"

# Filenames that look like artifacts or lockfiles are not profile sources.
_YAML_SUFFIX = ".yaml"
_YAML_ALT_SUFFIX = ".yml"

# Artifact name sanitization: only [a-z0-9._-]; anything else → warn+skip.
_SAFE_ARTIFACT = re.compile(r"^[a-z0-9._-]+$")


class SummarizeSpec(BaseModel):
    """Profile-driven summarize override."""

    prompt: str = Field(min_length=1)
    output_artifact: str = "summary.md"

    @field_validator("prompt")
    @classmethod
    def _must_contain_transcript(cls, v: str) -> str:
        if "{transcript}" not in v:
            # Per contract, a profile prompt without {transcript} is unsafe to
            # run (the whole point of overriding is to inject the transcript).
            raise ValueError("summarize.prompt must contain {transcript}")
        return v

    @field_validator("output_artifact")
    @classmethod
    def _sanitize_artifact(cls, v: str) -> str:
        if not _SAFE_ARTIFACT.match(v):
            raise ValueError(
                f"summarize.output_artifact {v!r} contains unsafe characters; "
                "allowed: [a-z0-9._-]"
            )
        return v


class Profile(BaseModel):
    """One yaml profile (validate-on-load: bad files are skipped, never crash)."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    min_host_version: str = "0.0.0"
    display_name: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    summarize: SummarizeSpec


    @model_validator(mode="before")
    @classmethod
    def _warn_unknown_fields(cls, data):
        if isinstance(data, dict):
            known = {
                "id",
                "version",
                "min_host_version",
                "display_name",
                "description",
                "tags",
                "summarize",
            }
            extras = set(data) - known
            if extras:
                log.warning(
                    "profile: ignoring unknown field(s) %s (forward-compat: will be re-introduced when implemented)",
                    sorted(extras),
                )
        return data
    @field_validator("id")
    @classmethod
    def _id_safe(cls, v: str) -> str:
        # slug shape; same character class as artifact for consistency
        if not _SAFE_ARTIFACT.match(v):
            raise ValueError(f"id {v!r} must match [a-z0-9._-]")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_nonempty_str(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("tags must be a non-empty list[str]")
        cleaned = [t.strip().lower() for t in v if isinstance(t, str) and t.strip()]
        if not cleaned:
            raise ValueError("tags must contain at least one non-empty string")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("tags must be unique (case-insensitive)")
        return cleaned

    @model_validator(mode="after")
    def _host_version_ok(self) -> Profile:
        if not _version_at_least(HOST_VERSION, self.min_host_version):
            raise ValueError(
                f"profile requires host >= {self.min_host_version}, host is {HOST_VERSION}"
            )
        return self


def _version_at_least(host: str, minimum: str) -> bool:
    """Semver-ish ``>=`` for MAJOR.MINOR.PATCH. Returns True if host >= minimum.

    A non-parseable segment is treated as 0 — forward-compat with
    ``1.0`` (no patch) and similar shorthand."""
    h = _vparts(host)
    m = _vparts(minimum)
    for hi, mi in zip(h, m, strict=False):
        if hi > mi:
            return True
        if hi < mi:
            return False
    return len(h) >= len(m) or h == m


def _vparts(v: str) -> tuple[int, ...]:
    out: list[int] = []
    for chunk in v.split("."):
        try:
            out.append(int(chunk))
        except ValueError:
            out.append(0)
    return tuple(out)


def _profile_files(profiles_dir: Path) -> list[Path]:
    if not profiles_dir.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(profiles_dir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.endswith((_YAML_SUFFIX, _YAML_ALT_SUFFIX)):
            out.append(entry)
    return out


def _load_one(path: Path) -> Profile | None:
    """Validate one yaml file. Returns a Profile, or None if it was skipped."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        log.warning("profile %s: skipping (yaml load failed: %s)", path, e)
        return None
    if not isinstance(raw, dict):
        log.warning("profile %s: skipping (expected a mapping at the root)", path)
        return None
    try:
        return Profile.model_validate(raw)
    except ValidationError as e:
        log.warning("profile %s: skipping (validation failed: %s)", path, e)
        return None
    except ValueError as e:
        # our own validators raise ValueError; normalize to ValidationError shape
        log.warning("profile %s: skipping (%s)", path, e)
        return None


def load_profiles(profiles_dir: Path | str) -> list[Profile]:
    """Re-scan ``profiles_dir`` for *.yaml/*.yml and validate each. A missing
    directory is not an error: returns ``[]`` so a fresh install boots clean.

    Returns profiles sorted by id (callers rely on this for determinism)."""
    pdir = Path(profiles_dir)
    if not pdir.is_dir():
        return []
    profiles: list[Profile] = []
    for entry in _profile_files(pdir):
        prof = _load_one(entry)
        if prof is not None:
            profiles.append(prof)
    profiles.sort(key=lambda p: p.id)
    return profiles


def match_profile(
    tags: Iterable[str], profiles_dir: Path | str
) -> Profile | None:
    """Return the first profile (sorted by id) whose ``tags`` intersect the
    recording's normalized tags. Multi-match → pick sorted-first + warn.

    Tags on both sides are compared lowercase; callers are expected to pass
    already-normalized tags, but we lowercase defensively for tag equality."""
    pdir = Path(profiles_dir)
    if not pdir.is_dir():
        return None
    rec_tags = {t.strip().lower() for t in tags if isinstance(t, str) and t.strip()}
    if not rec_tags:
        return None
    matches: list[Profile] = []
    for prof in load_profiles(pdir):
        if rec_tags.intersection(prof.tags):
            matches.append(prof)
    if not matches:
        return None
    matches.sort(key=lambda p: p.id)
    if len(matches) > 1:
        log.warning(
            "profile match: %d profiles match tags %s; using first by sorted id: %s",
            len(matches),
            sorted(rec_tags),
            matches[0].id,
        )
    return matches[0]


def artifacts_for_export(profiles_dir: Path | str) -> frozenset[str]:
    """Union of (static base artifacts) ∪ (every profile's output_artifact).

    The base set is the contract: transcript.md, diarized-transcript.md,
    summary.md. Profile output_artifacts extend the set so mirror-delete
    (export.py) doesn't leave stale renamed files behind when a profile is
    removed between regenerates.
    """
    base = frozenset({"transcript.md", "diarized-transcript.md", "summary.md"})
    extras: set[str] = set()
    for prof in load_profiles(profiles_dir):
        extras.add(prof.summarize.output_artifact)
    return base | extras
