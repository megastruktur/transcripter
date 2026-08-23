"""Summarize header behavior: keyless endpoints get no Authorization header."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from worker.summarize import summarize_transcript


def _cfg(api_key_env: str = "", monkeypatch=None) -> SimpleNamespace:
    if monkeypatch and api_key_env:
        monkeypatch.setenv(api_key_env, "sk-sum-1")
    return SimpleNamespace(
        summarize=SimpleNamespace(api_key_env=api_key_env, model="m", base_url="http://x/v1")
    )


@pytest.fixture
def meta(tmp_path: Path) -> Path:
    (tmp_path / "transcript.md").write_text("hello")
    return tmp_path


def _capture(monkeypatch):
    sent = {}

    def fake_post(url, **kw):
        sent["url"] = url
        sent["headers"] = kw.get("headers")

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "summary"}}]}

        return R()

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def test_empty_key_omits_header(meta, monkeypatch):
    sent = _capture(monkeypatch)
    out = summarize_transcript(meta, _cfg())
    assert out == "summary"
    assert "authorization" not in (sent["headers"] or {})


def test_key_env_sends_bearer(meta, monkeypatch):
    sent = _capture(monkeypatch)
    summarize_transcript(meta, _cfg("SUM_KEY", monkeypatch))
    assert sent["headers"]["authorization"] == "Bearer sk-sum-1"
