"""Profiles lister: GET /profiles.

Reads YAML profiles from cfg.profiles.path on every call — profiles may
change between requests and the listing is cheap (one stat + safe_load per
file). Bad files (broken YAML, missing required keys, wrong types) are
warn-logged and skipped; listing the rest still succeeds. Missing dir
yields an empty list (per contract) — the caller (client UI) treats no
profiles as "no summaries match", not an error.
"""

import logging
from typing import Any

import yaml
from fastapi import APIRouter, Request

from app.config import ServerConfig

router = APIRouter(prefix="/profiles")

_LOG = logging.getLogger("transcripter.api.profiles")

_REQUIRED_PROFILE_KEYS: tuple[str, ...] = (
    "id",
    "display_name",
    "description",
    "tags",
)


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

        missing = [k for k in _REQUIRED_PROFILE_KEYS if k not in raw]
        if missing:
            _LOG.warning(
                "profiles: skipping %s (missing required keys: %s)",
                path.name,
                ", ".join(missing),
            )
            continue

        tags_raw = raw["tags"]
        if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
            _LOG.warning(
                "profiles: skipping %s (`tags` must be a list of strings)",
                path.name,
            )
            continue

        out.append(
            {
                "id": raw["id"],
                "version": raw.get("version", ""),
                "display_name": raw["display_name"],
                "description": raw["description"],
                "tags": tags_raw,
            }
        )
    return out


@router.get("")
def list_profiles(request: Request) -> list[dict[str, Any]]:
    cfg: ServerConfig = request.app.state.config
    return _list_profiles(cfg.profiles.path)
