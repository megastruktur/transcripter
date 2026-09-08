"""Soniox async STT: one API call = transcription + speaker diarization.

Soniox (api.soniox.com, model stt-async-v5) runs STT and diarization in a
single async job: every token carries start_ms/end_ms and, with
enable_speaker_diarization, a `speaker` label. The soniox backend sends the
WHOLE recording — chunking exists to reset CPU-whisper decoder context
(repetition-loop incident 2026-08-25) and buys nothing here, and Soniox
recommends whole-file async for the best diarization accuracy.

REST flow (no SDK dependency):
    POST   /v1/files                    multipart upload -> file_id
    POST   /v1/transcriptions           create job (model, hints, context)
    GET    /v1/transcriptions/{id}      poll status until completed|error
    GET    /v1/transcriptions/{id}/transcript   tokens
    DELETE /v1/transcriptions/{id}      cleanup (quota: 2000 transcriptions)
    DELETE /v1/files/{file_id}          cleanup (quota: 10 GB / 1000 files)

The diarize stage must not re-run a paid call: transcribe() persists the
normalized tokens (soniox_tokens.json, per channel for stereo) and diarize()
synthesizes diarization.json from them via to_diarization() — merge.py sees
the same start/end/speaker shape DiariZen produces.
"""

import logging
import time
from collections import Counter
from pathlib import Path

import httpx

from .diarize import DiarizationResult, DiarSegment
from .transcribe import Segment, TranscriptionResult, Word

log = logging.getLogger("transcripter.soniox")

SONIOX_BASE_URL = "https://api.soniox.com"
DEFAULT_MODEL = "stt-async-v5"
# Hard Soniox limit on one uploaded file (fixed, cannot be raised).
MAX_DURATION_SEC = 300 * 60
TOKENS_NAME = "soniox_tokens.json"

_POLL_INTERVAL_SEC = 5.0
_POLL_REQ_TIMEOUT_SEC = 30.0
_UPLOAD_REQ_TIMEOUT_SEC = 600.0  # long FLACs on slow uplinks
_TRANSCRIPT_REQ_TIMEOUT_SEC = 120.0


class SonioxError(RuntimeError):
    """Soniox API failure (upload/create/poll/transcript or quota)."""


def transcribe_file(
    audio: Path,
    api_key: str,
    model: str = DEFAULT_MODEL,
    language_hints: list[str] | None = None,
    context_terms: list[str] | None = None,
    timeout_sec: float = 3600.0,
    client_reference_id: str = "",
    client: httpx.Client | None = None,
) -> dict:
    """One audio file -> one Soniox job -> normalized token stream.

    Returns {"language": str, "tokens": [{"text", "start", "end",
    "speaker"}, ...]} with seconds (Soniox speaks milliseconds; the merge
    layer speaks seconds). `speaker` is None on tokens the diarizer left
    unattributed. `timeout_sec` is the caller's total budget (the poll
    deadline); individual requests get fixed shorter timeouts. Upload and
    transcription objects are deleted best-effort even on failure.
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=_POLL_REQ_TIMEOUT_SEC)
    headers = {"authorization": f"Bearer {api_key}"}
    file_id: str | None = None
    transcription_id: str | None = None
    try:
        with open(audio, "rb") as f:
            r = client.post(
                f"{SONIOX_BASE_URL}/v1/files",
                files={"file": (audio.name, f)},
                headers=headers,
                timeout=_UPLOAD_REQ_TIMEOUT_SEC,
            )
        _check(r, "upload")
        file_id = r.json()["id"]

        payload: dict = {
            "model": model,
            "enable_speaker_diarization": True,
            "enable_language_identification": True,
            "file_id": file_id,
        }
        if language_hints:
            payload["language_hints"] = language_hints
        if context_terms:
            payload["context"] = {"terms": context_terms}
        if client_reference_id:
            payload["client_reference_id"] = client_reference_id
        r = client.post(
            f"{SONIOX_BASE_URL}/v1/transcriptions",
            json=payload,
            headers=headers,
        )
        _check(r, "create transcription")
        transcription_id = r.json()["id"]

        deadline = time.monotonic() + timeout_sec
        while True:
            r = client.get(
                f"{SONIOX_BASE_URL}/v1/transcriptions/{transcription_id}",
                headers=headers,
            )
            _check(r, "poll transcription")
            status = r.json().get("status")
            if status == "completed":
                break
            if status == "error":
                raise SonioxError(
                    f"soniox job failed: {r.json().get('error_message', 'unknown')}"
                )
            if time.monotonic() >= deadline:
                raise SonioxError(
                    f"soniox job not completed within {timeout_sec:.0f}s "
                    f"(status {status!r})"
                )
            time.sleep(_POLL_INTERVAL_SEC)

        r = client.get(
            f"{SONIOX_BASE_URL}/v1/transcriptions/{transcription_id}/transcript",
            headers=headers,
            timeout=_TRANSCRIPT_REQ_TIMEOUT_SEC,
        )
        _check(r, "fetch transcript")
        return normalize_tokens(r.json())
    finally:
        if transcription_id is not None:
            _delete_quietly(client, f"/v1/transcriptions/{transcription_id}", headers)
        if file_id is not None:
            _delete_quietly(client, f"/v1/files/{file_id}", headers)
        if own_client:
            client.close()


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code >= 400:
        raise SonioxError(f"soniox {what}: HTTP {r.status_code}: {r.text[:300]}")


def _delete_quietly(client: httpx.Client, path: str, headers: dict) -> None:
    try:
        client.delete(f"{SONIOX_BASE_URL}{path}", headers=headers)
    except httpx.HTTPError:
        log.warning("soniox cleanup failed for %s (quota-bound objects)", path)


def normalize_tokens(data: dict) -> dict:
    """Soniox transcript response -> {"language", "tokens"} with seconds.

    `language` is the most common per-token language label (identification
    is enabled for every job); "unknown" when no token carries one.
    """
    tokens = [
        {
            "text": t.get("text", ""),
            "start": t.get("start_ms", 0) / 1000.0,
            "end": t.get("end_ms", 0) / 1000.0,
            "speaker": t.get("speaker"),
            "language": t.get("language"),
        }
        for t in data.get("tokens", [])
    ]
    langs = Counter(t["language"] for t in tokens if t["language"])
    return {"language": langs.most_common(1)[0][0] if langs else "unknown", "tokens": tokens}


def to_transcription_result(tokens: dict, channel: str | None = None) -> TranscriptionResult:
    """Token stream -> pipeline TranscriptionResult.

    words: every token (merge.py attributes speakers by word overlap);
    segments: consecutive same-speaker runs (display grouping for
    transcript.md; the attributed diarized transcript comes from
    merge_speakers as usual).
    """
    words = [
        Word(t["start"], t["end"], t["text"], channel) for t in tokens["tokens"]
    ]
    segments: list[Segment] = []
    run: list[dict] = []
    key = lambda t: t["speaker"]
    for t in tokens["tokens"]:
        if run and key(run[-1]) != key(t):
            segments.append(_segment(run, channel))
            run = []
        run.append(t)
    if run:
        segments.append(_segment(run, channel))
    return TranscriptionResult(tokens.get("language", "unknown"), segments, words)


def _segment(run: list[dict], channel: str | None) -> Segment:
    text = "".join(t["text"] for t in run).strip()
    return Segment(run[0]["start"], run[-1]["end"], text, channel)


def to_diarization(tokens: dict) -> DiarizationResult:
    """Token stream -> DiarizationResult (consecutive same-speaker runs).

    Tokens without a speaker label are skipped (gaps, not boundaries). No
    labeled token at all -> empty result; merge_speakers then takes its
    existing 'no diarization' skip path instead of failing.
    """
    segments: list[DiarSegment] = []
    run: list[dict] = []
    for t in tokens["tokens"]:
        if t["speaker"] is None:
            continue
        if run and run[0]["speaker"] != t["speaker"]:
            segments.append(
                DiarSegment(start=run[0]["start"], end=run[-1]["end"], speaker=str(run[0]["speaker"]))
            )
            run = []
        run.append(t)
    if run:
        segments.append(
            DiarSegment(start=run[0]["start"], end=run[-1]["end"], speaker=str(run[0]["speaker"]))
        )
    speakers = sorted({s.speaker for s in segments})
    return DiarizationResult(speakers=speakers, segments=segments)
