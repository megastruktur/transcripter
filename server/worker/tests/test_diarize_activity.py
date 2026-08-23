"""Diarize activity: disabled-config skip path (no HTTP, stale artifacts removed)."""

from pathlib import Path

import pytest

from worker import activities
from worker.config import WorkerConfig
from worker.db import StageStatus


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

    async def fake_call(audio, cfg):
        return FakeResult()

    import worker.diarize as diarize_mod

    monkeypatch.setattr(diarize_mod, "diarize_audio", fake_call)
    monkeypatch.setattr(activities, "set_stage", lambda *a, **kw: None)

    result = await activities.diarize("rec1")

    assert result == {"speakers": ["spk_0"]}
    assert (meta / "diarization.json").exists()
