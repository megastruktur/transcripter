"""ProcessRecording workflow: transcribe → diarize → merge → summarize."""

import logging
from datetime import timedelta
from typing import TypedDict

from temporalio import workflow
from temporalio.common import RetryPolicy

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

        if idx <= 0:
            result["transcribe"] = await workflow.execute_activity(
                "transcribe",
                args=rec_id,
                start_to_close_timeout=timedelta(seconds=timeout_for(duration, 60, 12)),
                retry_policy=_retry(),
                heartbeat_timeout=timedelta(seconds=120),
            )
        if idx <= 1:
            result["diarize"] = await workflow.execute_activity(
                "diarize",
                args=rec_id,
                start_to_close_timeout=timedelta(seconds=timeout_for(duration, 60, 30)),
                retry_policy=_retry(),
            )
        if idx <= 2:
            result["merge_speakers"] = await workflow.execute_activity(
                "merge_speakers",
                args=rec_id,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=_retry(),
            )
        if idx <= 3:
            result["summarize"] = await workflow.execute_activity(
                "summarize",
                args=rec_id,
                start_to_close_timeout=timedelta(seconds=180),
                retry_policy=_retry(),
            )
        return result
