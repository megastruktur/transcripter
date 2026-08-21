"""Start workflows from the API (T3 wiring of the on_finalize hook).

Workflow/type constants duplicated from server/worker (separate venvs);
keep in sync with worker/workflows.py and worker/main.py.
"""

import os

from temporalio.client import Client

TASK_QUEUE = "transcripter-pipeline"
WORKFLOW_NAME = "ProcessRecording"
WORKFLOW_ID_PREFIX = "process-recording-"

_client: Client | None = None


async def get_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"))
    return _client


async def start_pipeline(rec_id: str, duration_sec: float | None = None) -> str:
    client = await get_client()
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        {"recording_id": rec_id, "duration_sec": duration_sec},
        id=f"{WORKFLOW_ID_PREFIX}{rec_id}",
        task_queue=TASK_QUEUE,
    )
    return handle.id


async def regenerate_stage(rec_id: str, stage: str, duration_sec: float | None = None) -> str:
    """New run of the same workflow id, starting at `stage`."""
    client = await get_client()
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        {"recording_id": rec_id, "start_stage": stage, "duration_sec": duration_sec},
        id=f"{WORKFLOW_ID_PREFIX}{rec_id}",
        task_queue=TASK_QUEUE,
    )
    return handle.id
