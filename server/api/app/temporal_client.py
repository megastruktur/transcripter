"""Start workflows from the API (T3 wiring of the on_finalize hook).

Workflow/type constants duplicated from server/worker (separate venvs);
keep in sync with worker/workflows.py and worker/main.py.
"""

import os
import time
import uuid

from temporalio.client import Client

TASK_QUEUE = "transcripter-pipeline"
WORKFLOW_NAME = "ProcessRecording"
WORKFLOW_ID_PREFIX = "process-recording-"
EXPORT_WORKFLOW_NAME = "ExportRecording"
EXPORT_WORKFLOW_ID_PREFIX = "export-recording-"
DIGEST_WORKFLOW_NAME = "TagDigest"
DIGEST_WORKFLOW_ID_PREFIX = "digest-"
RENAME_ENTITY_WORKFLOW_NAME = "RenameEntity"
RENAME_ENTITY_ID_PREFIX = "rename-entity-"

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

async def start_export(rec_id: str, rename_only: bool = False) -> str:
    """Re-export the recording's vault folder. rename_only=True (the title-only PATCH
    rename path) only renames the folder — files inside are NOT rewritten,
    so Obsidian edits survive. Unique workflow id so renames never collide
    with an in-flight pipeline run."""
    client = await get_client()
    handle = await client.start_workflow(
        EXPORT_WORKFLOW_NAME,
        {"recording_id": rec_id, "rename_only": rename_only},
        id=f"{EXPORT_WORKFLOW_ID_PREFIX}{rec_id}-{int(time.time())}",
        task_queue=TASK_QUEUE,
    )
    return handle.id


async def start_digest(tag: str, last_n: int) -> str:
    """Build a per-tag digest note (wave C).

    Unique workflow id with a uuid8 suffix keeps the name readable in the
    Temporal UI ("digest-<tag>-<uuid>") while letting concurrent digests
    of the same tag coexist. The activity itself is idempotent on the file
    write side (atomic rename), but a separate workflow id makes a
    long-running LLM call cancellable per attempt without losing the
    previous one's output.
    """
    suffix = uuid.uuid4().hex[:8]
    client = await get_client()
    handle = await client.start_workflow(
        DIGEST_WORKFLOW_NAME,
        {"tag": tag, "last_n": last_n},
        id=f"{DIGEST_WORKFLOW_ID_PREFIX}{tag}-{suffix}",
        task_queue=TASK_QUEUE,
    )
    return handle.id


async def start_rename_entity(tag: str, slug: str, label: str, type_: str | None = None) -> str:
    """Phase 4: rename ONE entity (label ± type) in the tag's namespace.

    Same unique-id pattern as start_digest: concurrent renames of the
    same entity must coexist (each PATCH is its own workflow; the
    activity's Neo4j SET is idempotent, last write wins).
    """
    suffix = uuid.uuid4().hex[:8]
    client = await get_client()
    handle = await client.start_workflow(
        RENAME_ENTITY_WORKFLOW_NAME,
        {"tag": tag, "slug": slug, "label": label, "type": type_},
        id=f"{RENAME_ENTITY_ID_PREFIX}{tag}-{slug}-{suffix}",
        task_queue=TASK_QUEUE,
    )
    return handle.id