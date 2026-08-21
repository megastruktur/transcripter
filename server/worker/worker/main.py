"""Worker entrypoint: preload model, connect Temporal, serve task queue."""

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import (
    diarize,
    finalize_recording,
    merge_speakers,
    summarize,
    transcribe,
)
from .config import load_config
from .db import init_engine
from .workflows import ProcessRecording

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

    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"))

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ProcessRecording],
        activities=[transcribe, diarize, merge_speakers, summarize, finalize_recording],
    )
    log.info("worker started on queue %s", TASK_QUEUE)
    await worker.run()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
