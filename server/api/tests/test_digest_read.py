"""Phase 1 API surface: GET /tags/{tag}/digest (read a generated digest).

The worker writes ``digests/<slug>.md`` under the transcripts root and
stamps the raw normalized tag into the YAML frontmatter. The API cannot
reconstruct the slug, so lookup = list *.md + frontmatter tag match.

2026-09-08: the endpoint replies structured JSON, not the raw file —
``{tag, generated_at, body, recordings}`` where ``body`` is the note
WITHOUT frontmatter (it used to render as ``---`` + YAML text in the
client's markdown view) and ``recordings`` resolves the frontmatter ids
against the catalog into click-through rows (title + date), dropping
ids whose recording no longer exists.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

AUTH = {"authorization": "Bearer sekrit"}


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
    digests_dir: Path,
    slug: str,
    tag: str,
    body: str = "# Digest\n\nhello\n",
    recordings: list[str] | None = None,
    generated_at: str = "2026-09-07T19:40:24+00:00",
) -> None:
    digests_dir.mkdir(parents=True, exist_ok=True)
    fm = f'---\ntag: "{tag}"\ngenerated_at: "{generated_at}"\n'
    if recordings is not None:
        fm += "recordings:\n" + "".join(f"  - {rid}\n" for rid in recordings)
        fm += f"count: {len(recordings)}\n"
    (digests_dir / f"{slug}.md").write_text(
        f"{fm}---\n\n{body}", encoding="utf-8"
    )


def _make_recording(client: TestClient, title: str) -> str:
    r = client.post("/recordings", json={"title": title})
    return r.json()["id"]


# ---------- 200 happy path ----------


def test_get_digest_returns_json_body_without_frontmatter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    _write_digest(digests, "pathfinder", "pathfinder")

    r = client.get("/tags/pathfinder/digest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    payload = r.json()
    assert payload["tag"] == "pathfinder"
    assert payload["generated_at"] == "2026-09-07T19:40:24+00:00"
    # The body carries the note content and NOTHING of the frontmatter —
    # no --- rules, no tag:, no generated_at: leaking into the view.
    assert payload["body"].startswith("# Digest")
    assert "hello" in payload["body"]
    assert "---" not in payload["body"]
    assert "generated_at:" not in payload["body"]
    assert payload["recordings"] == []


def test_get_digest_resolves_reference_recordings_oldest_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Frontmatter ids resolve to title/date rows in the catalog, in the
    frontmatter's order (the worker writes them chronological since
    2026-09-08) — the client renders them as reference links."""
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    r1 = _make_recording(client, "Trouble in Sandpoint")
    r2 = _make_recording(client, "The Lost Coast")
    _write_digest(
        digests, "pathfinder", "pathfinder", recordings=[r1, r2]
    )

    r = client.get("/tags/pathfinder/digest")
    assert r.status_code == 200
    payload = r.json()
    assert [rec["id"] for rec in payload["recordings"]] == [r1, r2]
    assert payload["recordings"][0]["title"] == "Trouble in Sandpoint"
    assert payload["recordings"][1]["title"] == "The Lost Coast"
    assert all(rec["recorded_at"] for rec in payload["recordings"])


def test_get_digest_drops_deleted_recordings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An id whose recording row is gone (deleted after generation)
    resolves to nothing and drops out — a stale link is worse than a
    missing one."""
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    live = _make_recording(client, "Alive")
    _write_digest(
        digests, "pathfinder", "pathfinder", recordings=[live, "00000000-0000-0000-0000-000000000000"]
    )

    r = client.get("/tags/pathfinder/digest")
    assert r.status_code == 200
    assert [rec["id"] for rec in r.json()["recordings"]] == [live]


def test_get_digest_untitled_recording_gets_placeholder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty title must render a readable placeholder, not an empty
    link label."""
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    rid = client.post("/recordings", json={"title": ""}).json()["id"]
    _write_digest(digests, "pathfinder", "pathfinder", recordings=[rid])

    r = client.get("/tags/pathfinder/digest")
    assert r.status_code == 200
    assert r.json()["recordings"][0]["title"] == "(untitled)"


def test_get_digest_first_sorted_match_wins(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two files claiming the same tag: deterministic pick (sorted names)."""
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    _write_digest(digests, "aaa", "dup", body="# A body\n")
    _write_digest(digests, "zzz", "dup", body="# Z body\n")

    r = client.get("/tags/dup/digest")
    assert r.status_code == 200
    assert "A body" in r.json()["body"]


def test_get_digest_unicode_tag_matches_normalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Raw tag «Проба Кириллица» normalizes to the lowercase fm tag."""
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    _write_digest(digests, "proba-kirillitsa", "проба кириллица")

    raw = "%D0%9F%D1%80%D0%BE%D0%B1%D0%B0%20%D0%9A%D0%B8%D1%80%D0%B8%D0%BB%D0%BB%D0%B8%D1%86%D0%B0"
    r = client.get(f"/tags/{raw}/digest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


# ---------- 404s ----------


def test_get_digest_no_file_for_tag_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    digests = tmp_path / "transcripts" / "digests"
    digests.mkdir(parents=True)
    (digests / "other.md").write_text(
        '---\ntag: "other"\n---\nnope', encoding="utf-8"
    )
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )

    r = client.get("/tags/nosuchtag/digest")
    assert r.status_code == 404
    assert "nosuchtag" in r.json()["detail"]
    assert "not generated yet" in r.json()["detail"]


def test_get_digest_missing_dir_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "nowhere"
    )

    r = client.get("/tags/anything/digest")
    assert r.status_code == 404
    assert "not generated yet" in r.json()["detail"]


def test_get_digest_skips_oversized_and_nonfrontmatter_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A >1MB file and a note without frontmatter must neither match nor 500."""
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    digests.mkdir(parents=True)
    (digests / "big.md").write_bytes(b"---\ntag: big\n---\n" + b"x" * (1024 * 1024 + 5))
    (digests / "no_fm.md").write_text("no frontmatter here", encoding="utf-8")

    r = client.get("/tags/big/digest")
    assert r.status_code == 404


def test_get_digest_malformed_frontmatter_degrades_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A note that frontmatter-matched but carries garbage YAML inside a
    key (unparseable recordings value) must degrade to empty references,
    never a 500."""
    digests = tmp_path / "transcripts" / "digests"
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )
    digests.mkdir(parents=True)
    # Valid tag so find_digest matches; recordings is not a list — the
    # endpoint's isinstance guard drops it.
    (digests / "odd.md").write_text(
        '---\ntag: "odd"\nrecordings: 42\n---\n\n# Odd\n', encoding="utf-8"
    )

    r = client.get("/tags/odd/digest")
    assert r.status_code == 200
    payload = r.json()
    assert payload["recordings"] == []
    assert payload["body"].startswith("# Odd")


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
