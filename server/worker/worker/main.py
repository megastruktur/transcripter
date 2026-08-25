"""Worker entrypoint: preload model, connect Temporal, serve task queue."""

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import (
    chunk,
    diarize,
    export_transcript,
    finalize_recording,
    merge_speakers,
    summarize,
    transcribe,
)
from .config import load_config
from .db import init_engine
from .workflows import ExportRecording, ProcessRecording

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("transcripter.worker")

TASK_QUEUE = "transcripter-pipeline"
WORKFLOW_ID_PREFIX = "process-recording-"


async def amain() -> None:
    cfg = load_config()
    init_engine(cfg.database.url)

    # Preload whisper model so the first activity doesn't pay cold-start.
    if cfg.transcribe.backend == "local":
        from .transcribe import LocalTranscriber

        log.info("preloading whisper model %r", cfg.transcribe.model)
        LocalTranscriber(cfg.transcribe.model)._ensure_loaded()

    # The diarization container is profile-gated; a plain `up -d` no longer
    # starts it. One cheap probe at startup (no retry loop — Temporal's
    # diarize retry policy already absorbs LinTO's ~2min weight load, and a
    # polling loop here would only delay worker readiness) to warn loudly
    # instead of leaving misconfiguration to per-recording failures.
    if cfg.diarization.enabled:
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                r = await client.head(
                    f"{cfg.diarization.endpoint.rstrip('/')}/healthcheck", timeout=5
                )
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning(
                "diarization.enabled=true but %s failed the startup probe (%s). "
                "If the stack just started, LinTO may still be loading and the "
                "Temporal retries will absorb it. Otherwise: start the bundled "
                "container (docker compose --profile diarization up -d), or point "
                "diarization.endpoint/DIARIZATION_ENDPOINT at an external service, "
                "or set diarization.enabled=false",
                cfg.diarization.endpoint,
                e,
            )

    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"))

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ProcessRecording, ExportRecording],
        activities=[chunk, transcribe, diarize, merge_speakers, summarize, finalize_recording, export_transcript],
    )
    log.info("worker started on queue %s", TASK_QUEUE)
    await worker.run()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
