"""Tag digest (wave C) — unit tests on digest.py + activity wiring.

The graph backend and the LLM are both mocked. The tests cover:

* input assembly (Postgres + Neo4j slices),
* prompt rendering shape,
* atomic file write (tmp + rename),
* frontmatter keys (tag, generated_at, recordings, count),
* filename sanitization (raises on unsafe tag),
* empty-selection path (no done recordings carry the tag),
* LLM failure propagation (httpx errors bubble as exceptions for the
  activity's retry policy).
* the ``tag_digest`` activity itself (registered, graph-disabled raises,
  empty selection returns ``{written: False, ...}``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.db import Base, Recording, RecordingState, Stage, session
from worker.digest import (
    DigestGraphSlice,
    DigestInput,
    DigestRow,
    _atomic_write,
    _render_prompt,
    build_digest_input,
    run_digest,
    safe_filename,
    write_digest,
)

# ---------- shared fixtures ----------------------------------------------------


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite catalog + module-level cfg pointing at tmp storage."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)

    cfg = MagicMock()
    cfg.graph.enabled = True
    cfg.graph.uri = "bolt://n:7687"
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.vault.path = tmp_path / "transcripts"
    cfg.summarize.base_url = "http://llm:8080/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = "SUM_KEY"
    cfg.storage.path = tmp_path / "storage"
    cfg.recordings_root = cfg.storage.path / "recordings"
    monkeypatch.setattr(activities, "_cfg", cfg)


def _seed_recordings() -> list[str]:
    """Three done recordings carrying the tag, one without.

    created_at is staggered so DESC returns them in deterministic
    insertion order — three uuid4 calls happen too close in time for
    microseconds alone to order them reliably.
    """
    import uuid

    base = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    rows = [
        ("pathfinder", "The Lost Coast", True, base + timedelta(days=2)),
        ("pathfinder", "Hook Mountain", True, base + timedelta(days=1)),
        ("pathfinder", "Trouble in Sandpoint", True, base),
        ("standup", "Other", False, base + timedelta(days=3)),
    ]
    rids: list[str] = []
    for tag, title, _has_tag, ts in rows:
        rid = str(uuid.uuid4())
        with session() as s:
            s.add(
                Recording(
                    id=rid,
                    title=title,
                    tags=[tag],
                    state=RecordingState.done,
                    sha256="x" * 64,
                    committed_bytes=100,
                    total_bytes=100,
                    duration_sec=60.0,
                    created_at=ts,
                )
            )
            for kind in ("chunk", "transcribe", "diarize", "merge_speakers", "summarize", "enrich"):
                s.add(Stage(recording_id=rid, kind=kind))
            s.commit()
        # Timeline artifact: written by enrich before any digest run —
        # run_digest drops rows without it (purge wipes them).
        cfg = activities._cfg
        assert cfg is not None
        meta = cfg.recordings_root / rid / "meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "events.json").write_text("{}", encoding="utf-8")
        rids.append(rid)
    return rids


def _make_graph_slice() -> DigestGraphSlice:
    """One entity seen across two sessions + two events + one relation."""
    return DigestGraphSlice(
        entities=[
            {
                "label": "Ameiko",
                "type": "character",
                "sessions": ["r1", "r2"],
                "session_count": 2,
            },
            {
                "label": "Sandpoint",
                "type": "place",
                "sessions": ["r1"],
                "session_count": 1,
            },
        ],
        events=[
            {"origin": "r1", "kind": "combat", "ts": "2026-08-01T00:00:00Z", "summary": "ambush"},
            {"origin": "r2", "kind": "rp", "ts": "2026-08-02T00:00:00Z", "summary": "tavern chat"},
        ],
        relations=[
            {
                "from": "Ameiko",
                "rel": "LIVES_IN",
                "to": "Sandpoint",
                "from_slug": "ameiko",
                "to_slug": "sandpoint",
            },
        ],
    )


# ---------- safe_filename ------------------------------------------------------


class TestSafeFilename:
    def test_accepts_alnum(self) -> None:
        assert safe_filename("pathfinder") == "pathfinder.md"

    def test_dots_slug_to_dashes(self) -> None:
        """Phase 0: the filename is the tag's SLUG — punctuation maps to
        dashes (enrich.slugify), the display tag lives in frontmatter."""
        assert safe_filename("foo.bar") == "foo-bar.md"

    def test_accepts_dashed(self) -> None:
        assert safe_filename("morning-standup") == "morning-standup.md"

    def test_spaces_slug_to_dashes(self) -> None:
        """Free tags may contain spaces (API regex allows them); the
        filename slugs them out."""
        assert safe_filename("dnd dark castle") == "dnd-dark-castle.md"

    def test_cyrillic_survives_slug(self) -> None:
        assert safe_filename("Мой Замок") == "мой-замок.md"

    def test_rejects_leading_dash(self) -> None:
        with pytest.raises(ValueError, match="not file-safe"):
            safe_filename("-leading")

    def test_rejects_leading_space(self) -> None:
        with pytest.raises(ValueError, match="not file-safe"):
            safe_filename(" leading")

    def test_rejects_dollar(self) -> None:
        with pytest.raises(ValueError, match="not file-safe"):
            safe_filename("a$b")

    def test_slug_collision_disambiguated(
        self, tmp_path: Path
    ) -> None:
        """'dnd dark castle' and 'dnd-dark-castle' slug to the SAME name;
        the second write gets the -2 suffix so both digests survive."""
        from worker.digest import _disambiguate_filename, safe_filename

        digests = tmp_path / "digests"
        digests.mkdir()
        first = _disambiguate_filename(digests, safe_filename("dnd dark castle"))
        (digests / first).write_text("one", encoding="utf-8")
        second = _disambiguate_filename(digests, safe_filename("dnd-dark-castle"))
        assert first == "dnd-dark-castle.md"
        assert second == "dnd-dark-castle-2.md"


# ---------- _atomic_write ------------------------------------------------------


class TestAtomicWrite:
    def test_writes_to_target(self, tmp_path: Path) -> None:
        target = tmp_path / "out.md"
        _atomic_write(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_cleans_up_tmp_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "out.md"
        _atomic_write(target, "x")
        # No leftover .tmp files in the dir.
        leftover = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftover == []

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "out.md"
        target.write_text("old", encoding="utf-8")
        _atomic_write(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_creates_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "digests" / "sub" / "out.md"
        _atomic_write(target, "deep")
        assert target.exists()


# ---------- _render_prompt -----------------------------------------------------


def test_render_prompt_shows_title_date_not_raw_uuids() -> None:
    """Phase 1: session lines are 'title (YYYY-MM-DD)' — raw recording
    UUIDs must not leak into the prompt (only mapped via origin)."""
    rows = [
        DigestRow("r1", "T1", datetime(2026, 8, 1, tzinfo=UTC),
                  datetime(2026, 8, 1, tzinfo=UTC)),
        DigestRow("r2", "T2", datetime(2026, 8, 2, tzinfo=UTC),
                  datetime(2026, 8, 2, tzinfo=UTC)),
    ]
    graph = _make_graph_slice()
    prompt = _render_prompt("pathfinder", 3, rows, graph)
    assert "Tag: pathfinder" in prompt
    # Sessions header lists title (date) pairs, not UUIDs.
    assert "Sessions: T1 (2026-08-01), T2 (2026-08-02)" in prompt
    # Event lines carry session title (date), not the raw origin id.
    assert "T1 (2026-08-01) [combat @ 2026-08-01T00:00:00Z] ambush" in prompt
    assert "T2 (2026-08-02) [rp @ 2026-08-02T00:00:00Z] tavern chat" in prompt
    assert "Ameiko" in prompt
    assert "Sandpoint" in prompt
    # Raw recording ids must NOT appear anywhere in the rendered prompt.
    assert "r1" not in prompt
    assert "r2" not in prompt


def test_render_prompt_says_none_when_empty() -> None:
    rows = [DigestRow("r1", "T1", datetime(2026, 8, 1, tzinfo=UTC),
                      datetime(2026, 8, 1, tzinfo=UTC))]
    graph = DigestGraphSlice(entities=[], events=[], relations=[])
    prompt = _render_prompt("pathfinder", 1, rows, graph)
    assert "(none)" in prompt
    assert "Sessions: T1 (2026-08-01)" in prompt


# ---------- build_digest_input (Postgres + Neo4j) -----------------------------


def test_build_digest_input_pulls_done_recordings_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rids = _seed_recordings()
    slice_ = _make_graph_slice()
    # Patch the graph read to return a stable slice without touching neo4j.
    monkeypatch.setattr(
        "worker.digest._fetch_graph_slice",
        lambda *a, **kw: slice_,
    )

    cfg = activities._cfg
    inp = build_digest_input("pathfinder", 2, cfg)
    # Three recordings exist with the tag; limit=2 returns the two
    # newest by created_at DESC — the seeds were inserted newest-first.
    assert [r.recording_id for r in inp.rows] == rids[:2]
    assert inp.tag == "pathfinder"
    assert inp.last_n == 2


def test_build_digest_input_excludes_non_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording in `uploading`/`processing` state must not appear
    even if it carries the tag — the digest is about completed work."""
    import uuid

    base = datetime(2026, 8, 1, tzinfo=UTC)
    # Insert one uploading row tagged with the searched tag.
    rid = str(uuid.uuid4())
    with session() as s:
        s.add(
            Recording(
                id=rid,
                title="In flight",
                tags=["pathfinder"],
                state=RecordingState.processing,
                sha256="x" * 64,
                committed_bytes=0,
                total_bytes=100,
                duration_sec=None,
                created_at=base,
            )
        )
        s.commit()
    monkeypatch.setattr(
        "worker.digest._fetch_graph_slice",
        lambda *a, **kw: DigestGraphSlice(entities=[], events=[], relations=[]),
    )
    cfg = activities._cfg
    inp = build_digest_input("pathfinder", 5, cfg)
    assert rid not in [r.recording_id for r in inp.rows]


def test_build_digest_input_handles_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "worker.digest._fetch_graph_slice",
        lambda *a, **kw: DigestGraphSlice(entities=[], events=[], relations=[]),
    )
    cfg = activities._cfg
    inp = build_digest_input("missing", 5, cfg)
    assert inp.rows == []
    assert inp.graph.entities == []


def test_build_digest_input_include_recording_id_rides_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-digest path: the just-enriched recording is still
    `processing` (finalize runs after the activity) yet MUST be selected
    — the 2026-09-04 pathfinder race."""
    import uuid

    base = datetime(2026, 8, 1, tzinfo=UTC)
    rid = str(uuid.uuid4())
    with session() as s:
        s.add(
            Recording(
                id=rid,
                title="Enriching now",
                tags=["pathfinder"],
                state=RecordingState.processing,
                sha256="x" * 64,
                committed_bytes=0,
                total_bytes=100,
                duration_sec=None,
                created_at=base + timedelta(days=5),
            )
        )
        s.commit()
    monkeypatch.setattr(
        "worker.digest._fetch_graph_slice",
        lambda *a, **kw: DigestGraphSlice(entities=[], events=[], relations=[]),
    )
    cfg = activities._cfg
    inp = build_digest_input("pathfinder", 5, cfg, include_recording_id=rid)
    assert rid in [r.recording_id for r in inp.rows]


def test_run_digest_drops_rows_without_events_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Purged-but-not-rebuilt recordings (no meta/events.json) must not
    surface empty sessions in the digest; a row with an artifact survives."""
    rids = _seed_recordings()  # three done pathfinder recordings, all with artifacts
    cfg = activities._cfg
    assert cfg is not None
    # Wipe two artifacts — the purge outcome for single-tag recordings.
    for rid in rids[1:]:
        (cfg.recordings_root / rid / "meta" / "events.json").unlink()

    monkeypatch.setattr(
        "worker.digest._fetch_graph_slice",
        lambda *a, **kw: DigestGraphSlice(entities=[], events=[], relations=[]),
    )
    monkeypatch.setattr(
        "worker.digest._call_llm", lambda prompt, cfg: "# Digest\n\nbody"
    )
    result = run_digest("pathfinder", 5, cfg, cfg.vault.path)
    assert result["written"] is True
    assert result["recordings"] == [rids[0]]  # artifact-less rows dropped


def test_run_digest_all_rows_without_artifact_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = activities._cfg
    assert cfg is not None
    _seed_recordings()
    # Wipe EVERY artifact: nothing survives the filter.
    for f in (cfg.recordings_root).glob("*/meta/events.json"):
        f.unlink()
    monkeypatch.setattr(
        "worker.digest._fetch_graph_slice",
        lambda *a, **kw: DigestGraphSlice(entities=[], events=[], relations=[]),
    )
    result = run_digest("pathfinder", 5, cfg, cfg.vault.path)
    assert result["written"] is False
    assert "timeline artifact" in result["reason"]


# ---------- write_digest -------------------------------------------------------


def test_write_digest_emits_frontmatter_and_body(tmp_path: Path) -> None:
    rows = [
        DigestRow("r1", "T1", datetime.now(UTC), datetime.now(UTC)),
        DigestRow("r2", "T2", datetime.now(UTC), datetime.now(UTC)),
    ]
    inp = DigestInput(
        tag="pathfinder",
        last_n=2,
        rows=rows,
        graph=DigestGraphSlice(entities=[], events=[], relations=[]),
    )
    body = "# My digest\n\ntext"
    transcripts = tmp_path / "transcripts"
    path = write_digest(transcripts, inp, body)
    assert path == transcripts / "digests" / "pathfinder.md"
    content = path.read_text(encoding="utf-8")
    # Frontmatter parses back.
    assert content.startswith("---\n")
    end = content.index("\n---\n", 4)
    fm = yaml.safe_load(content[4:end])
    assert fm["tag"] == "pathfinder"
    assert fm["recordings"] == ["r1", "r2"]
    assert fm["count"] == 2
    assert "generated_at" in fm
    # Body survives verbatim.
    assert "# My digest" in content[end + 5 :]


def test_write_digest_overwrites_existing_tag_file_in_place(tmp_path: Path) -> None:
    """Regeneration must refresh the tag's EXISTING note (frontmatter
    match, even under a -N collision name), not pile up -2/-3 copies —
    the auto-digest path would otherwise mint a new file every window
    (observed live: untagged.md + untagged-2.md for the same tag)."""
    transcripts = tmp_path / "transcripts"
    inp = DigestInput(
        tag="untagged",
        last_n=1,
        rows=[DigestRow("r1", "T1", datetime.now(UTC), datetime.now(UTC))],
        graph=DigestGraphSlice(entities=[], events=[], relations=[]),
    )
    first = write_digest(transcripts, inp, "first body")
    # Simulate a slug-collision rename from another tag: same tag under -2.
    second = first.with_name("untagged-2.md")
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    first.unlink()

    path = write_digest(transcripts, inp, "second body")
    assert path == second, "must overwrite the existing -2 file for the tag"
    assert "second body" in path.read_text(encoding="utf-8")
    # No new files minted.
    assert sorted(p.name for p in (transcripts / "digests").glob("*.md")) == [
        "untagged-2.md"
    ]


# ---------- run_digest ----------------------------------------------------------


def _mock_llm_post(monkeypatch: pytest.MonkeyPatch, body: str) -> Any:
    captured: dict[str, Any] = {}

    def _post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        r = MagicMock()
        r.json.return_value = {"choices": [{"message": {"content": body}}]}
        r.raise_for_status = MagicMock()
        return r

    monkeypatch.setattr("worker.digest.httpx.post", _post)
    return captured


def _patch_graph(monkeypatch: pytest.MonkeyPatch, slice_: DigestGraphSlice) -> None:
    monkeypatch.setattr(
        "worker.digest._fetch_graph_slice",
        lambda *a, **kw: slice_,
    )


def test_run_digest_writes_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_recordings()
    _patch_graph(monkeypatch, _make_graph_slice())
    captured = _mock_llm_post(monkeypatch, "# digest body")

    cfg = activities._cfg
    transcripts = cfg.vault.path
    result = asyncio.run(
        asyncio.to_thread(run_digest, "pathfinder", 2, cfg, transcripts)
    )
    assert result["written"] is True
    assert result["count"] == 2
    assert "pathfinder.md" in result["path"]
    written = Path(result["path"]).read_text(encoding="utf-8")
    assert "# digest body" in written
    # The LLM call went out with our prompt.
    assert captured["json"]["messages"][1]["role"] == "user"
    assert "Tag: pathfinder" in captured["json"]["messages"][1]["content"]
    # HTTP budget 30 s under the Temporal ceiling.
    assert captured["timeout"] == 2370.0


def test_run_digest_empty_selection_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """No done recordings carry the tag → no file, no LLM call."""
    _mock_llm_post(monkeypatch, "should never be sent")
    cfg = activities._cfg
    transcripts = cfg.vault.path
    result = asyncio.run(
        asyncio.to_thread(run_digest, "missing", 5, cfg, transcripts)
    )
    assert result["written"] is False
    assert "no done recordings" in result["reason"]
    assert not (transcripts / "digests").exists()


def test_run_digest_llm_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_recordings()
    _patch_graph(monkeypatch, _make_graph_slice())

    def _boom(*a, **kw):
        raise httpx.HTTPError("llm down")

    monkeypatch.setattr("worker.digest.httpx.post", _boom)
    cfg = activities._cfg
    with pytest.raises(httpx.HTTPError, match="llm down"):
        asyncio.run(
            asyncio.to_thread(run_digest, "pathfinder", 2, cfg, cfg.vault.path)
        )


def test_run_digest_invalid_tag_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanitization is defensive — the API already rejects bad tags, but
    an internal caller bypassing it must surface a clear ValueError.
    ("Bad Tag" is VALID under the Phase 0 unicode regex — case is the
    display-tag's business; punctuation outside the whitelist is not.)"""
    cfg = activities._cfg
    with pytest.raises(ValueError, match="not file-safe"):
        asyncio.run(
            asyncio.to_thread(run_digest, "bad$tag", 2, cfg, cfg.vault.path)
        )


# ---------- activity wiring ----------------------------------------------------


def test_tag_digest_activity_registered() -> None:
    """Guard from main.ACTIVITIES: tag_digest must be in the registration
    list or the workflow dies at runtime with NotFoundError."""
    import worker.activities as activities_mod
    import worker.main as main_mod

    registered = {fn.__name__ for fn in main_mod.ACTIVITIES}
    defined = {
        name
        for name, fn in vars(activities_mod).items()
        if callable(fn) and hasattr(fn, "__temporal_activity_definition")
    }
    assert "tag_digest" in registered
    assert "tag_digest" in defined


def test_tag_digest_activity_raises_when_graph_disabled() -> None:
    cfg = activities._cfg
    cfg.graph.enabled = False
    with pytest.raises(RuntimeError, match="graph backend not configured"):
        asyncio.run(activities.tag_digest({"tag": "pathfinder", "last_n": 5}))


def test_tag_digest_activity_returns_unwritten_when_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recordings carry the tag → activity returns {written: False},
    NOT an exception (the API already returned 202)."""
    _patch_graph(monkeypatch, DigestGraphSlice(entities=[], events=[], relations=[]))
    _mock_llm_post(monkeypatch, "ignored")
    # No recordings seeded → no rows.
    result = asyncio.run(activities.tag_digest({"tag": "missing", "last_n": 5}))
    assert result["written"] is False


def test_tag_digest_activity_writes_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_recordings()
    _patch_graph(monkeypatch, _make_graph_slice())
    _mock_llm_post(monkeypatch, "# digest body")
    result = asyncio.run(activities.tag_digest({"tag": "pathfinder", "last_n": 3}))
    assert result["written"] is True
    assert "pathfinder.md" in result["path"]


def test_tag_digest_activity_propagates_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_recordings()
    _patch_graph(monkeypatch, _make_graph_slice())

    def _boom(*a, **kw):
        raise httpx.HTTPError("llm down")

    monkeypatch.setattr("worker.digest.httpx.post", _boom)
    with pytest.raises(httpx.HTTPError):
        asyncio.run(activities.tag_digest({"tag": "pathfinder", "last_n": 3}))


# ---------- workflow definition -------------------------------------------------

def test_tag_digest_workflow_and_activity_registered() -> None:
    """End-to-end guard: the workflow class exists, the activity is in
    ACTIVITIES (the registration guard already enforces this for the
    defined-vs-registered set, but we re-state it here at the workflow
    level so a workflow refactor can't quietly drop the binding)."""
    import worker.main as main_mod
    from worker.workflows import TagDigest

    assert TagDigest is not None
    assert "run" in dir(TagDigest), (
        "TagDigest must be a Temporal workflow class with @workflow.run"
    )
    assert "tag_digest" in {fn.__name__ for fn in main_mod.ACTIVITIES}