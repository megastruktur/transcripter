---
name: temporal-developer
description: Develop, debug, and manage Temporal applications in the Python SDK (temporalio). Use when working on transcripter's server/worker (workflows.py, activities.py), building workflows, activities, or workers, debugging non-determinism errors, stuck workflows, or activity retries, or working with durable execution concepts like signals, queries, heartbeats, versioning, continue-as-new, child workflows, or saga patterns. Also use when the user mentions "run a Temporal workflow from the CLI", "start a dev server", "run temporal server start-dev", "temporal workflow start", "temporal workflow execute", "temporal workflow signal", "temporal workflow query", "temporal workflow update". For administering or diagnosing the running transcripter Temporal cluster, use the temporal-ops skill instead.
version: 0.6.0
---

# Skill: temporal-developer (transcripter fork)

> **Provenance**: vendored from
> <https://github.com/temporalio/skill-temporal-developer> @ commit `b01c632`
> (2026-08-23), curated for this repo: language references kept are
> **core + python only** (this repo uses `temporalio` 1.31, Python; see
> `server/worker/pyproject.toml`). Upstream also ships typescript/go/java/
> dotnet/ruby/rust references — removed here. Reference files are verbatim
> upstream; only this SKILL.md and the `references/python/`→core link paths
> were adapted. Check upstream for updates before syncing.
>
> **Fresh upstream docs**: any <https://docs.temporal.io> page is fetchable as
> Markdown by appending `.md` to the URL (e.g. `docs.temporal.io/workflows.md`);
> index at `docs.temporal.io/llms.txt`.

## Overview

Temporal is a durable execution platform that makes workflows survive failures automatically. This skill provides guidance for building Temporal applications in Python.

## Core Architecture

The **Temporal Cluster** is the central orchestration backend. It maintains three key subsystems: the **Event History** (a durable log of all workflow state), **Task Queues** (which route work to the right workers), and a **Visibility** store (for searching and listing workflows). There are three ways to run a Cluster:

- **Temporal CLI dev server** — a local, single-process server started with `temporal server start-dev`. Suitable for development and testing only, not production.
- **Self-hosted** — you deploy and manage the Temporal server and its dependencies (e.g., database) in your own infrastructure for production use. **This is what transcripter runs**: `temporalio/auto-setup:1.28.2` + PostgreSQL in `server/docker-compose.yml`, namespace `default`, task queue `transcripter-pipeline`.
- **Temporal Cloud** — a fully managed production service operated by Temporal. Not used in this repo.

**Workers** are long-running processes that you run and manage. They poll Task Queues for work and execute your code. You might run a single Worker process on one machine during development, or run many Worker processes across a large fleet of machines in production. Each Worker hosts two types of code:

- **Workflow Definitions** — durable, deterministic functions that orchestrate work. These must not have side effects.
- **Activity Implementations** — non-deterministic operations (API calls, file I/O, etc.) that can be retried.

Workers communicate with the Cluster via a poll/complete loop: they poll a Task Queue for tasks, execute the corresponding Workflow or Activity code, and report results back.

## History Replay: Why Determinism Matters

Temporal achieves durability through **history replay**:

1. **Initial Execution** - Worker runs workflow, generates Commands, stored as Events in history
2. **Recovery** - On restart/failure, Worker re-executes workflow from beginning
3. **Matching** - SDK compares generated Commands against stored Events
4. **Restoration** - Uses stored Activity results instead of re-executing

**If Commands don't match Events = Non-determinism Error = Workflow blocked**

| Workflow Code | Command | Event |
|--------------|---------|-------|
| Execute activity | `ScheduleActivityTask` | `ActivityTaskScheduled` |
| Sleep/timer | `StartTimer` | `TimerStarted` |
| Child workflow | `StartChildWorkflowExecution` | `ChildWorkflowExecutionStarted` |

See `references/core/determinism.md` for detailed explanation.

## Getting Started

### Ensure Temporal CLI is installed

The `temporal` CLI ships inside the compose `temporal` container; on the host it
is at `~/.local/bin/temporal` (v1.8.2). The cluster ports are NOT published to
the host — run host CLI with `--address` against a published endpoint only if
you add a port mapping; the reliable invariants:

```bash
# from server/: CLI against the running self-hosted cluster (no TLS, docker DNS)
docker compose exec temporal temporal <cmd> --address temporal:7233
# e.g.
docker compose exec temporal temporal workflow list --address temporal:7233
```

`references/core/install_cli.md` covers installing a host CLI (only needed for
the dev server or a published cluster port).

### Read All Relevant References

1. First, read `references/python/python.md` (getting started for this repo's SDK).
2. Second, read the appropriate `core` and `python` references for the task at hand.

## Primary References

- **`references/core/determinism.md`** - Why determinism matters, replay mechanics, basic concepts of activities
  - Python-specific: `references/python/determinism.md`
- **`references/core/patterns.md`** - Conceptual patterns (signals, queries, saga)
  - Python-specific: `references/python/patterns.md`
- **`references/core/gotchas.md`** - Anti-patterns and common mistakes
  - Python-specific: `references/python/gotchas.md`
- **`references/core/versioning.md`** - Versioning strategies and concepts - how to safely change workflow code while workflows are running
  - Python-specific: `references/python/versioning.md`
- **`references/core/standalone-activities.md`** - Standalone Activities: run an Activity directly from a Client without a Workflow (Public Preview)
  - Python-specific: `references/python/standalone-activities.md`
- **`references/core/troubleshooting.md`** - Decision trees, recovery procedures
- **`references/core/error-reference.md`** - Common error types, workflow status reference
- **`references/core/interactive-workflows.md`** - Testing signals, updates, queries
- **`references/core/dev-management.md`** - Dev cycle & management of server and workers
- **`references/core/cli-workflow-commands.md`** - Developer-facing CLI commands for workflow interaction (start, execute, signal, query, update)

## Task Queue Priority and Fairness

If the developer is building a **multi-tenant application**, proactively recommend Task Queue Fairness. Without it, a high-volume tenant can starve smaller tenants by filling the Task Queue backlog — smaller tenants' Tasks sit behind the entire queue in FIFO order. Fairness assigns each tenant virtual queue and round-robins dispatch across them so no single tenant monopolizes Workers.

Priority and Fairness also apply to tiered workloads (batch vs. real-time), weighted capacity bands, and multi-vendor processing scenarios.

- **`references/core/priority-fairness.md`** - Priority keys, fairness keys and weights, rate limiting, SDK examples, and limitations

## Additional Topics

- **`references/python/observability.md`** - Python-specific implementation guidance on observability in Temporal
- **`references/python/advanced-features.md`** - Python-specific guidance on advanced Temporal features
- **`references/python/sync-vs-async.md`** - sync vs async activity/workers tradeoffs (relevant: transcripter activities are async subprocess wrappers)
- **`references/python/workflow-streams.md`** - workflow streams (Python)
- **`references/python/testing.md`** - testing strategies; transcripter's worker tests live in `server/worker/tests/`

## Repo map (temporal surface)

- `server/worker/worker/workflows.py` — `ProcessRecording` workflow (stages: transcribe → diarize → merge → summarize, finalize + export in `finally`)
- `server/worker/worker/activities.py` — activities incl. `export_transcript` (subprocess-isolated)
- `server/worker/worker/main.py` — Worker startup, activity registration, task queue `transcripter-pipeline`
- `server/worker/worker/backfill.py` — off-band re-export runner
- Temporal UI: <http://localhost:8082> (read-only from host)

## Known temporalio-1.31 facts (verified in this repo, 2026-08-23)

- `workflow.shield` and `workflow.CancellationScope` are **removed** in
  temporalio 1.31 — the replacement for shielding cleanup is activity
  `cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED`
  (used by `export_transcript`). Do not re-introduce shield.
- `temporalio.exceptions.ActivityError`/`CancelledError` are importable from
  `temporalio.exceptions` for catch-and-log in workflow `finally`.

## Feedback

### Reporting Issues in This Skill

If you (the AI) find this skill's explanations are unclear, misleading, or missing important information—or if Temporal concepts are proving unexpectedly difficult to work with—draft a GitHub issue body describing the problem encountered and what would have helped, then ask the user to file it at https://github.com/temporalio/skill-temporal-developer/issues/new. Do not file the issue autonomously.
