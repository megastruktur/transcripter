"""Profiles lister: GET /profiles.

Reads YAML profiles from cfg.profiles.path on every call — profiles may
change between requests and the listing is cheap (one stat + safe_load per
file). Bad files (broken YAML, missing required keys, wrong types) are
warn-logged and skipped; listing the rest still succeeds. Missing dir
yields an empty list (per contract) — the caller (client UI) treats no
profiles as "no summaries match", not an error.
"""

import logging
import re
from typing import Any

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.config import ServerConfig

router = APIRouter(prefix="/profiles")

_LOG = logging.getLogger("transcripter.api.profiles")

# Mirrors of worker/profiles.py rules — the two packages ship in separate
# images, so the contract is duplicated here deliberately: a profile the
# worker would warn+skip must NOT appear in GET /profiles (the client would
# offer a profile that can never match). Keep these in sync with
# worker.profiles (HOST_VERSION, slug class, tags rules, summarize prompt).
_HOST_VERSION = "0.9.0"
_SAFE_SLUG = re.compile(r"^[a-z0-9._-]+$")


def _vparts(v: str) -> tuple[int, ...]:
    out: list[int] = []
    for chunk in v.split("."):
        try:
            out.append(int(chunk))
        except ValueError:
            out.append(0)
    return tuple(out)


def _version_at_least(host: str, minimum: str) -> bool:
    h = _vparts(host)
    m = _vparts(minimum)
    for hi, mi in zip(h, m, strict=False):
        if hi > mi:
            return True
        if hi < mi:
            return False
    return len(h) >= len(m) or h == m


class _SummarizeSpec(BaseModel):
    prompt: str = Field(min_length=1)
    # Same sanitize rule as the worker: the artifact name lands in the
    # Obsidian vault verbatim, so anything outside [a-z0-9._-] must be
    # rejected here too, or we'd list a profile the worker skips.
    output_artifact: str = "summary.md"

    @field_validator("prompt")
    @classmethod
    def _must_contain_transcript(cls, v: str) -> str:
        if "{transcript}" not in v:
            raise ValueError("summarize.prompt must contain {transcript}")
        return v

    @field_validator("output_artifact")
    @classmethod
    def _sanitize_artifact(cls, v: str) -> str:
        if not _SAFE_SLUG.match(v):
            raise ValueError(f"summarize.output_artifact {v!r} must match [a-z0-9._-]")
        return v


class _EnrichSpec(BaseModel):
    """Worker EnrichSpec mirror: a malformed enrich block makes the worker
    skip the WHOLE profile, so it must fail validation here too."""

    prompt: str = Field(min_length=1)

    @field_validator("prompt")
    @classmethod
    def _must_contain_transcript(cls, v: str) -> str:
        if "{transcript}" not in v:
            raise ValueError("enrich.prompt must contain {transcript}")
        return v


class _ProfileSpec(BaseModel):
    """The worker's Profile contract, listing-surface subset."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    min_host_version: str = "0.0.0"
    display_name: str = Field(min_length=1)
    description: str = ""
    tags: list[str]
    summarize: _SummarizeSpec
    enrich: _EnrichSpec | None = None

    @field_validator("id")
    @classmethod
    def _id_safe(cls, v: str) -> str:
        if not _SAFE_SLUG.match(v):
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
    def _host_version_ok(self) -> "_ProfileSpec":
        if not _version_at_least(_HOST_VERSION, self.min_host_version):
            raise ValueError(
                f"profile requires host >= {self.min_host_version}, host is {_HOST_VERSION}"
            )
        return self


def _list_profiles(profiles_dir: Any) -> list[dict[str, Any]]:
    """Scan `profiles_dir` and return one entry per loadable profile.

    The contract says:
      - success → entry has {id, display_name, description, version, tags}
      - missing dir → []
      - bad file → warn + skip that file, keep scanning
    Validation lives here (not on the worker) so the API surface matches
    the wave-A plan exactly — version/tag shaping happens before the
    client sees the row.
    """
    if not profiles_dir.exists() or not profiles_dir.is_dir():
        return []

    out: list[dict[str, Any]] = []
    for path in sorted(profiles_dir.glob("*.y*ml")):
        try:
            with open(path) as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError:
            _LOG.warning("profiles: skipping %s (broken YAML)", path.name)
            continue
        except OSError as exc:
            _LOG.warning("profiles: skipping %s (%s)", path.name, exc)
            continue

        if not isinstance(raw, dict):
            _LOG.warning(
                "profiles: skipping %s (top-level YAML must be a mapping)",
                path.name,
            )
            continue

        try:
            profile = _ProfileSpec.model_validate(raw)
        except ValidationError as exc:
            # Same skip semantics as the worker loader: warn + skip, keep
            # scanning. First error line is enough context for the author.
            _LOG.warning(
                "profiles: skipping %s (%s)",
                path.name,
                exc.errors()[0].get("msg", "invalid profile"),
            )
            continue

        out.append(
            {
                "id": profile.id,
                "version": profile.version,
                "display_name": profile.display_name,
                "description": profile.description,
                "tags": profile.tags,
                # Validated model: enrich survived _EnrichSpec (prompt with
                # {transcript}), so the worker would honor it too.
                "has_enrich": profile.enrich is not None,
            }
        )
    return out


@router.get("")
def list_profiles(request: Request) -> list[dict[str, Any]]:
    cfg: ServerConfig = request.app.state.config
    return _list_profiles(cfg.profiles.path)
