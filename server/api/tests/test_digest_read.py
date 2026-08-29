"""Phase 1 API surface: GET /tags/{tag}/digest (read a generated digest).

The worker writes ``digests/<slug>.md`` under the transcripts root and
stamps the raw normalized tag into the YAML frontmatter. The API cannot
reconstruct the slug, so lookup = list *.md + frontmatter tag match.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    from app import temporal_client

    monkeypatch.setattr(
        temporal_client, "start_digest", AsyncMock(return_value="wf-digest-abc")
    )
    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _write_digest(
    digests_dir: Path, slug: str, tag: str, body: str = "# Digest\n\nhello\n"
) -> None:
    digests_dir.mkdir(parents=True, exist_ok=True)
    (digests_dir / f"{slug}.md").write_text(
        f"---\ntag: \"{tag}\"\n---\n\n{body}", encoding="utf-8"
    )


# ---------- 200 happy path ----------


def test_get_digest_returns_markdown_for_frontmatter_match(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    digests = tmp_path / "transcripts" / "digests"
    _write_digest(digests, "pathfinder", "pathfinder")
    _write_digest(digests, "other-tag", "other tag")
    monkeypatch.setattr(
        client.app.state.config.transcripts, "path", tmp_path / "transcripts"
    )

    r = client.get("/tags/pathfinder/digest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "# Digest" in r.text


def test_get_digest_first_sorted_match_wins(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two files claiming the same tag: deterministic pick (sorted names)."""
    digests = tmp_path / "transcripts" / "digests"
    _write_digest(digests, "b-dup", "dup", body="B body")
    _write_digest(digests, "a-dup", "dup", body="A body")
    monkeypatch.setattr(
        client.app.state.config.transcripts, "path", tmp_path / "transcripts"
    )

    r = client.get("/tags/dup/digest")
    assert r.status_code == 200
    assert "A body" in r.text


def test_get_digest_unicode_tag_matches_normalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Raw tag «Проба Кириллица» normalizes to the lowercase fm tag."""
    digests = tmp_path / "transcripts" / "digests"
    _write_digest(digests, "проба-кириллица", "проба кириллица")
    monkeypatch.setattr(
        client.app.state.config.transcripts, "path", tmp_path / "transcripts"
    )

    raw = "%D0%9F%D1%80%D0%BE%D0%B1%D0%B0%20%D0%9A%D0%B8%D1%80%D0%B8%D0%BB%D0%BB%D0%B8%D1%86%D0%B0"
    r = client.get(f"/tags/{raw}/digest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")


# ---------- 404s ----------


def test_get_digest_no_file_for_tag_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    digests = tmp_path / "transcripts" / "digests"
    _write_digest(digests, "pathfinder", "pathfinder")
    monkeypatch.setattr(
        client.app.state.config.transcripts, "path", tmp_path / "transcripts"
    )

    r = client.get("/tags/nosuchtag/digest")
    assert r.status_code == 404
    assert "nosuchtag" in r.json()["detail"]
    assert "not generated yet" in r.json()["detail"]


def test_get_digest_missing_dir_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        client.app.state.config.transcripts, "path", tmp_path / "nowhere"
    )

    r = client.get("/tags/anything/digest")
    assert r.status_code == 404
    assert "not generated yet" in r.json()["detail"]


def test_get_digest_skips_oversized_and_nonfrontmatter_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A >1MB file and a note without frontmatter must neither match nor 500."""
    digests = tmp_path / "transcripts" / "digests"
    digests.mkdir(parents=True)
    (digests / "big.md").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    (digests / "nofm.md").write_text("no frontmatter here", encoding="utf-8")
    monkeypatch.setattr(
        client.app.state.config.transcripts, "path", tmp_path / "transcripts"
    )

    r = client.get("/tags/big/digest")
    assert r.status_code == 404


# ---------- 400 validation (same boundary as POST) ----------


def test_get_digest_path_traversal_tag_never_reaches_fs(client: TestClient) -> None:
    """``%2F`` never forms a single path segment (the router splits on it),
    so a traversal tag cannot reach the filesystem — router rejects first.
    Anything within one segment is regex-checked 400 in get_digest."""
    r = client.get("/tags/%2Fetc%2Fpasswd/digest")
    assert r.status_code in (400, 404)


def test_get_digest_dotdot_segment_400(client: TestClient) -> None:
    r = client.get("/tags/..../digest")
    assert r.status_code == 400


def test_get_digest_empty_after_normalize_400(client: TestClient) -> None:
    r = client.get("/tags/%20%20/digest")
    assert r.status_code == 400


def test_get_digest_disallowed_chars_400(client: TestClient) -> None:
    r = client.get("/tags/bad%21tag%40here/digest")
    assert r.status_code == 400


def test_get_digest_too_long_400(client: TestClient) -> None:
    r = client.get(f"/tags/{'a' * 65}/digest")
    assert r.status_code == 400
