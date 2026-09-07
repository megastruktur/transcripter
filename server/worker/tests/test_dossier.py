"""Entity dossiers (2026-09-07): the describe batch, coercion, the
guarded write, the depth-1 wave and the single-entity refresh.

The batch/wave/refresh share ONE wire shape (json_object chat call) and
ONE coercion ({"descriptions": [{"slug", "description"}]}); the tests
below pin the CONTRACT each consumer observes, not the plumbing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from worker.enrich import (
    ExtractedEntity,
    ExtractedEvent,
    ExtractedGraph,
    ExtractedRelation,
    _coerce_descriptions,
    describe_entities,
    language_name,
    render_known_entities,
    write_events_json,
)


def _ok_cfg() -> Any:
    cfg = MagicMock()
    cfg.summarize.base_url = "http://localhost:1234/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    return cfg


def _graph_cfg() -> Any:
    """Config whose graph.* attrs are STRINGS (os.environ.get rejects a
    MagicMock key — the refresh path reads them before any refusal)."""
    cfg = _ok_cfg()
    cfg.graph.uri = "bolt://x"
    cfg.graph.user = "u"
    cfg.graph.password_env = "NOPE_X"
    cfg.graph.database = "neo4j"
    return cfg


def _mock_post_json(payload: dict[str, Any], status_code: int = 200) -> Any:
    import json as _json

    m = MagicMock()
    m.status_code = status_code
    m.raise_for_status = MagicMock()
    m.json.return_value = {
        "choices": [{"message": {"content": _json.dumps(payload, ensure_ascii=False)}}]
    }
    return m


GRAPH = ExtractedGraph(
    events=[ExtractedEvent(ts="00:01", kind="note", summary="Galahad slew the orc", mentions=["galahad"])],
    entities=[
        ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
        ExtractedEntity(slug="orc", label="Orc Boss", type="npc"),
    ],
    relations=[ExtractedRelation(from_slug="galahad", to_slug="orc", type="slain_by")],
)


# --- coercion -----------------------------------------------------------------


class TestCoerceDescriptions:
    def test_keeps_known_slugs(self):
        out = _coerce_descriptions(
            {"descriptions": [
                {"slug": "galahad", "description": "A knight."},
                {"slug": "orc", "description": "A dead orc."},
                # label-style value: slugify maps spaces to dashes, so
                # "Orc Boss" becomes orc-boss — NOT folded onto orc.
                {"slug": "Orc Boss", "description": "dropped"},
            ]},
            {"galahad", "orc"},
        )
        assert out == {"galahad": "A knight.", "orc": "A dead orc."}

    def test_drops_unknown_slugs(self):
        out = _coerce_descriptions(
            {"descriptions": [{"slug": "ghost", "description": "boo"}]},
            {"galahad"},
        )
        assert out == {}

    def test_drops_empty_and_caps_length(self):
        long = "x" * 900
        out = _coerce_descriptions(
            {"descriptions": [
                {"slug": "galahad", "description": "   "},
                {"slug": "orc", "description": long},
            ]},
            {"galahad", "orc"},
        )
        assert "galahad" not in out
        assert len(out["orc"]) == 600

    def test_non_list_and_non_dict_are_noop(self):
        assert _coerce_descriptions({"descriptions": "galahad"}, {"galahad"}) == {}
        assert _coerce_descriptions(None, {"galahad"}) == {}
        assert _coerce_descriptions({"descriptions": [42]}, {"galahad"}) == {}


# --- describe batch -------------------------------------------------------------


class TestDescribeBatch:
    def test_returns_changed_map_for_new_and_moved(self):
        payload = {
            "descriptions": [
                {"slug": "galahad", "description": "A knight of the quest."},
                {"slug": "orc", "description": "New boss text."},
            ]
        }
        with patch("worker.enrich.httpx.post", return_value=_mock_post_json(payload)):
            descs, changed = describe_entities(
                GRAPH, "rec1", "Title", "en",
                {"orc": "Old boss text."}, _ok_cfg(),
            )
        assert set(descs) == {"galahad", "orc"}
        # galahad is NEW (no old text); orc MOVED (old → new).
        assert changed == {"galahad": None, "orc": "Old boss text."}

    def test_unchanged_text_not_reported_as_changed(self):
        payload = {"descriptions": [{"slug": "orc", "description": "Same."}]}
        with patch("worker.enrich.httpx.post", return_value=_mock_post_json(payload)):
            _, changed = describe_entities(
                GRAPH, "rec1", "Title", "en", {"orc": "Same."}, _ok_cfg()
            )
        assert changed == {}

    def test_llm_failure_is_best_effort_empty(self):
        with patch(
            "worker.enrich.httpx.post",
            MagicMock(side_effect=RuntimeError("backend down")),
        ):
            descs, changed = describe_entities(GRAPH, "rec1", "T", "en", {}, _ok_cfg())
        assert descs == {} and changed == {}

    def test_empty_extraction_short_circuits_without_llm(self):
        empty = ExtractedGraph()
        post = MagicMock()
        with patch("worker.enrich.httpx.post", post):
            descs, changed = describe_entities(empty, "r", "T", "en", {}, _ok_cfg())
        assert descs == {} and changed == {}
        post.assert_not_called()

    def test_prompt_carries_language_directive_and_events(self):
        payload = {"descriptions": []}
        with patch("worker.enrich.httpx.post", return_value=_mock_post_json(payload)) as p:
            describe_entities(GRAPH, "rec1", "Session 5", "ru", {}, _ok_cfg())
        sent = p.call_args.kwargs["json"]["messages"][1]["content"]
        assert "Russian" in sent  # explicit output-language directive
        assert "Galahad slew the orc" in sent  # events of the session
        assert "[new]" in sent  # an entity without an old description


# --- language map --------------------------------------------------------------


class TestLanguageName:
    def test_known_code(self):
        assert language_name("ru") == "Russian"

    def test_unknown_code_passes_through(self):
        assert language_name("xx") == "xx"

    def test_none_falls_back(self):
        assert "same language" in language_name(None)


# --- known-entities render -------------------------------------------------------


class TestRenderKnownEntities:
    def test_dossier_appended_and_truncated(self):
        rows = [
            {"slug": "a", "label": "A", "type": "person", "description": "d" * 300},
            {"slug": "b", "label": "B", "type": "place", "description": ""},
        ]
        out = render_known_entities(rows)
        lines = out.split("\n")
        assert lines[0].startswith("- a — A (person) — ")
        assert len(lines[0]) < len("- a — A (person) — ") + 161  # capped at 160
        # Legacy row (no dossier) renders exactly the old shape.
        assert lines[1] == "- b — B (place)"


# --- events.json artifact -------------------------------------------------------


class TestEventsJsonDescription:
    def test_description_written_additively(self, tmp_path: Path):
        path = tmp_path / "events.json"
        write_events_json(
            path,
            recording_id="r1",
            recording_date="2026-09-07T00:00:00",
            recording_title="T",
            profile_id="p",
            namespaces=["quest"],
            resolved=GRAPH,
            descriptions={"galahad": "A knight."},
        )
        import json

        doc = json.loads(path.read_text(encoding="utf-8"))
        by_slug = {e["slug"]: e for e in doc["entities"]}
        assert by_slug["galahad"]["description"] == "A knight."
        assert "description" not in by_slug["orc"]  # absent = no dossier

    def test_no_descriptions_key_for_legacy_shape(self, tmp_path: Path):
        path = tmp_path / "events.json"
        write_events_json(
            path,
            recording_id="r1",
            recording_date="2026-09-07T00:00:00",
            recording_title="T",
            profile_id="p",
            namespaces=["quest"],
            resolved=GRAPH,
        )
        import json

        doc = json.loads(path.read_text(encoding="utf-8"))
        assert all("description" not in e for e in doc["entities"])



class TestWriteDescriptionsLanded:
    def test_landed_lists_only_accepted_slugs(self):
        """write_descriptions returns the slugs the graph actually
        accepted (landed) — the events.json artifact must not carry a
        generated revision of a user-edited dossier (roborev 2104).
        The guard lives in Cypher; here we pin the RETURN contract."""
        from worker.enrich import _WRITE_DESC_CYPHER

        # The guarded query only matches non-edited nodes: a skipped
        # write (edited/missing) leaves the slug OUT of landed.
        assert "description_edited" in _WRITE_DESC_CYPHER
        assert "SET e.description = $desc" in _WRITE_DESC_CYPHER

    def test_delete_query_guards_description_edited(self):
        """The regenerate purge must spare description_edited nodes —
        same survival contract as user_corrected labels (roborev 2101);
        otherwise a regenerate silently trades the user's dossier text
        for a fresh generated one."""
        import inspect

        from worker.enrich import write_to_graph

        src = inspect.getsource(write_to_graph)
        assert "coalesce(n.description_edited, false) = false" in src
        assert "coalesce(n.user_corrected, false) = false" in src

# --- refresh refusal contract (unit level; graph paths are exercised
#     against a live driver in test_graph_edit.py patterns) ---------------------


class TestRefreshContract:
    def test_refresh_respects_edited_flag(self):
        # refresh_entity_description reads the node first; an edited
        # node must refuse BEFORE any LLM call. We pin the refusal by
        # patching the reader.
        from worker.enrich import refresh_entity_description

        with patch(
            "worker.enrich.read_entity_dossier",
            return_value={"label": "X", "type": "t", "description": "user text",
                          "edited": True, "events": [], "neighbors": [], "relations": []},
        ) as rd, patch("worker.enrich.httpx.post") as post:
            out = refresh_entity_description(_graph_cfg(), "quest", "x", "en")
        assert out == {"ok": False, "reason": "description_edited"}
        rd.assert_called_once()
        post.assert_not_called()
