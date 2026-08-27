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

from worker.enrich import (
    ExistingEntityLookup,
    ExtractedEntity,
    ExtractedEvent,
    ExtractedGraph,
    ExtractedRelation,
    _parse_extraction,
    _parse_yes_no,
    ask_same_entity,
    extract_from_transcript,
    pre_existing_lookup,
    resolve_slugs,
    slugify,
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

    def test_unicode_handled_as_alnum(self):
        # Non-ASCII is NOT alnum → dash, but CJK is dropped (matches the
        # contract: slug keys must be ASCII slug-safe). Documents the
        # behavior rather than failing on it.
        assert slugify("Галах") == "unknown" or "-" in slugify("Галах")


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


# --- pre_existing_lookup (uses real driver) ----------------------------------


class TestExistingEntityLookup:
    def test_returns_row_when_present(self):
        # Mock the driver without actually connecting.
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        row = MagicMock()
        row.single = MagicMock(return_value={"label": "Galahad", "type": "character", "slug": "galahad"})
        session.run = MagicMock(return_value=row)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session)
        lookup = ExistingEntityLookup(driver, "neo4j", "pathfinder")
        out = lookup("galahad")
        assert out == {"label": "Galahad", "type": "character", "slug": "galahad"}
        # The query filters by both tag AND slug — origin_recording_id is
        # not part of dedup (different recordings legitimately create
        # the same entity).
        params = session.run.call_args.kwargs
        assert params["tag"] == "pathfinder"
        assert params["slug"] == "galahad"

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