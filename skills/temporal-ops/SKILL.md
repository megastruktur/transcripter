---
name: temporal-ops
description: 'Administer and diagnose the transcripter self-hosted Temporal Server (docker compose) via the `temporal` CLI — not SDK code. Operations: cluster health, namespace CRUD, search attributes, workflow health, batch cancel/terminate/reset, schedules. Diagnosis: bottom-up triage of stuck workflows, non-determinism, worker-health, task-queue problems, payload size limits, performance bottlenecks, missed schedules. Do NOT trigger for generic TLS/gRPC errors unrelated to Temporal, or writing application code (use the temporal-developer skill).'
version: 0.2.0
---

# Skill: temporal-ops (transcripter fork)

> **Provenance**: vendored from
> <https://github.com/temporalio/skill-temporal-ops> @ commit `c2f7602`
> (2026-08-23), curated for this repo: **self-hosted only**. Removed upstream
> files: all `tcld`/Temporal Cloud ops references (cloud-*.md, ops/recipes.md,
> ha-failover, sdk-snippet-review), the upstream AGENT-PERMISSIONS.md, and the
> Cloud routing rows. Kept: `temporal operator` + data-plane CLI, full triage
> tree. `authentication.md` and `rate-limits.md` are retained because remaining
> files cross-link them; their Cloud sections are dead weight here but
> harmless. Reference files are verbatim upstream; only this SKILL.md was
> adapted. Check upstream for updates before syncing.
>
> **Fresh upstream docs**: any <https://docs.temporal.io> page is fetchable as
> Markdown by appending `.md` to the URL (e.g. `docs.temporal.io/self-hosted-guide.md`);
> index at `docs.temporal.io/llms.txt`.

## THIS REPO'S ENVIRONMENT (read first — replaces upstream "identify the backend")

- **Backend is always self-hosted** here: `temporalio/auto-setup:1.28.2` +
  PostgreSQL in `server/docker-compose.yml`. Never `tcld`, never Cloud.
- Namespace: `default`. Task queue: `transcripter-pipeline`.
- **Cluster port 7233 is NOT published to the host.** Run every `temporal`
  command inside the compose network from `server/`:

  ```bash
  alias t='docker compose exec temporal temporal'   # from server/
  t workflow list --address temporal:7233
  t operator cluster health --address temporal:7233
  ```

  The `--address temporal:7233` flag is required inside the container (CLI
  defaults to `localhost:7233`, but the server listens on the service DNS name;
  there is no localhost listener inside the CLI's container). Plain HTTP, no
  TLS, no auth (LAN compose network).
- Temporal UI (read-only, host): <http://localhost:8082>
- Host CLI `~/.local/bin/temporal` v1.8.2 exists but cannot reach the cluster
  (unpublished port; `docker compose port temporal 7233` → unpublished). Don't
  fight it — use the exec form above.
- Worker: `server/worker/` (Python, temporalio 1.31, activities registered in
  `worker/main.py`). Workflow type: `ProcessRecording`, workflow IDs
  `process-recording-<recording-uuid>`.

## Overview

This skill operates and diagnoses Temporal environments. It has two modes:

- **Operations:** the user wants to do something — check cluster health, find unhealthy workflows, cancel a batch, inspect a task queue. The skill executes the right commands and interprets the output.
- **Diagnosis:** the user arrives with a symptom — a stuck workflow, a connection timeout, a non-determinism panic. The skill routes the investigation through a layered, bottom-up diagnosis until a root cause is identified with a confidence score.

It does not teach how to write workflows or activities (use `temporal-developer` for that), and it does not reproduce exhaustive CLI flag tables — run `temporal <cmd> --help` for those, and see [cli-conventions.md](references/ops/cli-conventions.md) for cross-command CLI conventions. The boundary is: if the user needs to administer or troubleshoot a running Temporal environment, this skill applies.

## Out of scope

- **Writing workflows, activities, or SDK code** → `temporal-developer` skill.
- **Exhaustive CLI flags / command reference** → run `temporal <cmd> --help`; **cross-command CLI conventions** → [cli-conventions.md](references/ops/cli-conventions.md).
- **Temporal Cloud / `tcld`** → not applicable in this repo (deleted from this fork).
- **Helm, Kubernetes, database admin, monitoring stack config** — beyond the CLI surface.

If the conversation drifts into one of these areas, hand off to the relevant sibling skill rather than improvising.

## Philosophy

### Operator discipline

When the user wants to perform an operational task:

1. **Identify the intent.** In this repo the backend is fixed (self-hosted compose; see above) — no Cloud/self-hosted question to ask. Data-plane operations (`temporal workflow`, `temporal batch`, etc.) work as documented.
2. **Execute commands and interpret output.** Run the documented command (via the `docker compose exec` form), read the result, and report what it means — or act on it if the user asked for an action. Read-only commands (`get`, `list`, `describe`, `count`, `show`) run freely. Anything listed under [Destructive operations](#destructive-operations) is proposed to the user first.
3. **Verify the result.** After a mutating operation, confirm the new state matches the user's intent.

### Destructive operations

An operation belongs to this tier if it is **irreversible** (`temporal operator namespace delete` removes every Workflow Execution and Task Queue), **moves production traffic**, or **fans out to every match** (any `--query` form). Apply the test to the operation in front of you — this is a rule, not a list, and a command's absence from any list in this skill does not place it outside the tier.

For anything in the tier: gather the evidence and **propose**. Do not run it on your own initiative, and do not run one to find out what it would do.

1. **Blast radius as a number, not a description.** For any `--query` form, run `temporal workflow count --query '<query>'` with the byte-identical query first and carry the result into the proposal. A filter with no narrowing predicate beyond `ExecutionStatus="Running"` matches every open Execution in the Namespace.
2. **Name the target.** State the exact command, the target, and the Namespace it resolves to. If the Namespace was inferred from context rather than stated by the user, say so — a destructive command aimed at the wrong Namespace is the most common way this goes wrong.
3. **Ask explicitly, then run it so it completes.** Put the command, the target, and — for any `--query` form — the count from step 1 to the user as a direct question, and wait for an answer. Once they approve, run it with `--yes` on the `--query` batch forms; that flag is what lets an approved batch finish, since the interactive prompt needs a terminal and without one the command reports `user denied confirmation` and does nothing. `--yes` belongs in a command the user approved, never in a retry of one that failed its prompt. Do not substitute a loop over single-target `workflow terminate --workflow-id`. Approval covers one command against one target; it does not carry to the next command, a widened query, or a second Namespace.
4. **Verify, and know the abort path.** Re-run the corresponding `get`, `describe`, or `count`. A batch job drains asynchronously: `temporal batch describe --job-id <id>` shows how far it has gotten and `temporal batch terminate --job-id <id>` stops it before it reaches the rest of its matches.

When a reversible sibling reaches the same goal, propose it alongside: `cancel` lets Workflow cleanup code run where `terminate` does not; `apikey disable` is reversible where `delete` is not; `accepted-client-ca add` appends where `set` replaces.

Assume nothing in the environment will stop a destructive command on your behalf. Credential scope, command denylists, and confirmation prompts may or may not be configured, and their possible presence is not a reason to skip any step above — you are the safeguard the user is relying on.

### Diagnostic discipline

When the user arrives with a failure or anomaly:

1. **Bottom-up diagnosis.** Verify the lower layer before blaming the upper one. The layers, from bottom to top:
   1. DNS / network path
   2. TCP / port reachability
   3. TLS handshake
   4. Authentication (API key or mTLS client cert)
   5. gRPC health and Temporal frontend reachability
   6. Temporal namespace, task queues, workers
   7. Workflow code (determinism, signals, timers, child workflows)

   The full ladder lives in [diagnostic-ladder.md](references/triage/diagnostic-ladder.md). In this repo layers 3–4 (TLS/auth) are vacuous — plain gRPC inside the compose network — so a connectivity symptom starts at layer 6.

2. **Always verify the next layer up** rather than prescribing a speculative fix. If TLS works, prove auth works before blaming the workflow. If pollers are present, prove the workflow's last event before blaming the worker.

3. **Attach a confidence score** (1-10) to every proposed diagnosis:
   - 9-10: symptoms, operation, and confirming signals line up cleanly.
   - 6-8: evidence is good but at least one alternative remains plausible.
   - 1-5: the issue is still ambiguous; the "fix" is the next discriminating check, not a root cause.

4. **Name ambiguity explicitly.** Errors like `context deadline exceeded` are not self-describing. Surface that, gather more context, and scope the next step narrowly.

These are skill conventions, not docs-derived facts.

## Intent routing

### Operations

Find the row that matches the user's intent. The reference file contains the commands and procedures.

| Intent | Category | Reference |
|---|---|---|
| Self-hosted cluster health, describe, namespace CRUD | Self-hosted admin | [self-hosted-admin.md](references/ops/self-hosted-admin.md) |
| Self-hosted search attributes, Nexus endpoints | Self-hosted admin | [self-hosted-admin.md](references/ops/self-hosted-admin.md) |
| Find stuck/hung/unhealthy workflows via list queries | Workflow health | [workflow-health.md](references/ops/workflow-health.md) |
| Task queue poller status, workflow counts | Workflow health | [workflow-health.md](references/ops/workflow-health.md) |
| Cancel, terminate, or reset workflows | Workflow recovery | [workflow-stuck.md#recovery-commands](references/triage/workflow-stuck.md#recovery-commands) |
| Bulk / batch operations on workflows (`--query`) | CLI conventions | [cli-conventions.md](references/ops/cli-conventions.md#the---query--batch-job-bridge) |
| Schedule CRUD, time-spec, and operations | CLI conventions | [cli-conventions.md](references/ops/cli-conventions.md#schedule-time-spec-forms) |
| Complete or fail an activity externally | CLI conventions | [cli-conventions.md](references/ops/cli-conventions.md#operation--command-index) |

### Diagnosis

Find the row that matches the user's symptom. Start the investigation at the first check, then read the linked reference.

| Symptom | Category | First check | Reference |
|---|---|---|---|
| `connection refused`, cannot reach frontend | Connectivity | `nc -zvw10 <host> 7233` | [connectivity.md#connection-refused](references/triage/connectivity.md#connection-refused) |
| `no such host`, DNS resolution fails | Connectivity | `dig +short <host>` or `nslookup <host>` | [connectivity.md#dns](references/triage/connectivity.md#dns) |
| `tls: handshake failure`, server rejects handshake | Certificates | `openssl s_client -connect <host>:7233 -servername <host> </dev/null` | [certificates.md#handshake-failure](references/triage/certificates.md#handshake-failure) |
| `x509: certificate has expired` or `not yet valid` | Certificates | `openssl x509 -enddate -noout -in cert.pem` | [certificates.md#expired-or-not-yet-valid](references/triage/certificates.md#expired-or-not-yet-valid) |
| `x509: certificate signed by unknown authority` | Certificates | `openssl verify -CAfile ca.pem client.pem` | [certificates.md#unknown-authority](references/triage/certificates.md#unknown-authority) |
| `RESOURCE_EXHAUSTED` gRPC status | Rate limits | Identify which limit fired: the `resource_exhausted_cause` label (self-hosted) | [rate-limits.md#identifying-which-limit-was-hit](references/triage/rate-limits.md#identifying-which-limit-was-hit) |
| Task queue shows no pollers | Worker health | `temporal task-queue describe --task-queue <q>` | [worker-health.md#what-no-pollers-looks-like](references/triage/worker-health.md#what-no-pollers-looks-like) |
| Workflow stuck on a pending activity / timer / child / signal | Workflow stuck | `temporal workflow describe --workflow-id <id>` | [workflow-stuck.md#the-primary-inspection-command-temporal-workflow-describe](references/triage/workflow-stuck.md#the-primary-inspection-command-temporal-workflow-describe) |
| `NondeterminismError`, repeating `WorkflowTaskFailed` | Non-determinism | Identify the last `WorkflowTaskFailed` cause in the Event History | [non-determinism.md#the-wft-failure-signature-of-non-determinism](references/triage/non-determinism.md#the-wft-failure-signature-of-non-determinism) |
| Replay fails locally but prod workflow was running | Non-determinism | Fetch the history and run the SDK replayer in one test | [replay.md#step-2--run-the-sdk-replayer-all-supported-sdks](references/triage/replay.md#step-2--run-the-sdk-replayer-all-supported-sdks) |
| `context deadline exceeded` (unknown layer) | Runtime errors | Identify which operation and SDK emitted it | [runtime-errors.md#deadline-exceeded](references/triage/runtime-errors.md#deadline-exceeded) |
| `Workflow is busy` / `ResourceExhausted` on signal/update/query to one Workflow (BusyWorkflow) | Runtime errors | Rule out account-limit throttling, then split the metric by `operation` | [runtime-errors.md#workflow-lock-contention-busyworkflow](references/triage/runtime-errors.md#workflow-lock-contention-busyworkflow) |
| `PAYLOADS_TOO_LARGE`, `exceeds size limit`, payload/gRPC blob size error | Blob size limits | Check whether the issue is payload (2 MB) or gRPC message (4 MB) | [blob-size-limits.md](references/triage/blob-size-limits.md) |
| Workflow stuck in invisible retry loop (gRPC message too large) | Blob size limits | Check Worker logs for `ResourceExhausted`, reduce batch size | [blob-size-limits.md](references/triage/blob-size-limits.md) |
| High schedule-to-start latency, task slot depletion, slow Workflow Tasks | Performance bottlenecks | Check `temporal_workflow_task_schedule_to_start_latency` P95 | [performance-bottlenecks.md](references/triage/performance-bottlenecks.md) |
| High replay latency, cache evictions, deadlock detected | Performance bottlenecks | Check `workflow_task_replay_latency` and sticky cache metrics | [performance-bottlenecks.md](references/triage/performance-bottlenecks.md) |
| Schedule did not fire, missed catchup window | Missed Schedule Actions | `temporal schedule list` + `temporal schedule describe` (self-hosted path) | [schedule-missed.md](references/triage/schedule-missed.md) |

If a symptom does not map to a row, start at [diagnostic-ladder.md](references/triage/diagnostic-ladder.md) and work up from whichever layer was last known healthy.

## The process

### Operations path

#### Step 1: Identify intent

Determine what the user wants to do. In this repo the backend is fixed (self-hosted compose) — proceed without the upstream Cloud/self-hosted question.

#### Step 2: Execute and interpret

Look up the intent in the Operations table above. Read the linked reference file for the exact commands, flags, and expected output. Prefix every command with the repo's exec form (`docker compose exec temporal temporal --address temporal:7233 …`, run from `server/`). Run the command and interpret the result for the user.

#### Step 3: Verify

After a mutating operation (create, update, delete, rotate), confirm the new state:

- Re-run the corresponding `get` or `describe` command
- Confirm the output matches the user's intent
- Report the result

### Diagnosis path

#### Step 1: Identify the symptom

Ask the user for the exact, copy-pasted error text. Do not accept paraphrases — the exact string often encodes the layer (e.g. `x509:` prefix means TLS/cert layer, `RESOURCE_EXHAUSTED:` prefix means gRPC rate limit, `NondeterminismError` means workflow replay layer).

Confirm three things before continuing:

- What command was run, or what SDK call produced the error?
- What environment produced it (here: always the compose self-hosted cluster unless stated otherwise)?
- What changed recently (new deploy, new worker image, new namespace)?

#### Step 2: Gather context

The context the investigation needs depends on the category. At minimum:

- **For a stuck workflow:** workflow ID (here: `process-recording-<uuid>`), run ID, and the output of `temporal workflow describe --workflow-id <id>` (pending-operation state lives here, not in the Event History alone). Event History via `temporal workflow show` is the companion view.
- **For a worker health issue:** worker logs (`docker compose logs worker`; registration errors, auth errors, panics), the output of `temporal task-queue describe --task-queue transcripter-pipeline`, and `temporal worker describe --task-queue transcripter-pipeline` for per-worker details.
- **For a non-determinism error:** the worker log line containing the error, the workflow type name (`ProcessRecording`), and access to the history JSON for replay.

#### Step 3: Descend the ladder

Use [diagnostic-ladder.md](references/triage/diagnostic-ladder.md) to pick the right starting layer. As a rule of thumb:

- Worker / task-queue symptom → start at layer 6 (namespace + pollers).
- Stuck workflow / determinism symptom → start at layer 7 (workflow code), but confirm layer 6 (worker is actually polling) first.
- Connectivity symptom → in this repo, skip TLS/auth layers; it's compose DNS or an unpublished port.

Each layer has a command that proves it healthy and a failure signature that tells you whether the problem lives at that layer or higher.

#### Step 4: Fix and verify

Prescribe the fix scoped to the root cause. Then verify by re-running the layer's healthy-check command and, if possible, the original user operation. Attach the confidence score to the diagnosis.

If the layer above the fix is still failing, return to step 3 and continue walking upward — the first broken layer is rarely the only one.

## Prerequisites

- **Temporal CLI** — available two ways in this repo: inside the compose `temporal` container (use `docker compose exec temporal temporal --address temporal:7233 …` from `server/`), or host `~/.local/bin/temporal` v1.8.2 (only useful with a published port or `temporal server start-dev`).
- `tcld` — not applicable (self-hosted only).

## Reference files

### Operations

- [self-hosted-admin.md](references/ops/self-hosted-admin.md) — Self-hosted control plane via `temporal operator`: cluster health/describe, namespace CRUD, search-attribute create/list/remove, Nexus endpoint CRUD.
- [workflow-health.md](references/ops/workflow-health.md) — Data-plane health queries: `temporal workflow list` with List Filters, `temporal workflow describe`/`show`/`count`, `temporal task-queue describe` for poller status.
- [cli-conventions.md](references/ops/cli-conventions.md) — Cross-command `temporal` CLI conventions: connection/identity (`TEMPORAL_*` env vars ↔ `--address`/`--namespace`/`--api-key`, `--identity`), output/formatting (`--output`, `--time-format`, payload shorthand), the `--query` ⇒ batch-job bridge (with `temporal batch describe/list/terminate`), and schedule time-spec forms. Ends with an operation→command index that routes each data-plane operation to its owner file. Delegates exhaustive flags to `temporal <cmd> --help`.

### Diagnosis

- [diagnostic-ladder.md](references/triage/diagnostic-ladder.md) — the seven-layer bottom-up model, with one canonical command per layer and cross-links into the topical leaves.
- [connectivity.md](references/triage/connectivity.md) — DNS, TCP, endpoint families, firewall/proxy shapes, quick diagnostic scripts.
- [certificates.md](references/triage/certificates.md) — x509 and TLS alert strings, expiry / unknown-authority / hostname-mismatch / key-mismatch diagnosis, openssl recipes. (Cloud sections inapplicable here.)
- [authentication.md](references/triage/authentication.md) — `UNAUTHENTICATED` vs `PERMISSION_DENIED`, API-key lifecycle, mTLS after TLS. (Cloud sections inapplicable here; kept for cross-links — no auth on this cluster.)
- [workflow-stuck.md](references/triage/workflow-stuck.md) — Workflow Execution Status values, `temporal workflow describe` as the primary inspection command, Event History via `temporal workflow show`, pending activities / child workflows / signals / Workflow Tasks, WorkflowTaskFailed retry loops, recovery commands (signal, terminate, cancel, reset, pause/unpause).
- [non-determinism.md](references/triage/non-determinism.md) — determinism definition, WFT-failure signature, ND-inducing code patterns, per-SDK error shapes, identifying ND from Event History, local replay reproduction, remediation via Worker Versioning / patching / reset.
- [worker-health.md](references/triage/worker-health.md) — no-pollers runbook via `temporal task-queue describe`, reachability and versioning, worker-level describe, schedule-to-start latency, worker task slots, sticky execution and sticky cache, worker heartbeating, worker log signatures.
- [rate-limits.md](references/triage/rate-limits.md) — what `RESOURCE_EXHAUSTED` means (and does not), self-hosted `frontend.rps` / `frontend.namespaceRPS` dynamic config, identifying which limit fired via the `resource_exhausted_cause` label. (Cloud APS/RPS/OPS sections inapplicable here.)
- [runtime-errors.md](references/triage/runtime-errors.md) — deadline-exceeded disambiguated by operation and by where the call was made, Workflow lock contention (BusyWorkflow), routing for `no pollers` / `INVALID_ARGUMENT` / unspecified `UNAVAILABLE`.
- [replay.md](references/triage/replay.md) — fetching Event History with the SDK client (CLI export as fallback), running the SDK replayer (Python section applies), `TEMPORAL_DEBUG` and the deadlock detector, interpreting divergent and successful replays.
- [blob-size-limits.md](references/triage/blob-size-limits.md) — Payload size limit (2 MB) and gRPC message size limit (4 MB): error messages, per-SDK behavior (Python 1.23.0+ vs. others), claim check pattern, batch-size reduction.
- [performance-bottlenecks.md](references/triage/performance-bottlenecks.md) — Latency and throughput diagnosis via SDK metrics: schedule-to-start latency, workflow task execution latency, replay latency, activity execution latency, task slot depletion, network request metrics, sticky cache metrics.
- [schedule-missed.md](references/triage/schedule-missed.md) — Missed Schedule Actions: investigation via `temporal schedule list` + `temporal schedule describe`, DescribeSchedule fields (`missedCatchupWindow`, `overlapSkipped`, `bufferDropped`), default catchup window (one year), root causes, overlap policies (6 values), backfill remediation.
- [recipes.md](references/triage/recipes.md) — four end-to-end triage walkthroughs: stuck workflow at 3am, cert expired with workers offline, task-queue backlog mystery, non-determinism caught in prod.

## Feedback

### Reporting Issues in This Skill

If you (the AI) find this skill's explanations are unclear, misleading, or missing important information, draft a GitHub issue body describing the problem encountered and what would have helped, then ask the user to file it at https://github.com/temporalio/skill-temporal-ops/issues/new. Do not file the issue autonomously.