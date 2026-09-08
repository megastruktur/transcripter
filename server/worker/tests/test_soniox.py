"""Soniox backend: REST client flow, token synthesis, activity wiring.

MockTransport exercises the upload→create→poll→transcript→cleanup flow;
activity tests pin the soniox branches of chunk/transcribe/diarize (skip,
one-job stereo, diarization.json synthesis from saved tokens).
"""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.config import WorkerConfig
from worker.db import Base, Recording, RecordingState, StageStatus
from worker.soniox import (
    TOKENS_NAME,
    normalize_tokens,
    to_diarization,
    to_transcription_result,
    transcribe_file,
)


def _tokens_reply(*, speaker: bool = True, lang: str | None = "ru") -> dict:
    """Transcript response: two speakers, ms timestamps, leading spaces."""
    toks = [
        {"text": "При", "start_ms": 0, "end_ms": 200},
        {"text": "вет", "start_ms": 200, "end_ms": 400},
        {"text": " Hello", "start_ms": 500, "end_ms": 700},
        {"text": " there", "start_ms": 700, "end_ms": 900},
    ]
    if speaker:
        toks[0]["speaker"] = toks[1]["speaker"] = "1"
        toks[2]["speaker"] = toks[3]["speaker"] = "2"
    if lang:
        for t in toks:
            t["language"] = lang
    return {"tokens": toks}


class _SonioxMock:
    """Routes the whole REST flow; records create payloads + cleanup calls."""

    def __init__(self, *, poll_states: list[str] | None = None):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.poll_states = poll_states or ["pending", "completed"]

    def client(self) -> httpx.Client:
        mock = self
        state = {"polls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer test-key"
            path, method = request.url.path, request.method
            if path == "/v1/files" and method == "POST":
                return httpx.Response(200, json={"id": "file-1"})
            if path == "/v1/transcriptions" and method == "POST":
                mock.created.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "trx-1"})
            if path == "/v1/transcriptions/trx-1" and method == "GET":
                s = mock.poll_states[min(state["polls"], len(mock.poll_states) - 1)]
                state["polls"] += 1
                return httpx.Response(200, json={"status": s})
            if path == "/v1/transcriptions/trx-1/transcript" and method == "GET":
                return httpx.Response(200, json=_tokens_reply())
            if method == "DELETE" and path in ("/v1/transcriptions/trx-1", "/v1/files/file-1"):
                mock.deleted.append(path)
                return httpx.Response(204)
            raise AssertionError(f"unexpected request {method} {path}")

        return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def audio(tmp_path) -> Path:
    p = tmp_path / "audio.flac"
    p.write_bytes(b"fLaC" + b"\x00" * 64)
    return p


def test_flow_request_shape_and_cleanup(audio):
    mock = _SonioxMock()
    out = transcribe_file(
        audio, "test-key", language_hints=["ru"], context_terms=["Абсалом"], client=mock.client()
    )
    (create,) = mock.created
    assert create["model"] == "stt-async-v5"
    assert create["enable_speaker_diarization"] is True
    assert create["file_id"] == "file-1"
    assert create["language_hints"] == ["ru"]
    assert create["context"] == {"terms": ["Абсалом"]}
    assert sorted(mock.deleted) == ["/v1/files/file-1", "/v1/transcriptions/trx-1"]
    # ms -> seconds, leading spaces preserved for merge.py text joining
    assert out["tokens"][0] == {
        "text": "При",
        "start": 0.0,
        "end": 0.2,
        "speaker": "1",
        "language": "ru",
    }
    assert out["language"] == "ru"


def test_flow_error_status_raises_with_cleanup(audio):
    mock = _SonioxMock(poll_states=["error"])
    with pytest.raises(Exception, match="soniox job failed"):
        transcribe_file(audio, "test-key", client=mock.client())
    assert sorted(mock.deleted) == ["/v1/files/file-1", "/v1/transcriptions/trx-1"]


def test_to_transcription_result_groups_by_speaker():
    out = to_transcription_result(normalize_tokens(_tokens_reply()), channel="mic")
    assert out.language == "ru"
    assert [s.text for s in out.segments] == ["Привет", "Hello there"]
    assert [w.text for w in out.words] == ["При", "вет", " Hello", " there"]
    assert all(w.channel == "mic" for w in out.words)
    seg = out.segments[0]
    assert (seg.start, seg.end, seg.channel) == (0.0, 0.4, "mic")


def test_to_diarization_runs_and_gaps():
    out = to_diarization(normalize_tokens(_tokens_reply()))
    assert out.speakers == ["1", "2"]
    assert [(s.speaker, s.start, s.end) for s in out.segments] == [
        ("1", 0.0, 0.4),
        ("2", 0.5, 0.9),
    ]


def test_to_diarization_unlabeled_tokens_empty():
    """No speaker anywhere: empty result -> merge takes its skip path."""
    out = to_diarization(normalize_tokens(_tokens_reply(speaker=False)))
    assert out.speakers == [] and out.segments == []


@pytest.fixture
def soniox_env(tmp_path, monkeypatch):
    """Storage + sqlite session + soniox config on the activities module.

    The dev host has no ffprobe; split_channels (channel layout probe)
    is mocked to the mono layout — it is an implementation detail here.
    """
    recordings = tmp_path / "recordings" / "rec1"
    meta = recordings / "meta"
    meta.mkdir(parents=True)
    (recordings / "audio.flac").write_bytes(b"fLaC")
    cfg = WorkerConfig()
    cfg.transcribe.backend = "soniox"
    cfg.storage = type(cfg.storage)(path=tmp_path)
    monkeypatch.setattr(activities, "_cfg", cfg)

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    with Session() as s:
        s.add(Recording(id="rec1", state=RecordingState.processing, title="t", duration_sec=600.0))
        s.commit()
    monkeypatch.setattr(activities, "split_channels", lambda audio, meta: [])
    monkeypatch.setattr(activities, "set_stage", lambda *a, **kw: None)
    return meta


@pytest.mark.asyncio
async def test_chunk_skips_on_soniox(soniox_env, monkeypatch):
    calls = []
    monkeypatch.setattr(activities, "set_stage", lambda *a, **kw: calls.append(a))
    out = await activities.chunk("rec1")
    assert out == {"skipped": "soniox backend", "chunks": 0}
    assert calls[0][2] is StageStatus.skipped


@pytest.mark.asyncio
async def test_transcribe_soniox_mono_writes_tokens(soniox_env, monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "k")

    async def fake_job(c, audio, timeout_sec, terms=None, channel=None):
        from worker.soniox import to_transcription_result

        tokens = normalize_tokens(_tokens_reply())
        return to_transcription_result(tokens, channel=channel), tokens

    monkeypatch.setattr(activities, "_transcribe_soniox", fake_job)
    out = await activities.transcribe("rec1")
    assert out["backend"] == "soniox" and out["segments"] == 2
    saved = json.loads((soniox_env / TOKENS_NAME).read_text())
    assert saved["language"] == "ru"
    assert (soniox_env / "segments.json").exists()
    assert (soniox_env / "transcript.md").exists()


@pytest.mark.asyncio
async def test_transcribe_soniox_over_300min_fails_fast(soniox_env, monkeypatch):
    import worker.db as db_mod

    assert db_mod._SessionLocal is not None
    with db_mod._SessionLocal() as s:
        rec = s.get(Recording, "rec1")
        rec.duration_sec = 301 * 60.0
        s.commit()

    async def fail_job(*a, **kw):
        raise AssertionError("must not call soniox past the 300-min cap")

    monkeypatch.setattr(activities, "_transcribe_soniox", fail_job)
    with pytest.raises(RuntimeError, match="300 min"):
        await activities.transcribe("rec1")


@pytest.mark.asyncio
async def test_diarize_soniox_synthesizes_from_tokens(soniox_env):
    (soniox_env / TOKENS_NAME).write_text(
        json.dumps(normalize_tokens(_tokens_reply()), ensure_ascii=False)
    )
    out = await activities.diarize("rec1")
    assert out == {"speakers": ["1", "2"]}
    data = json.loads((soniox_env / "diarization.json").read_text())
    assert [(s["speaker"], s["start"], s["end"]) for s in data["segments"]] == [
        ("1", 0.0, 0.4),
        ("2", 0.5, 0.9),
    ]


@pytest.mark.asyncio
async def test_diarize_soniox_missing_tokens_fails_with_hint(soniox_env):
    with pytest.raises(RuntimeError, match="regenerate from stage 'transcribe'"):
        await activities.diarize("rec1")
