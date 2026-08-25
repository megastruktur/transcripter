"""ProcessRecording workflow: chunk → transcribe → diarize → merge → summarize."""

import logging
import math
from datetime import timedelta
from typing import TypedDict

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from .activities import timeout_for

log = logging.getLogger("transcripter.workflow")


class PipelineResult(TypedDict, total=False):
    chunk: dict
    transcribe: dict
    diarize: dict
    merge_speakers: dict
    summarize: dict
    export: dict


def _retry() -> RetryPolicy:
    return RetryPolicy(maximum_attempts=2)


def _no_retry() -> RetryPolicy:
    """Slow ML stages: a retry re-runs minutes of server-side compute and
    doubles load on the shared voice stack (two contending jobs run at ~half
    speed — measured 2026-08-25). Timeouts there must fail the stage once;
    the user re-runs regeneration deliberately from the UI."""
    return RetryPolicy(maximum_attempts=1)


def _diarize_retry() -> RetryPolicy:
    # No compose depends_on anymore (profile-gated service): the first
    # recording after `--profile diarization up` may hit LinTO still loading
    # its weights (~2 min). Bridge that with slower, longer retries.
    return RetryPolicy(
        maximum_attempts=4,
        initial_interval=timedelta(seconds=30),
        maximum_interval=timedelta(seconds=60),
    )

def _ml_budget(duration: float | None, chunk_result: dict | None) -> int:
    """StartToClose for transcribe/diarize under chunking: N × the per-chunk
    budget + slack, so each chunk's HTTP timeout (its own share − 30 s) still
    fires before Temporal would cancel the activity.

    Without manifest data (regenerate starting downstream of `chunk`) N is
    estimated from the default 10-min target; overestimation is harmless —
    heartbeat_timeout (120 s) is the real hang guard."""
    n = (chunk_result or {}).get("chunks") or 0
    if n > 0:
        per = timeout_for((chunk_result or {}).get("target_min", 10.0) * 60, 300, 40)
        return n * per + 300
    if duration:
        n_est = max(1, math.ceil(duration / 600))
        return n_est * timeout_for(600, 300, 40) + 300
    return timeout_for(duration, 300, 40)


@workflow.defn
class ProcessRecording:
    """Runs the full pipeline from `start_stage` (default: chunk)."""

    @workflow.run
    async def run(self, args: dict) -> PipelineResult:
        rec_id: str = args["recording_id"]
        start: str = args.get("start_stage", "chunk")
        duration: float | None = args.get("duration_sec")
        result: PipelineResult = {}

        order = ["chunk", "transcribe", "diarize", "merge_speakers", "summarize"]
        assert start in order, f"unknown stage {start}"
        idx = order.index(start)

        try:
            await self._run_stages(rec_id, idx, duration, result)
        finally:
            # Always set terminal recording state, even when a stage failed:
            # stage rows already carry `failed` from the activity itself.
            await workflow.execute_activity(
                "finalize_recording",
                rec_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_retry(),
            )
            # Best-effort note export. WAIT_CANCELLATION_COMPLETED keeps the
            # activity running to completion even if the workflow is
            # cancelled mid-finally (this SDK version has no workflow.shield;
            # this is its documented replacement). The activity returns
            # errors as values, so only infra failures raise — swallowed
            # here; worker.backfill is the designated re-export path.
            try:
                result["export"] = await workflow.execute_activity(
                    "export_transcript",
                    rec_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
            except (ActivityError, CancelledError):
                workflow.logger.warning("export_transcript failed for %s", rec_id)

        return result

    async def _run_stages(
        self,
        rec_id: str,
        idx: int,
        duration: float | None,
        result: PipelineResult,
    ) -> None:
        if idx <= 0:
            result["chunk"] = await workflow.execute_activity(
                "chunk",
                rec_id,
                # ffmpeg decode+re-encode runs at many times realtime; this
                # only guards a wedged process/mount. duration=None prices
                # the 2.5-h maximum, same convention as timeout_for.
                start_to_close_timeout=timedelta(seconds=int((duration or 9000) * 2 + 300)),
                retry_policy=_retry(),  # cheap stage: retry ×2 is fine
            )
        if idx <= 1:
            result["transcribe"] = await workflow.execute_activity(
                "transcribe",
                rec_id,
                # Budgets sized for the CPU voice stack (see activities.py):
                # 90-min recordings are the norm, 2.5 h the observed max.
                # Under chunking: per-chunk budgets summed (see _ml_budget).
                start_to_close_timeout=timedelta(seconds=_ml_budget(duration, result.get("chunk"))),
                # A retry re-runs minutes of compute on the shared voice
                # stack: never automatic (user regenerates from the UI).
                retry_policy=_no_retry(),
                heartbeat_timeout=timedelta(seconds=120),
            )
        # `merge_speakers` still runs and marks itself skipped when there is
        # no usable diarization, so the stage never sits pending forever.
        if idx <= 2:
            # Diarization is best-effort: it is flaky on short, quiet, or
            # single-speaker audio. A failure must not throw away a good
            # transcript, so degrade to transcript-only instead of aborting.
            # The stage row keeps `failed` + last_error for the UI.
            try:
                result["diarize"] = await workflow.execute_activity(
                    "diarize",
                    rec_id,
                    start_to_close_timeout=timedelta(seconds=_ml_budget(duration, result.get("chunk"))),
                    retry_policy=_diarize_retry(),
                    heartbeat_timeout=timedelta(seconds=120),
                )
            except ActivityError:
                workflow.logger.warning("diarize failed for %s; transcript-only", rec_id)
        if idx <= 3:
            result["merge_speakers"] = await workflow.execute_activity(
                "merge_speakers",
                rec_id,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=_retry(),
            )
        if idx <= 4:
            result["summarize"] = await workflow.execute_activity(
                "summarize",
                rec_id,
                start_to_close_timeout=timedelta(seconds=180),
                retry_policy=_retry(),
            )

@workflow.defn
class ExportRecording:
    """Re-export the Obsidian note for one recording (e.g. after a rename).

    Started by the API via temporal_client.start_export — workflow type,
    input keys and task queue are duplicated there by convention. Runs ONLY
    the export activity; there is no pipeline state to finalize."""

    @workflow.run
    async def run(self, args: dict) -> dict:
        rec_id: str = args["recording_id"]
        return await workflow.execute_activity(
            "export_transcript",
            rec_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
            # Same semantics as the ProcessRecording finally-block export:
            # let the activity run to completion even if the workflow is
            # cancelled mid-flight.
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
