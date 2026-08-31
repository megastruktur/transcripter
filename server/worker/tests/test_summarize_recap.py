"""Phase 3 recap: digest-note injection into the summarize prompt.

Covers build_recap (digest lookup reuses digest._existing_digest_for_tag,
frontmatter stripping, truncation, missing/unreadable → None), the message
injection shape for BOTH prompt modes (profile + legacy), the disabled-knob
no-op, and the digest prompt's new "Entity updates" section.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.db import Base, Recording, RecordingState, Stage, session
from worker.digest import (
    _DIGEST_PROMPT_HEADER,
    DigestGraphSlice,
    DigestRow,
    _render_prompt,
)
from worker.summarize import build_recap

# ---------- shared fixtures ----------------------------------------------------


def _cfg(
    recap: bool = True,
    graph_enabled: bool = True,
    tmp_path: Path | None = None,
) -> Any:
    cfg = MagicMock()
    cfg.summarize.enabled = True
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    cfg.summarize.base_url = "http://x/v1"
    cfg.summarize.recap = recap
    cfg.graph.enabled = graph_enabled
    cfg.vault.path = tmp_path if tmp_path is not None else Path("/tmp")
    return cfg


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite catalog + module-level cfg (mirrors test_enrich_activity)."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)


@pytest.fixture()
def digests(tmp_path: Path) -> Path:
    d = tmp_path / "digests"
    d.mkdir()
    return d


def _write_digest(dir_: Path, name: str, tag: str, body: str) -> Path:
    fm = f"---\ntag: {tag}\nrecordings: []\ncount: 0\n---"
    p = dir_ / name
    p.write_text(f"{fm}\n\n{body}\n", encoding="utf-8")
    return p


# ---------- build_recap ---------------------------------------------------------


class TestBuildRecap:
    def test_found_and_frontmatter_stripped(self, digests: Path) -> None:
        _write_digest(digests, "pathfinder.md", "pathfinder", "Overview body here.")
        root = digests.parent
        out = build_recap("pathfinder", root)
        assert out is not None
        assert "Overview body here." in out
        assert "tag:" not in out
        assert not out.startswith("---")

    def test_matches_by_frontmatter_not_filename(self, digests: Path) -> None:
        """Slug collision: the tag's note may live under a -N name."""
        _write_digest(digests, "pathf-2.md", "pathfinder", "Body under odd name.")
        out = build_recap("pathfinder", digests.parent)
        assert out is not None
        assert "Body under odd name." in out

    def test_missing_note_returns_none(self, tmp_path: Path) -> None:
        assert build_recap("no-such-tag", tmp_path) is None

    def test_missing_digests_dir_returns_none(self, tmp_path: Path) -> None:
        assert build_recap("tag", tmp_path / "nowhere") is None

    def test_unreadable_note_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "digests"
        d.mkdir()
        _write_digest(d, "t.md", "tag", "body")
        calls = {"n": 0}
        real_read_text = Path.read_text

        def flaky_read_text(self, *a, **kw):
            # First read (the _existing_digest_for_tag scan) succeeds so we
            # reach build_recap's own read; the second one dies.
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("disk gone")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", flaky_read_text)
        assert build_recap("tag", tmp_path) is None

    def test_truncation_appends_marker(self, digests: Path) -> None:
        _write_digest(digests, "t.md", "tag", "x" * 5000)
        out = build_recap("tag", digests.parent, max_chars=1000)
        assert out is not None
        assert out.endswith("\n…(truncated)")
        assert len(out) <= 1000 + len("\n…(truncated)") + 1

    def test_no_truncation_under_limit(self, digests: Path) -> None:
        _write_digest(digests, "t.md", "tag", "short body")
        out = build_recap("tag", digests.parent, max_chars=4000)
        assert out is not None
        assert "(truncated)" not in out
        assert out.startswith("short body")

    def test_unicode_tag_matches(self, digests: Path) -> None:
        """Phase 0 tags are Unicode — the frontmatter match must follow."""
        _write_digest(digests, "поход.md", "Поход в горы", "Тело дайджеста.")
        out = build_recap("Поход в горы", digests.parent)
        assert out is not None
        assert "Тело дайджеста." in out


# ---------- message injection ---------------------------------------------------


def _capture_post(monkeypatch: pytest.MonkeyPatch) -> dict:
    sent: dict = {}

    def fake_post(url, **kw):
        sent["url"] = url
        sent["headers"] = kw.get("headers")
        sent["json"] = kw.get("json")

        class R:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "summary"}}]}

        return R()

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


@pytest.fixture()
def meta(tmp_path: Path) -> Path:
    (tmp_path / "transcript.md").write_text("hello")
    return tmp_path


RECAP = "1. Overview: the party met a dragon."

# Constant prefix shared by the injection assertions below. Kept in sync
# with summarize_transcript's injection wording.
PREFIX = (
    "Prior context from this series' knowledge base "
    "(digest and retrieved excerpts of earlier sessions):\n\n"
)

# Exact instruction strings — assert against the real constants, never
# hand-typed prefixes (a truncated literal silently passes/fails wrong).
from worker.summarize import PROFILE_SYSTEM_PROMPT, SYSTEM_PROMPT


class TestRecapInjection:
    def test_profile_mode_appends_into_system(self, meta: Path, monkeypatch) -> None:
        sent = _capture_post(monkeypatch)
        from worker.summarize import summarize_transcript

        summarize_transcript(
            meta,
            _cfg(tmp_path=meta.parent),
            prompt_template="P: {transcript}",
            recap_block=RECAP,
        )
        messages = sent["json"]["messages"]
        # Recap MUST ride inside the single leading system message — a
        # second system entry mid-conversation is rejected by the
        # llama-server chat template ("System message must be at the
        # beginning", live HTTP 500, 2026-08-29).
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == PROFILE_SYSTEM_PROMPT + "\n\n" + PREFIX + RECAP
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "P: hello"

    def test_legacy_mode_appends_into_system(self, meta: Path, monkeypatch) -> None:
        sent = _capture_post(monkeypatch)
        from worker.summarize import summarize_transcript

        summarize_transcript(meta, _cfg(tmp_path=meta.parent), recap_block=RECAP)
        messages = sent["json"]["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == SYSTEM_PROMPT + "\n\n" + PREFIX + RECAP
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "hello"

    def test_empty_recap_block_not_injected(self, meta: Path, monkeypatch) -> None:
        """Empty string / None must leave the system message untouched."""
        from worker.summarize import summarize_transcript

        for empty in (None, ""):
            sent = _capture_post(monkeypatch)
            summarize_transcript(meta, _cfg(tmp_path=meta.parent), recap_block=empty)
            assert len(sent["json"]["messages"]) == 2
            assert sent["json"]["messages"][0]["content"] == SYSTEM_PROMPT

    def test_recap_not_truncated_by_transcript_cap(
        self, meta: Path, monkeypatch
    ) -> None:
        """The 100k cap applies to the transcript only; recap rides as-is."""
        from worker.summarize import _TRANSCRIPT_LIMIT, summarize_transcript

        big_recap = "R" * (_TRANSCRIPT_LIMIT + 50)
        sent = _capture_post(monkeypatch)
        summarize_transcript(
            meta,
            _cfg(tmp_path=meta.parent),
            prompt_template="P: {transcript}",
            recap_block=big_recap,
        )
        messages = sent["json"]["messages"]
        assert len(messages) == 2
        assert messages[0]["content"].endswith(PREFIX + big_recap)
        assert len(messages[0]["content"]) == (
            len(PROFILE_SYSTEM_PROMPT) + 2 + len(PREFIX) + len(big_recap)
        )
        # Transcript still capped.
        assert messages[1]["content"] == "P: " + "hello"

    def test_no_recap_leaves_single_system(self, meta: Path, monkeypatch) -> None:
        """Baseline: exactly [system, user] with the fixed instruction."""
        from worker.summarize import summarize_transcript

        sent = _capture_post(monkeypatch)
        summarize_transcript(
            meta,
            _cfg(tmp_path=meta.parent),
            prompt_template="P: {transcript}",
        )
        messages = sent["json"]["messages"]
        assert [m["role"] for m in messages] == ["system", "user"]
        assert messages[0]["content"] == PROFILE_SYSTEM_PROMPT


# ---------- activity wiring -----------------------------------------------------


def _make_recording(
    rid: str, tags: list[str], tmp_path: Path
) -> None:
    with session() as s:
        s.add(
            Recording(
                id=rid,
                title="Session 1",
                tags=tags,
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
    meta = tmp_path / "storage" / "recordings" / rid / "meta"
    meta.mkdir(parents=True)
    (meta / "transcript.md").write_text("the meeting transcript", encoding="utf-8")


def _full_cfg(tmp_path: Path, recap: bool, graph: bool) -> Any:
    cfg = MagicMock()
    cfg.summarize.enabled = True
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    cfg.summarize.base_url = "http://x/v1"
    cfg.summarize.recap = recap
    cfg.graph.enabled = graph
    cfg.graph.uri = "bolt://n:7687"
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.graph.auto_digest = False
    cfg.profiles.path = tmp_path / "profiles"
    cfg.storage.path = tmp_path / "storage"
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    cfg.vault.path = tmp_path / "transcripts"
    return cfg


class TestSummarizeActivityRecap:
    def test_recap_details_written_when_digest_present(self, tmp_path, monkeypatch) -> None:
        rid = "11111111-1111-1111-1111-111111111111"
        _make_recording(rid, ["pathfinder"], tmp_path)
        # Digest note the recap path will find.
        dig = tmp_path / "transcripts" / "digests"
        dig.mkdir(parents=True)
        _write_digest(dig, "pathfinder.md", "pathfinder", "Digest body text.")
        cfg = _full_cfg(tmp_path, recap=True, graph=True)
        monkeypatch.setattr(activities, "_cfg", cfg)

        sent = _capture_post(monkeypatch)
        import asyncio

        asyncio.run(activities.summarize(rid))

        messages = sent["json"]["messages"]
        recap_msgs = [m for m in messages if "knowledge base" in m["content"]]
        assert len(recap_msgs) == 1
        assert "Digest body text." in recap_msgs[0]["content"]
        # details REPLACE the default {} (set_stage semantics) and carry recap.
        with session() as s:
            st = s.query(Stage).filter_by(recording_id=rid, kind="summarize").one()
        assert st.status.value == "done"
        assert st.details == {
            "recap": {"used": True, "sessions": 0, "chars": len("Digest body text.\n")}
        }

    def test_recap_disabled_no_extra_message_no_details(
        self, tmp_path, monkeypatch
    ) -> None:
        rid = "22222222-2222-2222-2222-222222222222"
        _make_recording(rid, ["pathfinder"], tmp_path)
        dig = tmp_path / "transcripts" / "digests"
        dig.mkdir(parents=True)
        _write_digest(dig, "pathfinder.md", "pathfinder", "Should not appear.")
        cfg = _full_cfg(tmp_path, recap=False, graph=True)
        monkeypatch.setattr(activities, "_cfg", cfg)

        sent = _capture_post(monkeypatch)
        import asyncio

        asyncio.run(activities.summarize(rid))
        with session() as s:
            st = s.query(Stage).filter_by(recording_id=rid, kind="summarize").one()
        assert all(
            "knowledge base" not in m["content"] for m in sent["json"]["messages"]
        )

        assert st.status.value == "done"
        # Contract: details carry used=false when recap is disabled/absent.
        assert st.details == {"recap": {"used": False, "sessions": 0, "chars": 0}}

    def test_no_tag_no_recap_details_empty(self, tmp_path, monkeypatch) -> None:
        rid = "33333333-3333-3333-3333-333333333333"
        _make_recording(rid, [], tmp_path)
        dig = tmp_path / "transcripts" / "digests"
        dig.mkdir(parents=True)
        _write_digest(dig, "pathfinder.md", "pathfinder", "Should not appear.")
        cfg = _full_cfg(tmp_path, recap=True, graph=True)
        monkeypatch.setattr(activities, "_cfg", cfg)

        sent = _capture_post(monkeypatch)
        import asyncio

        asyncio.run(activities.summarize(rid))

        with session() as s:
            st = s.query(Stage).filter_by(recording_id=rid, kind="summarize").one()
        assert all("knowledge base" not in m["content"] for m in sent["json"]["messages"])
        assert st.status.value == "done"
        assert st.details == {"recap": {"used": False, "sessions": 0, "chars": 0}}

    def test_graph_disabled_no_recap(self, tmp_path, monkeypatch) -> None:
        rid = "44444444-4444-4444-4444-444444444444"
        _make_recording(rid, ["pathfinder"], tmp_path)
        dig = tmp_path / "transcripts" / "digests"
        dig.mkdir(parents=True)
        _write_digest(dig, "pathfinder.md", "pathfinder", "Should not appear.")
        cfg = _full_cfg(tmp_path, recap=True, graph=False)
        monkeypatch.setattr(activities, "_cfg", cfg)

        sent = _capture_post(monkeypatch)
        import asyncio


        asyncio.run(activities.summarize(rid))

        with session() as s:
            st = s.query(Stage).filter_by(recording_id=rid, kind="summarize").one()

        assert all("knowledge base" not in m["content"] for m in sent["json"]["messages"])
        assert st.status.value == "done"
        assert st.details == {"recap": {"used": False, "sessions": 0, "chars": 0}}

    def test_build_recap_failure_degrades_to_no_recap(
        self, tmp_path, monkeypatch
    ) -> None:
        """build_recap raising must not fail the summarize stage."""
        rid = "55555555-5555-5555-5555-555555555555"
        _make_recording(rid, ["pathfinder"], tmp_path)
        cfg = _full_cfg(tmp_path, recap=True, graph=True)
        monkeypatch.setattr(activities, "_cfg", cfg)

        def boom(tag, root, max_chars=4000):
            raise RuntimeError("recap exploded")

        monkeypatch.setattr(
            "worker.summarize.build_recap", boom
        )
        sent = _capture_post(monkeypatch)
        import asyncio

        asyncio.run(activities.summarize(rid))

        assert all("knowledge base" not in m["content"] for m in sent["json"]["messages"])
        with session() as s:
            st = s.query(Stage).filter_by(recording_id=rid, kind="summarize").one()
        assert st.status.value == "done"
        assert st.details == {"recap": {"used": False, "sessions": 0, "chars": 0}}


# ---------- digest prompt evolution ---------------------------------------------


class TestDigestPromptEntityUpdates:
    def test_entity_updates_section_present(self) -> None:
        assert "4. Entity updates" in _DIGEST_PROMPT_HEADER
        assert "state_change" in _DIGEST_PROMPT_HEADER

    def test_open_threads_renumbered_to_5(self) -> None:
        assert "5. Open threads" in _DIGEST_PROMPT_HEADER
        assert "4. Open threads" not in _DIGEST_PROMPT_HEADER

    def test_format_placeholders_intact(self) -> None:
        prompt = _render_prompt(
            "pathfinder",
            2,
            [
                DigestRow(
                    "r1", "T1", datetime_utc(2026, 8, 1), datetime_utc(2026, 8, 1)
                )
            ],
            DigestGraphSlice(entities=[], events=[], relations=[]),
        )
        assert "Tag: pathfinder" in prompt
        assert "Sessions: T1 (2026-08-01)" in prompt
        assert "Relations (from — rel — to):" in prompt
        assert "(none)" in prompt

# ---------- recap retrieval (semantic tail) --------------------------------------

_DIM = 4


class TestRecapRetrieval:
    """The recap-retrieval tail: KNN over the tag's Phase 3.5 index,
    other recordings only, rendered after the digest body."""

    def _seed_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Path]:
        """Build a real vec0 index for tag 'pathfinder' with one OTHER
        recording; return (root, meta_cur, index_path)."""
        from worker.semantic_index import index_path, index_segments

        root = tmp_path / "transcripts"
        meta_other = tmp_path / "other"
        meta_other.mkdir()
        (meta_other / "transcript.md").write_text(
            "# Transcript\n\n"
            "**[00:00:01 – 00:00:05]** dragon attacked the party at the bridge.\n\n"
            "**[00:01:00 – 00:01:10]** the party retreated to the tavern.\n",
            encoding="utf-8",
        )
        embed = SimpleNamespace(
            backend="local",
            model_path=Path("/models/bge"),
            base_url="",
            model="",
            api_key_env="",
            configured_dimensions=_DIM,
        )
        index_cfg = SimpleNamespace(graph=SimpleNamespace(embed=embed))

        monkeypatch.setattr(
            "worker.embeddings.embed_texts",
            lambda texts, c: [[0.1] * _DIM for _ in texts],
        )
        index_segments(
            "rec-old", "pathfinder", "Old session", meta_other, root, index_cfg
        )
        meta_cur = tmp_path / "cur"
        meta_cur.mkdir()
        (meta_cur / "transcript.md").write_text(
            "# Transcript\n\n**[00:00:01 – 00:00:05]** hello agenda dragons again.\n",
            encoding="utf-8",
        )
        return root, meta_cur, index_path(root, "pathfinder")

    def test_retrieval_appends_after_digest(self, tmp_path, monkeypatch) -> None:
        root, meta_cur, _ = self._seed_index(tmp_path, monkeypatch)
        dig = root / "digests"
        dig.mkdir(parents=True)
        _write_digest(dig, "pathfinder.md", "pathfinder", "Digest body.")
        cfg = SimpleNamespace(
            summarize=SimpleNamespace(recap_k=3, recap_budget_chars=800)
        )
        out = build_recap(
            "pathfinder",
            root,
            recording_id="rec-cur",
            meta_dir=meta_cur,
            cfg=cfg,
        )
        assert out is not None
        assert out.startswith("Digest body.")
        assert "Related earlier discussion" in out
        assert "Old session" in out
        assert "dragon attacked" in out

    def test_no_digest_retrieval_only(self, tmp_path, monkeypatch) -> None:
        root, meta_cur, _ = self._seed_index(tmp_path, monkeypatch)
        cfg = SimpleNamespace(
            summarize=SimpleNamespace(recap_k=3, recap_budget_chars=800)
        )
        out = build_recap(
            "pathfinder",
            root,
            recording_id="rec-cur",
            meta_dir=meta_cur,
            cfg=cfg,
        )
        assert out is not None
        assert out.startswith("Related earlier discussion")
        assert "dragon attacked" in out

    def test_current_recording_excluded(self, tmp_path, monkeypatch) -> None:
        root, meta_cur, _ = self._seed_index(tmp_path, monkeypatch)
        from worker.semantic_index import index_segments

        embed = SimpleNamespace(
            backend="local",
            model_path=Path("/models/bge"),
            base_url="",
            model="",
            api_key_env="",
            configured_dimensions=_DIM,
        )
        index_cfg = SimpleNamespace(graph=SimpleNamespace(embed=embed))
        monkeypatch.setattr(
            "worker.embeddings.embed_texts",
            lambda texts, c: [[0.1] * _DIM for _ in texts],
        )
        index_segments(
            "rec-cur", "pathfinder", "Current session", meta_cur, root, index_cfg
        )
        cfg = SimpleNamespace(
            summarize=SimpleNamespace(recap_k=3, recap_budget_chars=800)
        )
        out = build_recap(
            "pathfinder",
            root,
            recording_id="rec-cur",
            meta_dir=meta_cur,
            cfg=cfg,
        )
        assert out is not None
        assert "Current session" not in out
        assert "Old session" in out

    def test_no_index_digest_only(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "transcripts"
        root.mkdir()
        dig = root / "digests"
        dig.mkdir()
        _write_digest(dig, "pathfinder.md", "pathfinder", "Only digest.")
        meta_cur = tmp_path / "cur"
        meta_cur.mkdir()
        (meta_cur / "transcript.md").write_text(
            "# Transcript\n\n**[00:00:01 – 00:00:05]** agenda.\n", encoding="utf-8"
        )
        cfg = SimpleNamespace(
            summarize=SimpleNamespace(recap_k=3, recap_budget_chars=800)
        )
        out = build_recap(
            "pathfinder",
            root,
            recording_id="rec-cur",
            meta_dir=meta_cur,
            cfg=cfg,
        )
        assert out == "Only digest.\n"

    def test_embedding_failure_degrades_to_digest(self, tmp_path, monkeypatch) -> None:
        root, meta_cur, _ = self._seed_index(tmp_path, monkeypatch)
        dig = root / "digests"
        dig.mkdir(parents=True)
        _write_digest(dig, "pathfinder.md", "pathfinder", "Safe digest.")
        monkeypatch.setattr(
            "worker.embeddings.embed_texts",
            lambda texts, c: (_ for _ in ()).throw(RuntimeError("backend dead")),
        )
        cfg = SimpleNamespace(
            summarize=SimpleNamespace(recap_k=3, recap_budget_chars=800)
        )
        out = build_recap(
            "pathfinder",
            root,
            recording_id="rec-cur",
            meta_dir=meta_cur,
            cfg=cfg,
        )
        assert out == "Safe digest.\n"


def datetime_utc(*args: int) -> Any:
    from datetime import UTC, datetime

    return datetime(*args, tzinfo=UTC)
