"""ProcessRecording workflow: transcribe → diarize → merge → summarize."""

import logging
from datetime import timedelta
from typing import TypedDict

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from .activities import timeout_for

log = logging.getLogger("transcripter.workflow")


class PipelineResult(TypedDict, total=False):
    transcribe: dict
    diarize: dict
    merge_speakers: dict
    summarize: dict


def _retry() -> RetryPolicy:
    return RetryPolicy(maximum_attempts=2)


@workflow.defn
class ProcessRecording:
    """Runs the full pipeline from `start_stage` (default: transcribe)."""

    @workflow.run
    async def run(self, args: dict) -> PipelineResult:
        rec_id: str = args["recording_id"]
        start: str = args.get("start_stage", "transcribe")
        duration: float | None = args.get("duration_sec")
        result: PipelineResult = {}

        order = ["transcribe", "diarize", "merge_speakers", "summarize"]
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
        return result

    async def _run_stages(
        self,
        rec_id: str,
        idx: int,
        duration: float | None,
        result: PipelineResult,
    ) -> None:
        if idx <= 0:
            result["transcribe"] = await workflow.execute_activity(
                "transcribe",
                rec_id,
                start_to_close_timeout=timedelta(seconds=timeout_for(duration, 60, 12)),
                retry_policy=_retry(),
                heartbeat_timeout=timedelta(seconds=120),
            )
        # `merge_speakers` still runs and marks itself skipped when there is
        # no usable diarization, so the stage never sits pending forever.
        if idx <= 1:
            # Diarization is best-effort: it is flaky on short, quiet, or
            # single-speaker audio. A failure must not throw away a good
            # transcript, so degrade to transcript-only instead of aborting.
            # The stage row keeps `failed` + last_error for the UI.
            try:
                result["diarize"] = await workflow.execute_activity(
                    "diarize",
                    rec_id,
                    start_to_close_timeout=timedelta(seconds=timeout_for(duration, 60, 30)),
                    retry_policy=_retry(),
                )
            except ActivityError:
                workflow.logger.warning("diarize failed for %s; transcript-only", rec_id)
        if idx <= 2:
            result["merge_speakers"] = await workflow.execute_activity(
                "merge_speakers",
                rec_id,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=_retry(),
            )
        if idx <= 3:
            result["summarize"] = await workflow.execute_activity(
                "summarize",
                rec_id,
                start_to_close_timeout=timedelta(seconds=180),
                retry_policy=_retry(),
            )
