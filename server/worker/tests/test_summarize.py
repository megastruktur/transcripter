"""Summarize: keyless header + legacy path + profile-driven prompt override."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from worker.summarize import (
    _TRANSCRIPT_LIMIT,
    PROFILE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    summarize_transcript,
)


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


def _capture(monkeypatch, response="summary"):
    sent = {}

    def fake_post(url, **kw):
        sent["url"] = url
        sent["headers"] = kw.get("headers")
        sent["json"] = kw.get("json")

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": response}}]}

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


# --- profile-mode tests ----------------------------------------------------


def test_profile_prompt_substitutes_title_and_transcript(meta, monkeypatch):
    sent = _capture(monkeypatch)
    template = "Title: {title}\nBody:\n{transcript}"
    summarize_transcript(
        meta, _cfg(), prompt_template=template, title="My Call"
    )
    messages = sent["json"]["messages"]
    # System message fixed to "Follow the user's instructions." per contract.
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == PROFILE_SYSTEM_PROMPT
    # Single user message with substitution applied.
    assert messages[1]["role"] == "user"
    assert "Title: My Call" in messages[1]["content"]
    assert "hello" in messages[1]["content"]
    # No second user message — profile mode collapses into one.
    assert len(messages) == 2


def test_profile_mode_does_not_use_legacy_system_prompt(meta, monkeypatch):
    sent = _capture(monkeypatch)
    summarize_transcript(
        meta, _cfg(), prompt_template="P: {transcript}", title="t"
    )
    system_msg = sent["json"]["messages"][0]["content"]
    # Legacy SYSTEM_PROMPT must NOT bleed through in profile mode.
    assert system_msg != SYSTEM_PROMPT
    assert system_msg == PROFILE_SYSTEM_PROMPT


def test_legacy_mode_unchanged(meta, monkeypatch):
    """Without a profile prompt, behavior is bit-for-bit the legacy path."""
    sent = _capture(monkeypatch)
    summarize_transcript(meta, _cfg())
    messages = sent["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "hello"


def test_profile_mode_truncates_transcript_at_100k(meta, monkeypatch):
    sent = _capture(monkeypatch)
    big = "x" * (_TRANSCRIPT_LIMIT + 5000)
    (meta / "transcript.md").write_text(big)
    summarize_transcript(
        meta,
        _cfg(),
        prompt_template="wrap {transcript}",
        title="t",
    )
    user_content = sent["json"]["messages"][1]["content"]
    assert len(user_content) <= _TRANSCRIPT_LIMIT


def test_profile_mode_empty_title_renders(meta, monkeypatch):
    sent = _capture(monkeypatch)
    summarize_transcript(
        meta, _cfg(), prompt_template="T={title} B={transcript}", title=""
    )
    assert "T= B=hello" in sent["json"]["messages"][1]["content"]


def test_render_profile_prompt_tolerates_json_braces():
    from worker.summarize import _render_profile_prompt

    out = _render_profile_prompt('Schema {"a": 1}; title={title}; body: {transcript}', "t", "BODY")
    assert '{"a": 1}' in out and "title=t" in out and "body: BODY" in out
