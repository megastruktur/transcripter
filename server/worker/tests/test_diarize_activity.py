"""Diarize activity: disabled-config skip path (no HTTP, stale artifacts removed)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.config import WorkerConfig
from worker.db import Base, Recording, RecordingState, StageStatus


@pytest.fixture
def meta(tmp_path, monkeypatch) -> Path:
    """Recording meta dir with stale diarization artifacts from a prior run."""
    recordings = tmp_path / "recordings" / "rec1"
    meta = recordings / "meta"
    meta.mkdir(parents=True)
    (recordings / "audio.flac").write_bytes(b"fLaC")
    (meta / "diarization.json").write_text('{"speakers": [], "segments": []}')
    (meta / "diarized-transcript.md").write_text("# Diarized transcript\nstale")
    monkeypatch.setattr(activities, "_cfg", WorkerConfig(storage=type(WorkerConfig().storage)(path=tmp_path)))

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    with Session() as s:
        s.add(Recording(id="rec1", state=RecordingState.processing, title="t", duration_sec=3600.0))
        s.commit()
    return meta


@pytest.mark.asyncio
async def test_disabled_skips_without_http(meta, monkeypatch):
    cfg = activities.cfg()
    cfg.diarization.enabled = False

    calls = []

    def fake_set_stage(*a, **kw):
        calls.append((a, kw))

    async def fail_call(*a, **kw):  # any HTTP attempt fails the test
        raise AssertionError("diarize_audio must not be called when disabled")

    import worker.diarize as diarize_mod

    monkeypatch.setattr(diarize_mod, "diarize_audio", fail_call)
    monkeypatch.setattr(activities, "set_stage", fake_set_stage)

    result = await activities.diarize("rec1")

    assert result == {"skipped": "diarization disabled"}
    assert not (meta / "diarization.json").exists()
    assert not (meta / "diarized-transcript.md").exists()
    # Pin the transition: exactly one call, straight to skipped (no
    # running/inc_attempts transit, no stale error/details left for the UI).
    assert len(calls) == 1
    assert calls[0][0][2] is StageStatus.skipped
    assert not calls[0][1].get("inc_attempts")
    assert calls[0][1].get("details") == {}  # stale speaker details cleared


@pytest.mark.asyncio
async def test_enabled_calls_diarize(meta, monkeypatch):
    class FakeResult:
        @property
        def speakers(self) -> list[str]:
            return ["spk_0"]

        def model_dump_json(self) -> str:
            return '{"speakers": ["spk_0"], "segments": []}'

    seen = {}

    async def fake_call(audio, cfg, timeout_sec=None):
        seen["timeout_sec"] = timeout_sec
        return FakeResult()

    import worker.diarize as diarize_mod

    monkeypatch.setattr(diarize_mod, "diarize_audio", fake_call)
    monkeypatch.setattr(activities, "set_stage", lambda *a, **kw: None)

    result = await activities.diarize("rec1")

    assert result == {"speakers": ["spk_0"]}
    assert (meta / "diarization.json").exists()
    # Budget scales with duration: 1 h audio => 300 + 40*60 - 30 = 2670 s
    # (30 s under the Temporal budget; the old hardcoded 3600 under-budgeted
    # long recordings).
    assert seen["timeout_sec"] == 2670.0
