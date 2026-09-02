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
    enrich: dict
    export: dict


def _retry() -> RetryPolicy:
    return RetryPolicy(maximum_attempts=2)


def _no_retry() -> RetryPolicy:
    """Slow ML stages: a retry re-runs minutes of server-side compute and
    doubles load on the shared voice stack (two contending jobs run at ~half
    speed — measured 2026-08-25). Timeouts there must fail the stage once;
    the user re-runs regeneration deliberately from the UI."""
    return RetryPolicy(maximum_attempts=1)


def _enrich_retry() -> RetryPolicy:
    """Phase 3-F F2: enrich gets 3 attempts with a 5-min backoff.

    The live incident (2026-08-29): extraction sat in the shared LiteLLM
    FIFO queue behind ~10 req/min of parallel consumers and timed out at
    2400 s. A retry just gives the queue time to drain — no re-run risk
    of the transcribe kind (enrich is idempotent: DETACH DELETE by
    origin_recording_id). Intentional skips are excluded server-side:
    the activity raises ApplicationError(non_retryable=True) for them,
    which Temporal never retries regardless of this policy. Workflow
    ceiling is unlimited (no execution_timeout set), so 3×(2400+300)
    fits."""
    return RetryPolicy(
        maximum_attempts=3,
        initial_interval=timedelta(seconds=300),
        maximum_interval=timedelta(seconds=300),
    )


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

        order = ["chunk", "transcribe", "diarize", "merge_speakers", "summarize", "enrich"]
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
                    {"recording_id": rec_id},
                    start_to_close_timeout=timedelta(seconds=150),
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
                    start_to_close_timeout=timedelta(
                        seconds=_ml_budget(duration, result.get("chunk"))
                    ),
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
                start_to_close_timeout=timedelta(seconds=2400),
                retry_policy=_no_retry(),
                heartbeat_timeout=timedelta(seconds=120),
            )
        # Wave B: enrich is best-effort like diarize/merge — a failure
        # must not abort the recording. The activity catches its own
        # errors and marks `failed`; we only escalate infra errors.
        # Phase 3-F F2: _enrich_retry (3 attempts, 5-min backoff) lets a
        # starved FIFO queue drain; intentional skips are
        # non-retryable ApplicationErrors and never re-run.
        if idx <= 5:
            try:
                result["enrich"] = await workflow.execute_activity(
                    "enrich",
                    rec_id,
                    # Same envelope as summarize: the LiteLLM proxy
                    # timeout is the binding constraint, and the HTTP
                    # budget inside the activity is 30 s under.
                    start_to_close_timeout=timedelta(seconds=2400),
                    retry_policy=_enrich_retry(),
                    heartbeat_timeout=timedelta(seconds=120),
                )
            except ActivityError:
                workflow.logger.warning("enrich failed for %s; recording still done", rec_id)


@workflow.defn
class ExportRecording:
    """Re-export the Obsidian note for one recording (e.g. after a rename).

    Started by the API via temporal_client.start_export — workflow type,
    input keys and task queue are duplicated there by convention. Runs ONLY
    the export activity; there is no pipeline state to finalize."""

    @workflow.run
    async def run(self, args: dict) -> dict:
        return await workflow.execute_activity(
            "export_transcript",
            {"recording_id": args["recording_id"], "rename_only": args.get("rename_only", False)},
            start_to_close_timeout=timedelta(seconds=150),
            retry_policy=RetryPolicy(maximum_attempts=3),
            # Same semantics as the ProcessRecording finally-block export:
            # let the activity run to completion even if the workflow is
            # cancelled mid-flight.
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )


@workflow.defn
class TagDigest:
    """Wave-C: build a per-tag digest note from the last N done recordings.

    One activity (Postgres + Neo4j + LLM + atomic file write). The same
    budget envelope as summarize / enrich (LiteLLM proxy 2400 s ceiling,
    HTTP client 30 s under via the activity's own timeout): an LLM call
    on a contended llama-server can eat the whole budget, and a slow
    failure must NOT trigger Temporal's automatic retry — the digest is
    read-only w.r.t. the recording pipeline, so a retry doubles load on
    the shared LLM without giving the user a better answer. The user
    regenerates from the UI.

    No finalize / export: the digest writes its own file atomically and
    there is no per-recording state to keep in sync.
    """

    @workflow.run
    async def run(self, args: dict) -> dict:
        return await workflow.execute_activity(
            "tag_digest",
            args,
            start_to_close_timeout=timedelta(seconds=2400),
            # Same shape as summarize / enrich: LLM-bound, no automatic retry
            # on long failures. The user regenerates from the UI.
            retry_policy=_no_retry(),
            heartbeat_timeout=timedelta(seconds=120),
        )


@workflow.defn
class GraphGc:
    """Phase 1: periodic knowledge-graph GC (Temporal Schedule action).

    Schedule actions can only start WORKFLOWS, so this thin wrapper
    exists to carry the single ``graph_gc`` activity. The schedule is
    registered in ``main.amain`` when ``graph.enabled`` and
    ``graph.gc_interval_sec > 0``; CANCEL_OTHER overlap means a slow
    pass never stacks with the next one — the newer run cancels the
    older. The sweep itself is read-Postgres + delete-Neo4j and returns
    a count; there is no per-recording state to finalize.
    """

    @workflow.run
    async def run(self, args: dict | None = None) -> dict:
        return await workflow.execute_activity(
            "graph_gc",
            args or {},
            start_to_close_timeout=timedelta(minutes=10),
            # A failed sweep is simply retried on the next tick — but a
            # transient Neo4j hiccup should not burn the tick silently.
            retry_policy=RetryPolicy(maximum_attempts=2),
            heartbeat_timeout=timedelta(seconds=120),
        )


@workflow.defn
class RenameEntity:
    """Phase 4: rename ONE entity (label ± type + user_corrected flag)
    in one namespace. Thin single-activity wrapper — same shape as
    GraphGc: the API starts it via temporal_client.start_rename_entity
    after its own existence check; the workflow exists so the write is
    visible/cancellable in the Temporal UI and carries a retry policy.

    Short budget: a Neo4j SET plus at most ONE embed call — nothing
    here legitimately takes two minutes. A missing node raises
    non-retryable inside the activity, so the ×2 retry only covers
    transient Neo4j/HTTP hiccups.
    """

    @workflow.run
    async def run(self, args: dict) -> dict:
        return await workflow.execute_activity(
            "rename_entity",
            args,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )


@workflow.defn
class GraphMaintenance:
    """Phase A: debounced digest renewal for ONE tag after graph edits.

    Every applied edit signals ``edit_applied``; the workflow waits
    ``graph.edit_debounce_sec`` of silence (timer reset per signal — a
    burst of edits renews the digest ONCE), then runs the tag_digest
    activity only when the digest file is older than the newest edit
    row (the mtime skip: an enrich auto-digest that already rewrote the
    note suppresses a redundant LLM call).

    Runs forever (one workflow per tag, workflow id
    ``graph-maintenance-<tag>``); the activity carries the same 2400 s
    LLM budget envelope as TagDigest. Timer value is passed in args
    (read from config by the starter) so the workflow itself stays
    deterministic.
    """

    @workflow.signal
    async def edit_applied(self) -> None:
        self._signalled = True

    @workflow.run
    async def run(self, args: dict) -> dict:
        tag: str = args["tag"]
        debounce_s: float = float(args.get("edit_debounce_sec", 180))
        self._signalled = True  # first start counts as one pending renewal
        while True:
            # Drain-then-wait: debounce — any signal during the window
            # restarts it. ``wait_condition`` with a timeout returns on
            # EITHER the condition flipping (signal → restart the
            # window) or the timeout elapsing (silence → renew). The
            # pending-signal flag is cleared BEFORE waiting so a signal
            # that lands during the wait is seen, not swallowed.
            if not self._signalled:
                await workflow.wait_condition(lambda: self._signalled, timeout=None)
            while self._signalled:
                self._signalled = False
                try:
                    await workflow.wait_condition(lambda: self._signalled, timeout=debounce_s)
                except TimeoutError:
                    pass  # silence reached — fall through to renewal
            # Renewal; the mtime skip inside the activity decides
            # whether an LLM call is actually needed.
            await workflow.execute_activity(
                "renew_tag_digest",
                {"tag": tag},
                start_to_close_timeout=timedelta(seconds=2400),
                retry_policy=_no_retry(),
                heartbeat_timeout=timedelta(seconds=120),
            )


@workflow.defn
class ApplyGraphEdit:
    """Phase A: apply ONE stored graph edit. Thin single-activity
    wrapper — same shape as RenameEntity: the API starts it after its
    own validation; the workflow makes the write visible/cancellable in
    the Temporal UI and carries a short retry for transient
    Neo4j/Postgres hiccups (target-missing raises non-retryable inside
    the activity)."""

    @workflow.run
    async def run(self, args: dict) -> dict:
        return await workflow.execute_activity(
            "apply_graph_edit",
            args,
            start_to_close_timeout=timedelta(seconds=300),
            retry_policy=RetryPolicy(maximum_attempts=2),
            heartbeat_timeout=timedelta(seconds=60),
        )
