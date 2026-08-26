"""LocalTranscriber: thread-safe singleton, download_root wiring, shared preload."""

import sys
import threading
import time
import types
from pathlib import Path

import pytest

from worker import activities
from worker.config import WorkerConfig
from worker.transcribe import LocalTranscriber, TranscriptionResult


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel (injected via sys.modules).

    Counts constructions and records download_root; a small artificial load
    delay makes thread races actually overlap.
    """

    constructions = 0
    last_download_root: object = None

    def __init__(
        self,
        model_name: str,
        *,
        device: str,
        compute_type: str,
        cpu_threads: int,
        download_root: object = None,
    ) -> None:
        type(self).constructions += 1
        type(self).last_download_root = download_root
        time.sleep(0.02)

    def transcribe(self, audio: str, word_timestamps: bool = False):
        return iter([]), types.SimpleNamespace(language="en")


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch):
    FakeWhisperModel.constructions = 0
    FakeWhisperModel.last_download_root = None
    module = types.SimpleNamespace(WhisperModel=FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return FakeWhisperModel


@pytest.fixture
def clean_local(monkeypatch: pytest.MonkeyPatch):
    """Isolate the activities module-global between tests."""
    monkeypatch.setattr(activities, "_local", None)


def test_download_root_from_env(fake_whisper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_DOWNLOAD_ROOT", "/models")
    LocalTranscriber("small")._ensure_loaded()
    assert fake_whisper.last_download_root == "/models"


def test_download_root_defaults_to_hf_cache(
    fake_whisper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WHISPER_DOWNLOAD_ROOT", raising=False)
    LocalTranscriber("small")._ensure_loaded()
    assert fake_whisper.last_download_root is None


def test_download_root_empty_env_means_default(
    fake_whisper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHISPER_DOWNLOAD_ROOT", "")
    LocalTranscriber("small")._ensure_loaded()
    assert fake_whisper.last_download_root is None


def test_model_constructed_once_across_calls(fake_whisper) -> None:
    lt = LocalTranscriber("small")
    first = lt._ensure_loaded()
    assert lt._ensure_loaded() is first
    assert fake_whisper.constructions == 1


def test_model_constructed_once_under_threads(fake_whisper) -> None:
    lt = LocalTranscriber("small")
    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(lt._ensure_loaded())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake_whisper.constructions == 1
    assert all(r is results[0] for r in results)


def test_preload_local_shares_module_instance(fake_whisper, clean_local) -> None:
    activities.preload_local("small")
    shared = activities._local
    assert shared is not None
    assert fake_whisper.constructions == 1
    # Same model again: reuse, no reload.
    activities.preload_local("small")
    assert activities._local is shared
    assert fake_whisper.constructions == 1
    # Model change: fresh instance.
    activities.preload_local("medium")
    assert activities._local is not shared
    assert fake_whisper.constructions == 2


@pytest.mark.asyncio
async def test_transcribe_file_reuses_preloaded_instance(
    fake_whisper, clean_local, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def passthrough(aw):
        return await aw

    monkeypatch.setattr(activities, "_heartbeat_while", passthrough)
    activities.preload_local("small")
    cfg = WorkerConfig()  # backend=local, model=small
    result = await activities._transcribe_file(cfg, Path("x.flac"), timeout_sec=1.0)
    assert isinstance(result, TranscriptionResult)
    assert result.language == "en"
    assert fake_whisper.constructions == 1  # no second load for the activity
