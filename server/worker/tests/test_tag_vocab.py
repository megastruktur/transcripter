"""Tag-vocabulary plumbing: _hotword_prompt/_glossary_block helpers +
the transcribe/summarize stages passing them through."""


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.chunk import ChunkEntry, Manifest, save_manifest
from worker.config import TranscribeConfig, WorkerConfig
from worker.db import Base, Recording, RecordingState, TagDef
from worker.transcribe import Segment, TranscriptionResult, Word


@pytest.fixture
def Session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    return Session


# ---------- helpers ----------


def test_hotword_prompt_none_for_no_tags(Session):
    with Session() as s:
        assert activities._hotword_prompt(s, []) is None


def test_hotword_prompt_none_for_empty_registry(Session):
    with Session() as s:
        assert activities._hotword_prompt(s, ["ghost"]) is None


def test_hotword_prompt_unions_tags_casefold_dedup(Session):
    with Session() as s:
        s.add(TagDef(name="a", vocabulary=["Абсалом", "Bytchez"]))
        s.add(TagDef(name="b", vocabulary=["абсалом", "Мендель"]))
        s.commit()
        out = activities._hotword_prompt(s, ["a", "b"])
    assert out is not None
    assert out.startswith("Термины и имена")
    assert "Абсалом" in out and "Bytchez" in out and "Мендель" in out
    # casefold dedup: abstract+Абсалом collapse to first spelling
    assert out.count("бсалом") == 1


def test_hotword_prompt_cap_truncates_whole_words(Session):
    with Session() as s:
        s.add(TagDef(name="a", vocabulary=[f"слово{i:03d}" for i in range(200)]))
        s.commit()
        out = activities._hotword_prompt(s, ["a"])
    assert out is not None
    assert len(out) <= activities._HOTWORD_PROMPT_CAP
    assert not out.endswith(" ")  # whole-word cut
    # most words survive the cap
    assert out.count("слово") > 80


def test_glossary_block_semicolons(Session):
    with Session() as s:
        s.add(TagDef(name="a", vocabulary=["Абсалом", "Bytchez"]))
        s.commit()
        out = activities._glossary_block(s, ["a"])
    assert out == "Абсалом; Bytchez"


def test_glossary_block_none(Session):
    with Session() as s:
        assert activities._glossary_block(s, []) is None
        assert activities._glossary_block(s, ["ghost"]) is None


# ---------- transcribe stage plumbing ----------


@pytest.fixture
def rec_env(tmp_path, monkeypatch, Session) -> dict:
    """rec1 with chunk manifest + FakeApi recording prompts."""
    recordings = tmp_path / "recordings" / "rec1"
    meta = recordings / "meta"
    meta.mkdir(parents=True)
    cfg = WorkerConfig(
        storage=type(WorkerConfig().storage)(path=tmp_path),
        transcribe=TranscribeConfig(backend="api", base_url="http://speaches/v1", model="m"),
    )
    monkeypatch.setattr(activities, "_cfg", cfg)
    with Session() as s:
        s.add(Recording(id="rec1", state=RecordingState.processing, title="t", duration_sec=3600.0, tags=["a", "b"]))
        s.commit()
    monkeypatch.setattr(activities, "set_stage", lambda *a, **kw: None)
    # no vault rehydrate
    monkeypatch.setattr(activities, "_rehydrate_if_vault", lambda rid: None)

    m = Manifest(duration_sec=1198.0, target_min=10.0, overlap_sec=2.0)
    m.chunks = [
        ChunkEntry(index=0, file="chunk_000.flac", start=0.0, end=600.0),
        ChunkEntry(index=1, file="chunk_001.flac", start=598.0, end=1198.0),
    ]
    d = meta / "chunks"
    d.mkdir(exist_ok=True)
    for c in m.chunks:
        (d / c.file).write_bytes(b"fLaC")
    save_manifest(m, meta)

    def result(name):
        return TranscriptionResult(
            "ru", [Segment(0.0, 1.0, "привет")], [Word(0.0, 1.0, "привет")]
        )

    fake = _FakeApi({"chunk_000.flac": result("a"), "chunk_001.flac": result("b")})
    monkeypatch.setattr(activities, "_api", fake)
    return {"meta": meta, "fake": fake, "Session": Session}


class _FakeApi:
    def __init__(self, results):
        self.results = results
        self.calls: list[dict] = []

    def transcribe(self, audio_path, timeout_sec=600.0, prompt=None, condition_on_previous_text=None):
        self.calls.append({"file": audio_path.name, "prompt": prompt, "reset": condition_on_previous_text})
        return self.results[audio_path.name]


@pytest.mark.asyncio
async def test_transcribe_passes_hotwords_per_chunk(rec_env):
    with rec_env["Session"]() as s:
        s.add(TagDef(name="a", vocabulary=["Абсалом"]))
        s.add(TagDef(name="b", vocabulary=["Мендель"]))
        s.commit()
    await activities.transcribe("rec1")
    prompts = [c["prompt"] for c in rec_env["fake"].calls]
    assert len(prompts) == 2
    assert all(p is not None and "Абсалом" in p and "Мендель" in p for p in prompts)


@pytest.mark.asyncio
async def test_transcribe_no_vocab_prompt_none(rec_env):
    await activities.transcribe("rec1")
    assert all(c["prompt"] is None for c in rec_env["fake"].calls)


@pytest.mark.asyncio
async def test_transcribe_suspect_keeps_hotwords(rec_env):
    """A suspect chunk resets decoder context but keeps the vocabulary."""
    with rec_env["Session"]() as s:
        s.add(TagDef(name="a", vocabulary=["Абсалом"]))
        s.commit()
    # mark chunk 0 suspect via the manifest API (chunks/chunks.json)
    from worker.chunk import load_manifest, save_manifest

    meta = rec_env["meta"]
    m = load_manifest(meta)
    assert m is not None
    m.chunks[0].transcribe_suspect = True
    save_manifest(m, meta)
    await activities.transcribe("rec1")
    calls = rec_env["fake"].calls
    suspect = next(c for c in calls if c["file"] == "chunk_000.flac")
    assert suspect["reset"] is False  # reset_context=False means "reset"
    assert suspect["prompt"] is not None and "Абсалом" in suspect["prompt"]
