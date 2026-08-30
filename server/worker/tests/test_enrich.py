"""enrich.py unit tests.

The graph is mocked everywhere — these tests must NEVER need a running
Neo4j. They cover:

* slug normalization (cheap, deterministic, always-on)
* LLM extraction: success, retry-on-failure, terminal failure
* dedup: same slug + Y from LLM → merge; same slug + N → -2/-3
* dedup: LLM error → "treat as same" (best-effort)
* write_to_graph: idempotency (DETACH DELETE runs FIRST in the same
  transaction as MERGE/CREATE)

The LLM and graph calls are all stubbed at the boundary so the tests
are fast and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from worker.embeddings import embedder_reset_for_tests
from worker.enrich import (
    _FALLBACK_ENRICH_PROMPT,
    ExistingEntityLookup,
    ExtractedEntity,
    ExtractedEvent,
    ExtractedGraph,
    ExtractedRelation,
    _coerce_event,
    _event_mentions,
    _parse_extraction,
    _parse_yes_no,
    _render_prompt,
    ask_same_entity,
    extract_from_transcript,
    pre_existing_lookup,
    render_known_entities,
    resolve_slugs,
    slugify,
    write_events_json,
    write_to_graph,
)

# --- slugify -----------------------------------------------------------------


class TestSlugify:
    def test_lowercases_and_dashes(self):
        assert slugify("Hello World") == "hello-world"

    def test_strips_punctuation(self):
        assert slugify("Sir Galahad!!") == "sir-galahad"

    def test_collapses_runs(self):
        assert slugify("foo   ---  bar") == "foo-bar"

    def test_empty_falls_back_to_unknown(self):
        assert slugify("") == "unknown"
        assert slugify("!!!") == "unknown"

    def test_unicode_labels_survive(self):
        # The slug is the MERGE key and Cyrillic must survive: an
        # ASCII-only mapping collapses every non-ASCII label to "unknown",
        # folding distinct entities into one node (roborev 2022 HIGH).
        assert slugify("Галах") == "галах"
        assert slugify("Сэр Галах!") == "сэр-галах"


# --- _parse_extraction -------------------------------------------------------


def _ok_cfg() -> Any:
    cfg = MagicMock()
    cfg.summarize.base_url = "http://localhost:1234/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    return cfg


def _write_transcript(tmp_path: Path) -> Path:
    p = tmp_path / "transcript.md"
    p.write_text("the meeting transcript", encoding="utf-8")
    return p


class TestParseExtraction:
    def test_full_payload(self):
        payload = {
            "events": [
                {"ts": "00:05", "kind": "combat", "summary": "Galahad attacked the orc"},
            ],
            "entities": [
                {"slug": "galahad", "label": "Sir Galahad", "type": "character"},
                {"slug": "orc", "label": "Orc Boss", "type": "npc"},
            ],
            "relations": [
                {"from_slug": "galahad", "to_slug": "orc", "type": "attacked"},
            ],
        }
        g = _parse_extraction(payload)
        assert len(g.events) == 1
        assert g.events[0].kind == "combat"
        assert len(g.entities) == 2
        assert g.entities[0].slug == "galahad"
        assert len(g.relations) == 1

    def test_missing_keys_default_to_empty(self):
        g = _parse_extraction({})
        assert g.events == []
        assert g.entities == []
        assert g.relations == []

    def test_non_object_raises(self):
        with pytest.raises(TypeError, match="not a JSON object"):
            _parse_extraction([])  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="not a JSON object"):
            _parse_extraction("just a string")  # type: ignore[arg-type]

    def test_bad_rows_are_dropped_not_failed(self):
        # Items missing required fields are dropped (silently tolerated);
        # non-dict entries are dropped too. The rest survives.
        payload = {
            "events": [
                {"ts": "ok", "kind": "k", "summary": "s"},
                "garbage",
                {"kind": "no ts"},  # missing ts → dropped
                {"ts": "no summary", "kind": "k"},  # missing summary → dropped
            ],
            "entities": [
                {"label": "", "type": "char"},  # empty label → dropped
                {"label": "ok", "slug": "ok", "type": "char"},
            ],
            "relations": [
                {"from_slug": "ok", "to_slug": "ok", "type": "knows"},
                "garbage",
                {"from_slug": "missing-to"},  # missing to_slug → dropped
            ],
        }
        g = _parse_extraction(payload)
        assert len(g.events) == 1
        assert len(g.entities) == 1
        assert g.entities[0].slug == "ok"
        assert len(g.relations) == 1


# --- _parse_yes_no -----------------------------------------------------------


class TestParseYesNo:
    @pytest.mark.parametrize("text", ["Y", "y", " yes ", "Y.", "Yes", "Y!"])
    def test_affirmative(self, text):
        assert _parse_yes_no(text) is True

    @pytest.mark.parametrize("text", ["N", "n", " no ", "N.", "No", "N!"])
    def test_negative(self, text):
        assert _parse_yes_no(text) is False

    @pytest.mark.parametrize("text", ["", "  ", "maybe", "???"])
    def test_ambiguous_is_none(self, text):
        assert _parse_yes_no(text) is None


# --- extract_from_transcript (with mocked HTTP) -------------------------------


def _mock_post_json(payload: dict[str, Any], status_code: int = 200) -> Any:
    m = MagicMock()
    m.status_code = status_code
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value={"choices": [{"message": {"content": json.dumps(payload)}}]})
    return m


def _mock_post_text(text: str, status_code: int = 200) -> Any:
    m = MagicMock()
    m.status_code = status_code
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value={"choices": [{"message": {"content": text}}]})
    return m


class TestExtractFromTranscript:
    def test_valid_json_first_try(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        transcript = _write_transcript(tmp_path)
        payload = {
            "events": [{"ts": "00:01", "kind": "intro", "summary": "hi"}],
            "entities": [{"label": "Alice", "slug": "alice", "type": "character"}],
            "relations": [],
        }
        with patch("worker.enrich.httpx.post", return_value=_mock_post_json(payload)):
            g = extract_from_transcript(transcript, "T", "extract {transcript}", _ok_cfg())
        assert len(g.events) == 1
        assert len(g.entities) == 1

    def test_retries_on_non_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        transcript = _write_transcript(tmp_path)
        valid = {
            "events": [],
            "entities": [{"label": "X", "slug": "x", "type": "thing"}],
            "relations": [],
        }
        with patch(
            "worker.enrich.httpx.post",
            side_effect=[
                _mock_post_text("not json"),
                _mock_post_text("also not json"),
                _mock_post_json(valid),
            ],
        ):
            g = extract_from_transcript(transcript, "", "p {transcript}", _ok_cfg())
        assert len(g.entities) == 1

    def test_three_failures_raises(self, tmp_path: Path):
        transcript = _write_transcript(tmp_path)
        with patch(
            "worker.enrich.httpx.post",
            side_effect=[
                _mock_post_text("garbage 1"),
                _mock_post_text("garbage 2"),
                _mock_post_text("garbage 3"),
            ],
        ), pytest.raises(json.JSONDecodeError):
            extract_from_transcript(transcript, "", "p {transcript}", _ok_cfg())

    def test_http_5xx_is_retried(self, tmp_path: Path):
        transcript = _write_transcript(tmp_path)
        good = {"events": [], "entities": [], "relations": []}
        bad = MagicMock()
        bad.status_code = 500
        import httpx

        bad.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=bad)
        )
        bad.json = MagicMock(side_effect=ValueError("no body"))
        with patch(
            "worker.enrich.httpx.post",
            side_effect=[bad, bad, _mock_post_json(good)],
        ):
            g = extract_from_transcript(transcript, "", "p {transcript}", _ok_cfg())
        assert g.entities == []


# --- ask_same_entity (LLM Y/N) ----------------------------------------------


class TestAskSameEntity:
    def test_yes_answer(self):
        cfg = _ok_cfg()
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("Y"),
        ):
            assert ask_same_entity("Galahad", "character", "Sir Galahad", "npc", cfg) is True

    def test_no_answer(self):
        cfg = _ok_cfg()
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("N"),
        ):
            assert ask_same_entity("Galahad", "character", "The Orc", "npc", cfg) is False

    def test_llm_error_treated_as_same(self):
        cfg = _ok_cfg()
        import httpx

        with patch(
            "worker.enrich.httpx.post",
            side_effect=httpx.ConnectError("boom"),
        ):
            assert ask_same_entity("a", "x", "b", "y", cfg) is True

    def test_ambiguous_answer_treated_as_same(self):
        cfg = _ok_cfg()
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("I think maybe?"),
        ):
            assert ask_same_entity("a", "x", "b", "y", cfg) is True


# --- resolve_slugs -----------------------------------------------------------


class TestResolveSlugs:
    def test_no_collisions(self):
        graph = ExtractedGraph(
            events=[],
            entities=[
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
                ExtractedEntity(slug="orc", label="Orc", type="npc"),
            ],
            relations=[],
        )
        cfg = _ok_cfg()
        with patch("worker.enrich.httpx.post") as p:
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        # No collisions → no LLM call, slugs unchanged.
        assert p.call_count == 0
        assert [e.slug for e in out.entities] == ["galahad", "orc"]

    def test_same_slug_yields(self):
        graph = ExtractedGraph(
            events=[],
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Galahad the Brave", type="character"),
            ],
            relations=[],
        )
        cfg = _ok_cfg()
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("Y"),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        # Same → second entity dropped, only one remains.
        assert len(out.entities) == 1
        assert out.entities[0].label == "Sir Galahad"

    def test_same_slug_no_disambiguates(self):
        graph = ExtractedGraph(
            events=[],
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="The Orc", type="npc"),
            ],
            relations=[],
        )
        cfg = _ok_cfg()
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("N"),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        slugs = sorted(e.slug for e in out.entities)
        assert slugs == ["galahad", "galahad-2"]

    def test_three_way_collision(self):
        graph = ExtractedGraph(
            events=[],
            entities=[
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Orc", type="npc"),
                ExtractedEntity(slug="galahad", label="Goblin", type="npc"),
            ],
            relations=[],
        )
        cfg = _ok_cfg()
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("N"),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        slugs = sorted(e.slug for e in out.entities)
        assert slugs == ["galahad", "galahad-2", "galahad-3"]

    def test_existing_lookup_collision_yes(self):
        graph = ExtractedGraph(
            events=[],
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
            ],
            relations=[],
        )
        cfg = _ok_cfg()
        lookup = MagicMock()
        lookup.return_value = {"slug": "galahad", "label": "Sir Galahad", "type": "character"}
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("Y"),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder", existing_lookup=lookup)
        assert [e.slug for e in out.entities] == ["galahad"]

    def test_existing_lookup_collision_no_disambiguates(self):
        graph = ExtractedGraph(
            events=[],
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
            ],
            relations=[],
        )
        cfg = _ok_cfg()
        lookup = MagicMock()
        # First lookup hits; disambiguation also hits (still colliding).
        lookup.side_effect = [
            {"slug": "galahad", "label": "Sir G", "type": "character"},
            {"slug": "galahad-2", "label": "Sir G 2", "type": "character"},
            None,  # third lookup finds a free slot
        ]
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("N"),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder", existing_lookup=lookup)
        assert out.entities[0].slug == "galahad-3"

    def test_relations_follow_canonical_slug_after_disambiguation(self):
        # Regression: re-anchoring used to key the map by LABEL (last
        # duplicate wins), so a relation authored against the pre-resolution
        # slug landed on the DISAMBIGUATED copy (galahad-2) instead of the
        # canonical first entity (galahad).
        graph = ExtractedGraph(
            events=[],
            entities=[
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="galahad", type="npc"),
            ],
            relations=[ExtractedRelation(from_slug="galahad", to_slug="galahad", type="fought")],
        )
        cfg = _ok_cfg()
        with patch(
            "worker.enrich.httpx.post",
            return_value=_mock_post_text("N"),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        assert sorted(e.slug for e in out.entities) == ["galahad", "galahad-2"]
        # First mapping wins: the raw slug refers to the canonical entity.
        assert out.relations[0].from_slug == "galahad"
        assert out.relations[0].to_slug == "galahad"

# --- write_to_graph ----------------------------------------------------------


def _tx_recorder() -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """Build a mock neo4j driver + session that records every Cypher run."""
    runs: list[tuple[str, dict[str, Any]]] = []
    tx = MagicMock()
    tx.run = MagicMock(side_effect=lambda q, **params: (runs.append((q, params)) or _ok_single()))
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.begin_transaction = MagicMock()
    session.begin_transaction.return_value.__enter__ = MagicMock(return_value=tx)
    session.begin_transaction.return_value.__exit__ = MagicMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    driver.close = MagicMock()
    return driver, runs


def _ok_single() -> Any:
    single = MagicMock()
    single.single = MagicMock(return_value=MagicMock(__getitem__=lambda s, k: "node-id"))
    return single


class TestWriteToGraph:
    def test_detach_delete_runs_before_merges(self):
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                events=[
                    ExtractedEvent(ts="00:05", kind="combat", summary="Galahad attacked"),
                ],
                entities=[
                    ExtractedEntity(slug="galahad", label="Galahad", type="character"),
                    ExtractedEntity(slug="orc", label="Orc", type="npc"),
                ],
                relations=[ExtractedRelation(from_slug="galahad", to_slug="orc", type="attacked")],
            )
            from worker.profiles import EnrichNodeLabels

            write_to_graph(
                rec_id="rec-1",
                tag="pathfinder",
                graph=graph,
                node_labels=EnrichNodeLabels(),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
            )
        # DETACH DELETE is the FIRST run.
        first_q, first_params = runs[0]
        assert "DETACH DELETE" in first_q
        assert first_params["rec"] == "rec-1"
        # MERGE for each entity comes after.
        merge_qs = [q for q, _ in runs if "MERGE" in q]
        assert any("Entity" in q for q in merge_qs)
        # Relations create REL edges.
        rel_qs = [q for q, _ in runs if "REL {type" in q]
        assert any("attacked" in str(p.get("type", "")) for _, p in runs if "type" in p)
        assert rel_qs
        # Driver is closed even on success.
        driver.close.assert_called_once()

    def test_idempotency_via_detach_delete(self):
        """A second run on the same recording produces the same graph: the
        DELETE clears anything tagged origin_recording_id=rec-1, so the
        MERGE creates a fresh slate each time."""
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            write_to_graph(
                rec_id="rec-2",
                tag="pathfinder",
                graph=ExtractedGraph(),
                node_labels=MagicMock(event="Event", entity="Entity"),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
            )
        # First statement must be the DELETE; no MERGEs follow (empty graph).
        assert "DETACH DELETE" in runs[0][0]
        assert "MERGE" not in runs[0][0]

    def test_custom_labels_propagate(self):
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                events=[],
                entities=[ExtractedEntity(slug="x", label="X", type="thing")],
                relations=[],
            )
            write_to_graph(
                rec_id="rec-3",
                tag="t",
                graph=graph,
                node_labels=MagicMock(event="CampaignEvent", entity="Thing"),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
            )
        merged_qs = [q for q, _ in runs if "MERGE" in q]
        assert any("Thing" in q for q in merged_qs)
        assert not any("`Event`" in q for q in merged_qs)

    def test_event_props_carry_recording_date_and_title(self):
        """Phase 1: every CREATE Event statement receives
        recording_date (ISO-8601 UTC) and recording_title params, and the
        Cypher sets both as node properties."""
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                events=[
                    ExtractedEvent(ts="00:42:13", kind="decision", summary="release postponed"),
                ],
                entities=[],
                relations=[],
            )
            write_to_graph(
                rec_id="rec-9",
                tag="daily blob",
                graph=graph,
                node_labels=MagicMock(event="Event", entity="Entity"),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
                recording_date="2026-08-29T09:00:00+00:00",
                recording_title="Daily Blob Aug 26",
            )
        event_runs = [(q, p) for q, p in runs if "recording_date" in q]
        assert event_runs, "event CREATE query must set recording_date"
        _q, params = event_runs[0]
        assert params["recording_date"] == "2026-08-29T09:00:00+00:00"
        assert params["recording_title"] == "Daily Blob Aug 26"
        assert params["rec"] == "rec-9"
        assert params["tag"] == "daily blob"


# --- pre_existing_lookup (uses real driver) ----------------------------------


class TestExistingEntityLookup:
    def test_returns_row_when_present(self):
        # Mock the driver without actually connecting.
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        row = MagicMock()
        row.single = MagicMock(
            return_value={
                "label": "Galahad",
                "type": "character",
                "slug": "galahad",
                "embedding": None,
                "user_corrected": False,
            }
        )
        session.run = MagicMock(return_value=row)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        lookup = ExistingEntityLookup(driver, "neo4j", "pathfinder")
        out = lookup("galahad")
        assert out == {
            "label": "Galahad",
            "type": "character",
            "slug": "galahad",
            "embedding": None,
            "user_corrected": False,
        }
        # The query filters by tag AND slug, and EXCLUDES the current
        # recording's own nodes (origin_recording_id <> rec) so a
        # regenerate reclaims its own slugs instead of drifting -2/-3.
        # Default exclude_rec="" keeps every node in play.
        params = session.run.call_args.kwargs
        assert params["tag"] == "pathfinder"
        assert params["slug"] == "galahad"
        assert params["rec"] == ""

    def test_returns_none_when_missing(self):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.run = MagicMock(return_value=MagicMock(single=MagicMock(return_value=None)))
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        lookup = ExistingEntityLookup(driver, "neo4j", "pathfinder")
        assert lookup("nope") is None


def test_pre_existing_lookup_returns_helper_instance() -> None:
    with patch("worker.enrich.GraphDatabase.driver") as gdmock:
        driver = MagicMock()
        gdmock.return_value = driver
        lookup = pre_existing_lookup("bolt://n", "neo4j", "pw", "neo4j", "pathfinder")
    assert isinstance(lookup, ExistingEntityLookup)

def test_render_prompt_tolerates_json_braces():
    """Profile prompts embed JSON schema examples; only {title}/{transcript}
    are placeholders — literal braces must survive (str.format regression)."""
    from worker.enrich import _render_prompt

    out = _render_prompt('Return {"events": []} for «{title}». {transcript}', "t", "BODY")
    assert '{"events": []}' in out and "«t»" in out and "BODY" in out


def test_pre_existing_lookup_instantiates_driver(monkeypatch):
    """Regression: pre_existing_lookup passed the UNCALLED GraphDatabase.driver
    factory into the lookup (2026-08-27): 'function' object has no attribute
    'close'. The factory MUST be called with uri+auth."""
    import worker.enrich as en

    calls = {}

    class _FakeDriver:
        def session(self, database=None):  # pragma: no cover - not needed here
            raise AssertionError("not used")

        def close(self):
            calls["closed"] = True

    def fake_driver(uri, auth=None):
        calls["uri"] = uri
        calls["auth"] = auth
        return _FakeDriver()

    monkeypatch.setattr(en.GraphDatabase, "driver", fake_driver)
    lookup = en.pre_existing_lookup("bolt://x:7687", "neo4j", "pw", "neo4j", tag="t")
    assert calls["uri"] == "bolt://x:7687" and calls["auth"] == ("neo4j", "pw")
    lookup.close()
    assert calls.get("closed") is True


# --- Phase 2: known_entities -------------------------------------------------


class TestRenderKnownEntities:
    def test_empty_is_none_literal(self):
        assert render_known_entities([]) == "(none)"

    def test_one_row_per_line(self):
        rows = [
            {"slug": "galahad", "label": "Galahad", "type": "character"},
            {"slug": "orcus", "label": "Orcus", "type": "npc"},
        ]
        out = render_known_entities(rows)
        assert out == "- galahad — Galahad (character)\n- orcus — Orcus (npc)"


class TestRenderPromptKnownEntities:
    def test_placeholder_replaced(self):
        out = _render_prompt(
            "Known: {known_entities} | {transcript}", "t", "BODY", "- a — A (thing)"
        )
        assert "Known: - a — A (thing)" in out
        assert "{known_entities}" not in out

    def test_default_is_empty_string(self):
        """Legacy calls (no block) render the placeholder as ''."""
        out = _render_prompt("K:{known_entities} {transcript}", "t", "B")
        assert "K: B" in out

    def test_json_braces_still_safe(self):
        out = _render_prompt(
            '{"events": []} {known_entities} {transcript}', "t", "B", "X"
        )
        assert '{"events": []}' in out and "X" in out


class TestExtractKnownEntitiesThreading:
    def test_block_reaches_http_body(self, tmp_path: Path):
        transcript = _write_transcript(tmp_path)
        payload: dict = {"events": [], "entities": [], "relations": []}
        post = MagicMock(return_value=_mock_post_json(payload))
        with patch("worker.enrich.httpx.post", post):
            extract_from_transcript(
                transcript, "T", "K:{known_entities} {transcript}", _ok_cfg(), "BLK"
            )
        sent = post.call_args.kwargs["json"]["messages"][1]["content"]
        assert "K:BLK" in sent

    def test_no_block_when_prompt_lacks_placeholder(self, tmp_path: Path):
        """The contract: placeholder absent → NO lookup happens (zero cost).
        At this layer: an empty block is passed and nothing renders."""
        transcript = _write_transcript(tmp_path)
        payload: dict = {"events": [], "entities": [], "relations": []}
        post = MagicMock(return_value=_mock_post_json(payload))
        with patch("worker.enrich.httpx.post", post):
            extract_from_transcript(
                transcript, "T", "plain {transcript}", _ok_cfg(), ""
            )
        sent = post.call_args.kwargs["json"]["messages"][1]["content"]
        assert "BLK" not in sent


# --- Phase 2: model-declared mentions ----------------------------------------


class TestCoerceEventMentions:
    def test_parses_entities_list(self):
        ev = _coerce_event(
            {"ts": "t", "kind": "k", "summary": "s", "entities": ["Galahad", "orc"]}
        )
        assert ev is not None and ev.mentions == ["galahad", "orc"]

    def test_dedupes_and_slugifies(self):
        ev = _coerce_event(
            {"ts": "t", "kind": "k", "summary": "s", "entities": ["Galahad", "galahad!"]}
        )
        assert ev is not None and ev.mentions == ["galahad"]

    def test_unknown_slugs_dropped(self):
        ev = _coerce_event(
            {
                "ts": "t",
                "kind": "k",
                "summary": "s",
                "entities": ["galahad", "ghost-slug"],
            },
            known_slugs={"galahad"},
        )
        assert ev is not None and ev.mentions == ["galahad"]

    def test_no_entities_field_defaults_empty(self):
        ev = _coerce_event({"ts": "t", "kind": "k", "summary": "s"})
        assert ev is not None and ev.mentions == []

    def test_non_list_entities_ignored(self):
        ev = _coerce_event(
            {"ts": "t", "kind": "k", "summary": "s", "entities": "galahad"}
        )
        assert ev is not None and ev.mentions == []


class TestParseExtractionMentions:
    def test_declared_mentions_kept_when_slug_extracted(self):
        g = _parse_extraction(
            {
                "events": [
                    {
                        "ts": "t",
                        "kind": "k",
                        "summary": "s",
                        "entities": ["galahad"],
                    }
                ],
                "entities": [
                    {"slug": "galahad", "label": "Galahad", "type": "character"}
                ],
                "relations": [],
            }
        )
        assert g.events[0].mentions == ["galahad"]

    def test_declared_mentions_dropped_when_slug_unknown(self):
        g = _parse_extraction(
            {
                "events": [
                    {
                        "ts": "t",
                        "kind": "k",
                        "summary": "s",
                        "entities": ["ghost-slug"],
                    }
                ],
                "entities": [
                    {"slug": "galahad", "label": "Galahad", "type": "character"}
                ],
                "relations": [],
            }
        )
        assert g.events[0].mentions == []


class TestEventMentions:
    def test_declared_wins_over_heuristic(self):
        ev = ExtractedEvent(
            ts="t", kind="k", summary="nothing matching here", mentions=["galahad"]
        )
        ents = [ExtractedEntity(slug="galahad", label="Galahad", type="character")]
        assert _event_mentions(ev, ents) == ["galahad"]

    def test_fallback_heuristic_when_no_declared(self):
        ev = ExtractedEvent(ts="t", kind="k", summary="Galahad attacked the orc")
        ents = [
            ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ExtractedEntity(slug="orc", label="Orc", type="npc"),
        ]
        assert _event_mentions(ev, ents) == ["galahad", "orc"]

    def test_declared_not_revalidated_against_entities(self):
        """The resolver is intentionally dumb about re-validation: coerce
        already filtered against the extraction's entity set."""
        ev = ExtractedEvent(ts="t", kind="k", summary="s", mentions=["kept-slug"])
        assert _event_mentions(ev, []) == ["kept-slug"]


class TestWriteToGraphDeclaredMentions:
    def test_mentions_edges_follow_declared(self):
        """Declared mentions produce MENTIONS edges; the heuristic is NOT
        consulted (the summary deliberately contains no labels)."""
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                events=[
                    ExtractedEvent(
                        ts="00:05",
                        kind="combat",
                        summary="the duel concluded",
                        mentions=["galahad"],
                    ),
                ],
                entities=[
                    ExtractedEntity(slug="galahad", label="Galahad", type="character"),
                    ExtractedEntity(slug="orc", label="Orc", type="npc"),
                ],
                relations=[],
            )
            from worker.profiles import EnrichNodeLabels

            write_to_graph(
                rec_id="rec-m",
                tag="pathfinder",
                graph=graph,
                node_labels=EnrichNodeLabels(),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
            )
        mentions = [(q, p) for q, p in runs if "MENTIONS" in q]
        assert len(mentions) == 1
        # The single edge is galahad's node id.
        assert mentions[0][1]["b"] == "node-id"

    def test_heuristic_fallback_when_no_declared(self):
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                events=[
                    ExtractedEvent(
                        ts="00:05", kind="combat", summary="Galahad attacked"
                    ),
                ],
                entities=[
                    ExtractedEntity(slug="galahad", label="Galahad", type="character"),
                    ExtractedEntity(slug="orc", label="Orc", type="npc"),
                ],
                relations=[],
            )
            from worker.profiles import EnrichNodeLabels

            write_to_graph(
                rec_id="rec-h",
                tag="pathfinder",
                graph=graph,
                node_labels=EnrichNodeLabels(),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
            )
        mentions = [p for q, p in runs if "MENTIONS" in q]
        # Heuristic: only "Galahad" occurs in the summary.
        assert len(mentions) == 1

    def test_events_json_uses_declared_mentions(self, tmp_path: Path):
        """events.json and the graph share one resolver — declared
        mentions flow through to the artifact verbatim."""
        path = tmp_path / "events.json"
        resolved = ExtractedGraph(
            events=[
                ExtractedEvent(
                    ts="t", kind="k", summary="no labels here", mentions=["a", "b"]
                ),
            ],
            entities=[
                ExtractedEntity(slug="a", label="A", type="thing"),
                ExtractedEntity(slug="b", label="B", type="thing"),
            ],
            relations=[],
        )
        write_events_json(
            path,
            recording_id="r",
            recording_date="2026-08-29T00:00:00+00:00",
            recording_title="T",
            profile_id="p",
            namespaces=["n"],
            resolved=resolved,
        )
        import json as _json

        data = _json.loads(path.read_text(encoding="utf-8"))
        assert data["events"][0]["mentions"] == ["a", "b"]


# --- Phase 2: fallback prompt constant ---------------------------------------


class TestFallbackEnrichPrompt:
    def test_carries_all_three_placeholders(self):
        assert "{title}" in _FALLBACK_ENRICH_PROMPT
        assert "{transcript}" in _FALLBACK_ENRICH_PROMPT
        assert "{known_entities}" in _FALLBACK_ENRICH_PROMPT

    def test_valid_with_known_entities_true(self):
        """The fallback prompt must satisfy the EnrichSpec cross-field
        validation used by the activity (known_entities=True)."""
        from worker.profiles import EnrichSpec

        spec = EnrichSpec(prompt=_FALLBACK_ENRICH_PROMPT, known_entities=True)
        assert spec.known_entities is True


# --- Phase 2.5: embedding prefilter -------------------------------------------


def _vec(cos: float, base: list[float] | None = None) -> list[float]:
    """Unit vector at exactly ``cos`` cosine to ``base`` (or e0)."""
    if base is None:
        base = [1.0, 0.0]
    n = sum(x * x for x in base) ** 0.5
    base = [x / n for x in base]
    perp = [base[1], -base[0]]
    return [cos * base[0] + (1 - cos * cos) ** 0.5 * perp[0],
            cos * base[1] + (1 - cos * cos) ** 0.5 * perp[1]]


def _embed_cfg(taus: bool = True) -> Any:
    """Config whose graph knobs claim embed_enabled=True; the EMBEDDER
    itself is monkeypatched per test — no model files involved."""
    cfg = _ok_cfg()
    cfg.graph.embed_enabled = True
    cfg.graph.embed_model_path = "/models/bge-m3-int8"
    if taus:
        cfg.graph.embed_tau_high = 0.90
        cfg.graph.embed_tau_low = 0.75
    return cfg


class _FakeEmbedder:
    """Deterministic label → vector map pretending to be the ONNX
    singleton. Keys not in the mapping → None-vector behavior (raise:
    tests declare every label they expect embedded)."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [self.table[t] for t in texts]


class TestResolveSlugsPrefilter:
    def setup_method(self) -> None:
        embedder_reset_for_tests()

    def test_tau_high_skips_llm(self):
        """cosine >= tau_high → merged WITHOUT any LLM call."""
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ],
            relations=[],
        )
        cfg = _embed_cfg()
        fake = _FakeEmbedder({
            "Sir Galahad": _vec(1.0),
            "Galahad": _vec(0.95),
        })
        with (
            patch("worker.embeddings._embedder", return_value=fake),
            patch("worker.enrich.httpx.post") as p,
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        p.assert_not_called()
        assert len(out.entities) == 1
        assert out.entities[0].label == "Sir Galahad"

    def test_tau_low_skips_llm_and_disambiguates(self):
        """cosine <= tau_low → DISTINCT without the LLM; the candidate
        gets -2 and survives as its own entity."""
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ],
            relations=[],
        )
        cfg = _embed_cfg()
        fake = _FakeEmbedder({
            "Sir Galahad": _vec(1.0),
            "Galahad": _vec(0.3),
        })
        with (
            patch("worker.embeddings._embedder", return_value=fake),
            patch("worker.enrich.httpx.post") as p,
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        p.assert_not_called()
        assert sorted(e.slug for e in out.entities) == ["galahad", "galahad-2"]

    def test_gray_zone_still_asks_llm(self):
        """0.75 < cosine < 0.90 → 'ask' → the LLM Y/N call happens."""
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ],
            relations=[],
        )
        cfg = _embed_cfg()
        fake = _FakeEmbedder({
            "Sir Galahad": _vec(1.0),
            "Galahad": _vec(0.8),
        })
        with (
            patch("worker.embeddings._embedder", return_value=fake),
            patch(
                "worker.enrich.httpx.post",
                return_value=_mock_post_text("Y"),
            ),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        # The LLM WAS consulted (gray zone) and said yes → merged.
        assert len(out.entities) == 1
        assert out.entities[0].label == "Sir Galahad"

    def test_missing_vector_falls_back_to_llm(self):
        """No embedder (unavailable model) → behavior identical to the
        pre-2.5 pure-LLM loop: httpx consulted for the collision."""
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Galahad the Brave", type="character"),
            ],
            relations=[],
        )
        cfg = _embed_cfg()
        with (
            patch("worker.embeddings._embedder", return_value=None),
            patch(
                "worker.enrich.httpx.post",
                return_value=_mock_post_text("Y"),
            ),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        assert len(out.entities) == 1

    def test_embedder_batch_failure_falls_back_to_llm(self):
        """A dying model mid-batch must not fail dedup — pure-LLM path."""
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ],
            relations=[],
        )
        cfg = _embed_cfg()
        boom = MagicMock()
        boom.embed.side_effect = RuntimeError("onnx exploded")
        with (
            patch("worker.embeddings._embedder", return_value=boom),
            patch(
                "worker.enrich.httpx.post",
                return_value=_mock_post_text("Y"),
            ),
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        assert len(out.entities) == 1

    def test_live_graph_embedding_used_for_comparison(self):
        """Live-graph collision: the existing node's stored embedding is
        the comparison vector; tau_high merge happens with no LLM call."""
        graph = ExtractedGraph(
            entities=[ExtractedEntity(slug="galahad", label="Galahad", type="character")],
            relations=[],
        )
        cfg = _embed_cfg()
        fake = _FakeEmbedder({"Galahad": _vec(0.95)})
        lookup_rows = {
            "galahad": {
                "slug": "galahad",
                "label": "Sir Galahad",
                "type": "character",
                "embedding": _vec(1.0),
            },
        }
        lookup = MagicMock(side_effect=lookup_rows.get)
        with (
            patch("worker.embeddings._embedder", return_value=fake),
            patch("worker.enrich.httpx.post") as p,
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder", existing_lookup=lookup)
        p.assert_not_called()
        assert [e.slug for e in out.entities] == ["galahad"]
        assert out.entities[0].label == "Sir Galahad"

    def test_embed_disabled_never_touches_embedder(self):
        """Config-level off → _embedder not even consulted; LLM decides."""
        graph = ExtractedGraph(
            entities=[
                ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
                ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ],
            relations=[],
        )
        cfg = _embed_cfg()
        cfg.graph.embed_enabled = False
        with (
            patch("worker.embeddings._embedder") as emb,
            patch(
                "worker.enrich.httpx.post",
                return_value=_mock_post_text("Y"),
            ) as p,
        ):
            out = resolve_slugs(graph, cfg, tag="pathfinder")
        emb.assert_not_called()
        assert len(out.entities) == 1
        assert p.call_count == 1


class TestWriteToGraphEmbeddings:
    def test_embedding_written_when_dict_present(self):
        driver, runs = _tx_recorder()
        vec = [0.1, 0.2, 0.3]
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                entities=[ExtractedEntity(slug="galahad", label="Galahad", type="character")],
                relations=[],
            )
            write_to_graph(
                rec_id="rec-e1",
                tag="pathfinder",
                graph=graph,
                node_labels=MagicMock(event="Event", entity="Entity"),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
                embeddings={"galahad": vec},
            )
        # ensure_vector_index ran BEFORE the transaction, through its own
        # driver.session() call (the tx-recorder only sees tx.run rows).
        assert driver.session.call_count == 2
        index_session = driver.session.call_args_list[0]
        assert index_session.kwargs["database"] == "neo4j"
        merge_runs = [(q, p) for q, p in runs if "MERGE" in q]
        assert any("e.embedding = $embedding" in q for q, _ in merge_runs)
        params = merge_runs[0][1]
        assert params["embedding"] == vec

    def test_no_dict_no_embedding_clause_no_index(self):
        """embeddings=None → the property is NEVER in the Cypher text and
        ensure_vector_index is not called (pre-2.5 behavior intact)."""
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                entities=[ExtractedEntity(slug="x", label="X", type="thing")],
                relations=[],
            )
            write_to_graph(
                rec_id="rec-e2",
                tag="t",
                graph=graph,
                node_labels=MagicMock(event="Event", entity="Entity"),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
            )
        assert not any("embedding" in q for q, _ in runs)
        merge_qs = [q for q, _ in runs if "MERGE" in q]
        assert all("$embedding" not in q for q in merge_qs)

    def test_missing_key_writes_null(self):
        """A FINAL slug absent from the dict (entity_vectors failure
        recovery) → embedding=None → no corrupt partial vector."""
        driver, runs = _tx_recorder()
        with patch("worker.enrich.GraphDatabase.driver", return_value=driver):
            graph = ExtractedGraph(
                entities=[
                    ExtractedEntity(slug="a", label="A", type="t"),
                    ExtractedEntity(slug="b", label="B", type="t"),
                ],
                relations=[],
            )
            write_to_graph(
                rec_id="rec-e3",
                tag="t",
                graph=graph,
                node_labels=MagicMock(event="Event", entity="Entity"),
                graph_uri="bolt://n",
                graph_user="neo4j",
                graph_password="x",
                graph_database="neo4j",
                embeddings={"a": [1.0, 0.0]},
            )
        by_slug = {p["slug"]: p for _, p in runs if "slug" in p}
        assert by_slug["a"]["embedding"] == [1.0, 0.0]
        assert by_slug["b"]["embedding"] is None


class TestExistingEntityLookupEmbedding:
    def test_row_carries_embedding(self):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        row = MagicMock()
        row.single = MagicMock(
            return_value={
                "label": "Galahad",
                "type": "character",
                "slug": "galahad",
                "embedding": [0.5, 0.5],
                "user_corrected": False,
            }
        )
        session.run = MagicMock(return_value=row)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        lookup = ExistingEntityLookup(driver, "neo4j", "pathfinder")
        out = lookup("galahad")
        assert out["embedding"] == [0.5, 0.5]
        q = session.run.call_args.args[0]
        assert "e.embedding AS embedding" in q
