"""Enrich activity integration: skipped/done/failed paths.

The graph backend and the LLM are both mocked. The activity's own
branching (skipped without profile or graph, done on success, failed
on persistent errors) is what these tests cover — the unit-level
extraction/dedup/write logic lives in test_enrich.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.exceptions import ApplicationError

from worker import activities
from worker.db import Base, Recording, RecordingState, Stage, StageStatus, session
from worker.profiles import EnrichNodeLabels, EnrichSpec, Profile, SummarizeSpec


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    monkeypatch.setattr(activities, "_cfg", _make_cfg(tmp_path))
    # Phase 3-F F3: the dedup soft gate would REALLY probe the LLM
    # (and sleep 60/120 s on failure) — unit tests patch it healthy.
    import worker.enrich as _enrich_mod

    monkeypatch.setattr(_enrich_mod, "dedup_llm_gate", lambda cfg: True)


def _make_cfg(tmp_path: Path) -> Any:
    cfg = MagicMock()
    cfg.graph.enabled = True
    cfg.graph.uri = "bolt://n:7687"
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.profiles.path = tmp_path / "profiles"
    # REAL paths under tmp: the fixture writes a real transcript.md into
    # the meta dir and the enrich stage writes a real meta/events.json —
    # both must survive actual filesystem calls.
    cfg.storage.path = tmp_path / "storage"
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    return cfg


@pytest.fixture()
def recording_id(tmp_path: Path) -> str:
    import uuid

    rid = str(uuid.uuid4())
    with session() as s:
        s.add(
            Recording(
                id=rid,
                title="Session 1",
                tags=["pathfinder"],
                state=RecordingState.done,
                sha256="x" * 64,
                committed_bytes=100,
                total_bytes=100,
                duration_sec=60.0,
            )
        )
        s.commit()
    for kind in ("chunk", "transcribe", "diarize", "merge_speakers", "summarize", "enrich"):
        with session() as s:
            s.add(Stage(recording_id=rid, kind=kind))
            s.commit()
    # Transcript under the storage root the activity reads.
    cfg = activities._cfg
    meta = cfg.recordings_root / rid / "meta"
    meta.mkdir(parents=True)
    (meta / "transcript.md").write_text("the meeting transcript", encoding="utf-8")
    return rid


def _make_profile(has_enrich: bool) -> Profile:
    return Profile(
        id="pathfinder",
        version="1.0.0",
        min_host_version="0.10.0",
        display_name="Pathfinder",
        description="d",
        type="ttrpg",
        summarize=SummarizeSpec(prompt="sum {transcript}", output_artifact="session-log.md"),
        enrich=EnrichSpec(prompt="enr {transcript}", node_labels=EnrichNodeLabels())
        if has_enrich
        else None,
    )


def _cfg(graph_enabled: bool, tmp_path: Path) -> Any:
    cfg = MagicMock()
    cfg.graph.enabled = graph_enabled
    cfg.graph.uri = "bolt://n" if graph_enabled else ""
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    # Phase 2 knobs: done-path tests never want a digest LLM call, so
    # auto-digest is off by default here (dedicated tests opt in).
    cfg.graph.enrich_all = True
    cfg.graph.auto_digest = False
    cfg.graph.auto_digest_window_sec = 3600
    # Phase 3-F F3: the dedup soft gate reads summarize.* for its probe.
    cfg.summarize.base_url = "http://llm:8080/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    cfg.profiles.path = tmp_path / "profiles"
    # Real paths: the done-path tests let the activity write
    # meta/events.json for real (write_events_json uses os.replace).
    cfg.storage.path = tmp_path / "storage"
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    return cfg


def _run(coro):
    return asyncio.run(coro)


def test_skipped_when_graph_disabled(recording_id: str, tmp_path: Path) -> None:
    """Phase 3-F F2: intentional skips raise a NON-RETRYABLE
    ApplicationError (the row still says skipped; Temporal sees a
    terminal failure the retry policy can never re-run)."""
    cfg = _cfg(graph_enabled=False, tmp_path=tmp_path)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=_make_profile(has_enrich=True)),
        pytest.raises(ApplicationError, match="graph disabled"),
    ):
        _run(activities.enrich(recording_id))
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.skipped


def test_skipped_when_no_profile_with_enrich(recording_id: str, tmp_path: Path) -> None:
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=_make_profile(has_enrich=False)),
        pytest.raises(ApplicationError, match="no profile with enrich"),
    ):
        _run(activities.enrich(recording_id))
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.skipped


def test_skipped_when_no_profile_at_all(recording_id: str, tmp_path: Path) -> None:
    """Phase 2: skip requires enrich_all=False — with the flag on (the
    default) a no-match recording takes the builtin-fallback path
    instead (see tests/test_enrich_fallback.py)."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    cfg.graph.enrich_all = False
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        pytest.raises(ApplicationError, match="no profile with enrich"),
    ):
        _run(activities.enrich(recording_id))


def test_done_when_extraction_and_write_succeed(recording_id: str, tmp_path: Path) -> None:
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    profile = _make_profile(has_enrich=True)
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch(
            "worker.enrich.extract_from_transcript",
            return_value=extracted_graph,
        ),
        patch(
            "worker.enrich.resolve_slugs",
            return_value=extracted_graph,
        ),
        patch(
            "worker.enrich.pre_existing_lookup",
            return_value=MagicMock(),
        ),
        patch(
            "worker.enrich.write_to_graph",
            return_value=0,
        ),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))
    assert result["profile_id"] == "pathfinder"
    assert result["events"] == 0
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done
        assert st.details["profile_id"] == "pathfinder"


def test_failed_when_extraction_raises(recording_id: str, tmp_path: Path) -> None:
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    profile = _make_profile(has_enrich=True)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch(
            "worker.enrich.extract_from_transcript",
            side_effect=ValueError("LLM gave up"),
        ),pytest.raises(ValueError, match="LLM gave up")
    ):
        _run(activities.enrich(recording_id))
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.failed
        assert "LLM gave up" in st.last_error


def test_done_when_dedup_fails_falls_back_to_raw(recording_id: str, tmp_path: Path) -> None:
    """Dedup is best-effort: a flakey LLM or lookup must never kill the
    stage. The activity falls back to the raw extraction graph and
    still marks the stage done."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    profile = _make_profile(has_enrich=True)
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []

    lookup_mock = MagicMock()
    lookup_mock._driver.close = MagicMock()

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch(
            "worker.enrich.extract_from_transcript",
            return_value=extracted_graph,
        ),
        patch(
            "worker.enrich.resolve_slugs",
            side_effect=RuntimeError("lookup died"),
        ),
        patch(
            "worker.enrich.pre_existing_lookup",
            return_value=lookup_mock,
        ),
        patch(
            "worker.enrich.write_to_graph",
            return_value=0,
        ),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))
    assert result["profile_id"] == "pathfinder"
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done


def test_done_writes_events_json(recording_id: str, tmp_path: Path) -> None:
    """Phase 1 contract: a successful enrich run writes meta/events.json
    with the locked client shape — recording identity, profile, the tag
    namespaces, per-event mentions (label-occurrence approximation),
    and the FIRST namespace's entities/relations."""
    import json as json_mod

    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    profile = _make_profile(has_enrich=True)

    from worker.enrich import ExtractedEntity, ExtractedEvent, ExtractedGraph, ExtractedRelation

    extraction = ExtractedGraph(
        events=[
            ExtractedEvent(ts="00:42:13", kind="decision", summary="Release Q3 postponed"),
            ExtractedEvent(ts="00:50:00", kind="note", summary="nothing referenced"),
        ],
        entities=[
            ExtractedEntity(slug="release-q3", label="Release Q3", type="project"),
        ],
        relations=[
            ExtractedRelation(from_slug="release-q3", to_slug="release-q3", type="SELF"),
        ],
    )

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch("worker.enrich.extract_from_transcript", return_value=extraction),
        patch("worker.enrich.resolve_slugs", return_value=extraction),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=1) as wtg,
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))
    assert result["profile_id"] == "pathfinder"
    # write_to_graph received the timeline keys from the recording row.
    assert wtg.call_count == 1  # single namespace: tags=["pathfinder"]

    events_path = activities.meta_dir(recording_id) / "events.json"
    assert events_path.exists(), "meta/events.json must be written on success"
    data = json_mod.loads(events_path.read_text(encoding="utf-8"))
    assert data["recording_id"] == recording_id
    assert data["recording_title"] == "Session 1"
    assert data["recording_date"]  # ISO string from recorded_at/created_at
    assert data["profile_id"] == "pathfinder"
    assert data["namespaces"] == ["pathfinder"]
    from worker.enrich import compute_event_keys

    keys = compute_event_keys(
        recording_id,
        [
            ExtractedEvent(ts="00:42:13", kind="decision", summary="Release Q3 postponed"),
            ExtractedEvent(ts="00:50:00", kind="note", summary="nothing referenced"),
        ],
    )
    assert data["events"] == [
        {
            "event_key": keys[0],
            "ts": "00:42:13",
            "kind": "decision",
            "summary": "Release Q3 postponed",
            "mentions": ["release-q3"],  # label "Release Q3" occurs in summary
        },
        {
            "event_key": keys[1],
            "ts": "00:50:00",
            "kind": "note",
            "summary": "nothing referenced",
            "mentions": [],
        },
    ]
    assert data["entities"] == [{"slug": "release-q3", "label": "Release Q3", "type": "project"}]
    assert data["relations"] == [{"from": "release-q3", "to": "release-q3", "type": "SELF"}]

# --- Phase 2: known-entities lookup wiring -----------------------------------


def test_known_entities_lookup_skipped_when_prompt_lacks_placeholder(
    recording_id: str, tmp_path: Path
) -> None:
    """Zero-cost contract: a profile with known_entities=False (default)
    must never touch the graph for the block."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    profile = _make_profile(has_enrich=True)  # prompt has no {known_entities}
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch("worker.enrich.extract_from_transcript", return_value=extracted_graph),
        patch("worker.enrich.resolve_slugs", return_value=extracted_graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch("worker.enrich.list_known_entities") as lke,
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        _run(activities.enrich(recording_id))
    lke.assert_not_called()


def test_known_entities_lookup_runs_for_first_namespace(
    recording_id: str, tmp_path: Path
) -> None:
    """Enabled + placeholder → exactly one snapshot, from the FIRST
    namespace, with the recording excluded (regenerate must not be
    steered by the nodes it is about to delete)."""
    from worker.profiles import EnrichSpec

    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    profile = _make_profile(has_enrich=True)
    # Enable the lookup on the profile (placeholder + true).
    profile.enrich = EnrichSpec(
        prompt="enr {transcript}\nKnown:\n{known_entities}\n", known_entities=True
    )
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []
    captured: dict[str, Any] = {}

    def fake_list(uri, user, password, database, tag, exclude_rec, limit):
        captured.update(
            uri=uri, tag=tag, exclude_rec=exclude_rec, limit=limit
        )
        return [
            {"slug": "galahad", "label": "Galahad", "type": "character"},
        ]

    sent: dict[str, Any] = {}

    def fake_extract(path, title, prompt_template, c, known_entities=""):
        sent["block"] = known_entities
        return extracted_graph

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch(
            "worker.enrich.extract_from_transcript", side_effect=fake_extract
        ),
        patch("worker.enrich.resolve_slugs", return_value=extracted_graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch("worker.enrich.list_known_entities", side_effect=fake_list),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        _run(activities.enrich(recording_id))
    assert captured["tag"] == "pathfinder"  # first (only) namespace
    assert captured["exclude_rec"] == recording_id
    assert captured["limit"] == 25  # known_entities: true → default cap
    assert captured["uri"] == "bolt://n"
    assert "- galahad — Galahad (character)" in sent["block"]


# --- Phase 2.5: embedding vectors threaded to the graph write ------------------


def _extracted_graph() -> Any:
    from worker.enrich import ExtractedEntity, ExtractedEvent, ExtractedGraph

    return ExtractedGraph(
        events=[ExtractedEvent(ts="00:01", kind="info", summary="s")],
        entities=[
            ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ExtractedEntity(slug="orc", label="Orc", type="npc"),
        ],
        relations=[],
    )


def test_done_passes_final_slug_vectors_to_write(recording_id: str, tmp_path: Path) -> None:
    """The enrich activity embeds the RESOLVED entities once per
    namespace and hands write_to_graph the FINAL-slug → vector dict.
    Graph/LLM stubbed; the embedder is faked at the activity boundary."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    cfg.graph.embed_enabled = True
    cfg.graph.embed_model_path = "/models/bge-m3-int8"
    extraction = _extracted_graph()
    captured: dict[str, Any] = {}

    def fake_wtg(rec_id, tag, graph, node_labels, *args, **kwargs):
        captured["tag"] = tag
        captured["embeddings"] = kwargs.get("embeddings")
        return 1

    fake_embedder = MagicMock()
    fake_embedder.embed.side_effect = lambda texts: [
        [float(len(t)), 1.0, 0.0] for t in texts
    ]

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=_make_profile(has_enrich=True)),
        patch("worker.enrich.extract_from_transcript", return_value=extraction),
        patch("worker.enrich.resolve_slugs", side_effect=lambda g, c, t, lookup=None: g),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", side_effect=fake_wtg) as wtg,
        patch("worker.embeddings._embedder", return_value=fake_embedder) as emb,
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        _run(activities.enrich(recording_id))

    # entity_vectors consulted the singleton with the activity config.
    emb.assert_called_once()
    # write_to_graph got the vectors keyed by FINAL slug.
    assert wtg.call_count == 1
    embeddings = captured["embeddings"]
    assert embeddings is not None
    assert set(embeddings) == {e.slug for e in extraction.entities}
    assert all(len(v) == 3 for v in embeddings.values())
    assert captured["tag"] == "pathfinder"


def test_done_skips_embeddings_when_model_unavailable(recording_id: str, tmp_path: Path) -> None:
    """Model unavailable → write_to_graph called with embeddings=None;
    the stage still completes (graceful degradation)."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    cfg.graph.embed_enabled = True
    captured: dict[str, Any] = {}

    def fake_wtg(rec_id, tag, graph, node_labels, *args, **kwargs):
        captured["embeddings"] = kwargs.get("embeddings")
        return 0

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=_make_profile(has_enrich=True)),
        patch("worker.enrich.extract_from_transcript", return_value=_extracted_graph()),
        patch("worker.enrich.resolve_slugs", side_effect=lambda g, c, t, lookup=None: g),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", side_effect=fake_wtg),
        patch("worker.embeddings._embedder", return_value=None),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        _run(activities.enrich(recording_id))

    assert captured["embeddings"] is None


# --- Phase 3.5: semantic index hook at the end of enrich ------------------------


def test_done_indexes_segments(recording_id: str, tmp_path: Path) -> None:
    """A successful enrich runs index_segments per namespace and the
    details payload carries indexed_segments."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    cfg.vault.path = tmp_path / "transcripts"
    profile = _make_profile(has_enrich=True)
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []
    calls: list[str] = []

    def fake_index(rec_id, tag, title, meta_dir, root, c):
        calls.append(tag)
        return 3

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch("worker.enrich.extract_from_transcript", return_value=extracted_graph),
        patch("worker.enrich.resolve_slugs", return_value=extracted_graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch("worker.semantic_index.index_segments", side_effect=fake_index),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))
    assert calls == ["pathfinder"]
    assert result["indexed_segments"] == 3
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.details["indexed_segments"] == 3


def test_indexing_failure_never_fails_enrich(recording_id: str, tmp_path: Path) -> None:
    """Best-effort contract: a dead embedder logs and reports 0 — the
    stage still completes."""
    cfg = _cfg(graph_enabled=True, tmp_path=tmp_path)
    cfg.vault.path = tmp_path / "transcripts"
    profile = _make_profile(has_enrich=True)
    extracted_graph = MagicMock()
    extracted_graph.events = []
    extracted_graph.entities = []
    extracted_graph.relations = []

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=profile),
        patch("worker.enrich.extract_from_transcript", return_value=extracted_graph),
        patch("worker.enrich.resolve_slugs", return_value=extracted_graph),
        patch("worker.enrich.pre_existing_lookup", return_value=MagicMock()),
        patch("worker.enrich.write_to_graph", return_value=0),
        patch(
            "worker.semantic_index.index_segments",
            side_effect=RuntimeError("embedder down"),
        ),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
    ):
        result = _run(activities.enrich(recording_id))
    assert result["indexed_segments"] == 0
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind="enrich").one()
        assert st.status == StageStatus.done


def test_json_payload_strips_think_tag_and_fences() -> None:
    """qwen3.6 via llama.cpp (reasoning budget 0) leaks a bare `</think>`
    and wraps JSON in ```json fences despite response_format=json_object —
    json.loads on the raw content dies at char 0."""
    from worker.enrich import _json_payload

    raw = '</think>\n\n```json\n{\n  "entities": []\n}\n```'
    assert _json_payload(raw) == '{\n  "entities": []\n}'
    # Plain JSON passes through untouched.
    assert _json_payload('{"entities": []}') == '{"entities": []}'
