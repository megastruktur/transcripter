"""ApiTranscriber: request shape and response parsing (mocked httpx)."""

from pathlib import Path

import httpx
import pytest

from worker.transcribe import ApiTranscriber


def _serve(responses: dict) -> httpx.MockTransport:
    """MockTransport asserting request shape; keyed response by call count."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/audio/transcriptions")
        assert request.method == "POST"
        # empty-key runs must omit the header entirely (httpx rejects "Bearer ")
        if not handler.expected_key:  # type: ignore[attr-defined]
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == f"Bearer {handler.expected_key}"  # type: ignore[attr-defined]
        body = request.read().decode()
        # multipart form: model, response_format, both granularities present
        assert 'name="model"' in body
        assert 'name="response_format"' in body
        assert 'name="timestamp_granularities[]"' in body
        assert body.count('name="timestamp_granularities[]"') == 2
        assert "word" in body and "segment" in body
        calls["n"] += 1
        item = responses[calls["n"]]
        if isinstance(item, int):
            return httpx.Response(item, text="boom")
        return httpx.Response(200, json=item)

    handler.expected_key = ""  # type: ignore[attr-defined]
    return httpx.MockTransport(handler)


@pytest.fixture
def audio(tmp_path) -> Path:
    p = tmp_path / "audio.flac"
    p.write_bytes(b"fLaC" + b"\x00" * 64)
    return p


def _run(monkeypatch, responses, audio, key=""):
    import httpx as _httpx

    serve = _serve(responses)
    serve.handler.expected_key = key  # type: ignore[attr-defined]
    monkeypatch.setattr(
        _httpx,
        "post",
        lambda *a, **kw: _httpx.Client(transport=serve).post(*a, **kw),
    )
    t = ApiTranscriber("http://speaches:8000/v1", "Systran/faster-whisper-small", key)
    return t.transcribe(audio)


def test_words_top_level(monkeypatch, audio):
    """OpenAI/Speaches shape: words array at the top level."""
    payload = {
        "language": "en",
        "segments": [{"start": 0.0, "end": 2.0, "text": "hello there"}],
        "words": [
            {"start": 0.0, "end": 0.5, "word": "hello"},
            {"start": 0.6, "end": 1.0, "word": " there"},
        ],
    }
    result = _run(monkeypatch, {1: payload}, audio)
    assert result.language == "en"
    assert [w.text for w in result.words] == ["hello", " there"]
    assert len(result.segments) == 1


def test_words_nested_in_segments(monkeypatch, audio):
    """Groq shape: words nested under each segment."""
    payload = {
        "language": "en",
        "segments": [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "hello there",
                "words": [
                    {"start": 0.0, "end": 0.5, "word": "hello"},
                    {"start": 0.6, "end": 1.0, "word": " there"},
                ],
            }
        ],
    }
    result = _run(monkeypatch, {1: payload}, audio)
    assert [w.text for w in result.words] == ["hello", " there"]


def test_no_words_yields_empty_list(monkeypatch, audio):
    """No words anywhere: parse succeeds, words empty (merge will skip)."""
    payload = {"language": "en", "segments": [{"start": 0.0, "end": 2.0, "text": "hi"}]}
    result = _run(monkeypatch, {1: payload}, audio)
    assert result.words == []
    assert len(result.segments) == 1


def test_http_error_raises(monkeypatch, audio):
    """Server errors surface as httpx exceptions (stage marks failed)."""
    with pytest.raises(httpx.HTTPStatusError):
        _run(monkeypatch, {1: 500}, audio)


def test_nonempty_key_sends_bearer(monkeypatch, audio):
    """With a key set, the Authorization header must be present and exact."""
    payload = {"language": "en", "segments": [], "words": []}
    _run(monkeypatch, {1: payload}, audio, key="sk-test-123")
