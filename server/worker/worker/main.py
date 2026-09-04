"""Worker entrypoint: preload model, connect Temporal, serve task queue."""

import asyncio
import logging
import os
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.worker import Worker

from .activities import (
    apply_graph_edit,
    chunk,
    diarize,
    enrich,
    export_transcript,
    finalize_recording,
    fix_apply,
    fix_preview,
    graph_gc,
    merge_speakers,
    preload_local,
    rename_entity,
    renew_tag_digest,
    summarize,
    tag_digest,
    transcribe,
)
from .config import load_config
from .db import init_engine
from .workflows import (
    ApplyGraphEdit,
    ExportRecording,
    GraphFixApply,
    GraphFixPreview,
    GraphGc,
    GraphMaintenance,
    ProcessRecording,
    RenameEntity,
    TagDigest,
)

ACTIVITIES = [
    chunk,
    transcribe,
    diarize,
    merge_speakers,
    summarize,
    enrich,
    finalize_recording,
    export_transcript,
    tag_digest,
    graph_gc,
    rename_entity,
    apply_graph_edit,
    renew_tag_digest,
    fix_preview,
    fix_apply,
]
# (an unregistered activity fails workflows at runtime with NotFoundError
# while the stage row sits pending — observed 2026-08-27 on enrich).

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("transcripter.worker")

TASK_QUEUE = "transcripter-pipeline"
WORKFLOW_ID_PREFIX = "process-recording-"


async def amain() -> None:
    cfg = load_config()
    init_engine(cfg.database.url)

    # Preload whisper model so the first activity doesn't pay cold-start.
    # preload_local assigns the shared module-level instance in activities —
    # a throwaway LocalTranscriber here would be GC'd and the first activity
    # would load the weights a second time.
    if cfg.transcribe.backend == "local":
        log.info("preloading whisper model %r", cfg.transcribe.model)
        preload_local(cfg.transcribe.model)

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

    # Phase 1: periodic knowledge-graph GC. A Temporal Schedule starts the
    # tiny GraphGc workflow every gc_interval_sec; CANCEL_OTHER keeps a slow
    # pass from stacking with the next tick. Created best-effort at startup
    # (schedule persists server-side; "already running" = a previous worker
    # created it — log and continue). Interval 0 disables registration.
    if cfg.graph.enabled and cfg.graph.gc_interval_sec > 0:
        try:
            await client.create_schedule(
                id="graph-gc",
                schedule=Schedule(
                    action=ScheduleActionStartWorkflow(
                        GraphGc.run,
                        {},
                        id="graph-gc-workflow",
                        task_queue=TASK_QUEUE,
                    ),
                    spec=ScheduleSpec(
                        intervals=[ScheduleIntervalSpec(
                            every=timedelta(seconds=cfg.graph.gc_interval_sec)
                        )],
                    ),
                    policy=SchedulePolicy(
                        overlap=ScheduleOverlapPolicy.CANCEL_OTHER,
                    ),
                ),
            )
            log.info("graph GC scheduled every %ds", cfg.graph.gc_interval_sec)
        except ScheduleAlreadyRunningError:
            log.info("graph GC schedule already exists; leaving it as is")

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            ProcessRecording,
            ExportRecording,
            TagDigest,
            GraphGc,
            RenameEntity,
            ApplyGraphEdit,
            GraphMaintenance,
            GraphFixPreview,
            GraphFixApply,
        ],
        activities=ACTIVITIES,
    )
    log.info("worker started on queue %s", TASK_QUEUE)
    await worker.run()

def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
