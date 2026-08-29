"""Profile loader + matcher (declarative yaml knowledge-graph profiles).

Profiles live under PROFILES_DIR (default /etc/transcripter/profiles) as
one yaml file each. The host re-scans the directory on EVERY call
(no cache between stage runs, by D11), validates each file against the
contract, and exposes two helpers used downstream:

- ``load_profiles(profiles_dir)`` — list[Profile] (warn+skip on bad files)
- ``match_profile_by_type(rec_type, profiles_dir)`` — first profile by
  sorted(id) whose ``type`` equals the recording's type; logs a warning
  on multi-match. A recording with no type (NULL) never matches →
  callers fall back to the built-in default pipeline.

A missing directory is treated as "no profiles configured" (return ``[]``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

log = logging.getLogger("transcripter.profiles")

# Host version this build accepts. Bumped per release; profiles declare a
# minimum compatible host in ``min_host_version`` and are rejected below it.
HOST_VERSION = "0.10.0"

# Filenames that look like artifacts or lockfiles are not profile sources.
_YAML_SUFFIX = ".yaml"
_YAML_ALT_SUFFIX = ".yml"

# Artifact name sanitization: only [a-z0-9._-]; anything else → warn+skip.
_SAFE_ARTIFACT = re.compile(r"^[a-z0-9._-]+$")

# Recording-type slug (the pipeline-routing key): lowercase, starts with
# an alphanumeric, then alphanumerics/dashes, ≤32 chars. Mirrored by the
# API (routes/recordings.py TYPE_RE + routes/profiles.py) — keep in sync.
_SAFE_TYPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


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


class EnrichNodeLabels(BaseModel):
    """Optional per-profile label overrides for the knowledge graph.

    The defaults below match the data-model section of the wave-B plan:
    every node carries ``origin_recording_id`` and ``tag`` regardless of
    label. Profiles only ever pick different label *names* for the same
    concept (``Event`` / ``Entity``) so cross-profile queries still work.
    """

    event: str = "Event"
    entity: str = "Entity"


class EnrichSpec(BaseModel):
    """Profile-driven knowledge-graph extraction (wave B).

    The schema is fixed in code (events/entities/relations — see
    ``worker/enrich.py``); profiles steer the domain by shaping the
    prompt. ``prompt`` must contain ``{transcript}``; ``{title}`` is
    optional. ``node_labels`` lets a profile rename the default node
    labels without forking the extraction pipeline.

    ``known_entities`` (Phase 2) opts the profile into the known-entities
    block: ``false`` (default) = no lookup, zero cost; ``true`` = the
    activity renders the target namespace's top-25 entities; an integer
    N = top-N. Enabling it requires the ``{known_entities}`` placeholder
    in the prompt (validated on load — a prompt that asks for a block it
    never receives would render stale text). The placeholder WITHOUT the
    lookup is legal: it simply renders as an empty string.
    """

    prompt: str = Field(min_length=1)
    node_labels: EnrichNodeLabels = Field(default_factory=EnrichNodeLabels)
    known_entities: bool | int = False

    @field_validator("known_entities")
    @classmethod
    def _known_entities_sane(cls, v: bool | int) -> bool | int:
        # bool is an int subclass — reject 0/True-as-int misuse: only
        # False, True, or a positive integer are valid.
        if isinstance(v, bool):
            return v
        if v <= 0:
            raise ValueError("enrich.known_entities integer must be >= 1")
        return v

    @field_validator("prompt")
    @classmethod
    def _must_contain_transcript(cls, v: str) -> str:
        if "{transcript}" not in v:
            raise ValueError("enrich.prompt must contain {transcript}")
        return v

    @model_validator(mode="after")
    def _known_entities_needs_placeholder(self) -> EnrichSpec:
        # Cross-field: enabling the lookup without the placeholder would
        # pay the graph query and drop the block. Reject on load (the
        # profile file is then warn+skipped like any invalid profile).
        if self.known_entities is not False and "{known_entities}" not in self.prompt:
            raise ValueError(
                "enrich.known_entities is enabled but the prompt lacks the "
                "{known_entities} placeholder"
            )
        return self


class Profile(BaseModel):
    """One yaml profile (validate-on-load: bad files are skipped, never crash)."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    min_host_version: str = "0.0.0"
    display_name: str = Field(min_length=1)
    description: str = ""
    # Phase 0 cutover: the recording TYPE this profile routes (was: tags
    # intersection). Mandatory; a legacy file carrying `tags:` fails
    # validation via the unknown-field rule below (warn+skip as invalid).
    type: str
    summarize: SummarizeSpec
    enrich: EnrichSpec | None = None

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
                "type",
                "summarize",
                "enrich",
            }
            extras = set(data) - known
            if extras:
                log.warning(
                    "profile: ignoring unknown field(s) %s (forward-compat: will be re-introduced when implemented)",
                    sorted(extras),
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _tags_removed(cls, data):
        """Phase 0 cutover: ``tags:`` is the REMOVED tag-intersection
        routing. Unlike ordinary unknown fields (forward-compat: warn +
        load), a file still carrying ``tags:`` is skipped as invalid —
        silently loading it with default routing would mask the author's
        intent. Replace it with ``type:``."""
        if isinstance(data, dict) and "tags" in data:
            raise ValueError(
                "field 'tags' was removed in Phase 0 — replace it with 'type' "
                "(profile routing is by recording.type now)"
            )
        return data

    @field_validator("id")
    @classmethod
    def _id_safe(cls, v: str) -> str:
        # slug shape; same character class as artifact for consistency
        if not _SAFE_ARTIFACT.match(v):
            raise ValueError(f"id {v!r} must match [a-z0-9._-]")
        return v

    @field_validator("type")
    @classmethod
    def _type_safe(cls, v: str) -> str:
        if not _SAFE_TYPE.match(v):
            raise ValueError(
                f"type {v!r} must match ^[a-z0-9][a-z0-9-]{{0,31}}$"
            )
        return v

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


def match_profile_by_type(
    rec_type: str | None, profiles_dir: Path | str
) -> Profile | None:
    """Return the first profile (sorted by id) whose ``type`` equals the
    recording's type. Multi-match → pick sorted-first + warn.

    ``rec_type=None`` (untyped recording) → ``None``: the caller falls
    back to the built-in default pipeline (summarize) / skips enrich.
    A type that matches no profile behaves the same way — unknown types
    are stored as-is, the pipeline simply has no profile for them.
    """
    if not rec_type:
        return None
    pdir = Path(profiles_dir)
    if not pdir.is_dir():
        return None
    matches: list[Profile] = [p for p in load_profiles(pdir) if p.type == rec_type]
    if not matches:
        return None
    matches.sort(key=lambda p: p.id)
    if len(matches) > 1:
        log.warning(
            "profile match: %d profiles match type %r; using first by sorted id: %s",
            len(matches),
            rec_type,
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
