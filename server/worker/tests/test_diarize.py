"""Diarize client dialects: DiariZen (start/end/speaker) vs LinTO
(seg_begin/seg_end/spk_id) — the response shape tells them apart."""

import asyncio

import httpx

from worker.config import DiarizationConfig
from worker.diarize import diarize_audio


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload: dict):
        self.payload = payload
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.payload)


def _run(payload: dict, tmp_path=None) -> tuple:
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"RIFF")
    transport = _FakeTransport(payload)
    client = httpx.AsyncClient(transport=transport)

    class _Cfg:  # diarize_audio only touches cfg.diarization.endpoint
        diarization = DiarizationConfig(endpoint="http://svc")

    cfg = _Cfg()
    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: client
    try:
        result = asyncio.run(diarize_audio(audio, cfg))
    finally:
        httpx.AsyncClient = orig
    return result, transport.requests


def test_diarizen_dialect(tmp_path):
    result, reqs = _run(
        {
            "speakers": ["spk_0", "spk_1"],
            "segments": [
                {"start": 0.1, "end": 2.5, "speaker": "spk_0"},
                {"start": 2.0, "end": 4.5, "speaker": "spk_1"},
            ],
        },
        tmp_path,
    )
    assert result.speakers == ["spk_0", "spk_1"]
    assert [(s.start, s.end, s.speaker) for s in result.segments] == [
        (0.1, 2.5, "spk_0"),
        (2.0, 4.5, "spk_1"),
    ]
    assert reqs[0].url.path == "/diarization"


def test_linto_dialect_rollback(tmp_path):
    result, _ = _run(
        {
            "speakers": ["spk1"],
            "segments": [{"seg_begin": 1.0, "seg_end": 3.0, "spk_id": "spk1"}],
        },
        tmp_path,
    )
    assert result.speakers == ["spk1"]
    assert [(s.start, s.end, s.speaker) for s in result.segments] == [
        (1.0, 3.0, "spk1")
    ]


def test_empty_segments(tmp_path):
    result, _ = _run({"speakers": [], "segments": []}, tmp_path)
    assert result.speakers == []
    assert result.segments == []
