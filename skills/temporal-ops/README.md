# Temporal Ops Skill

A skill for operating and diagnosing [Temporal](https://temporal.io/) environments — namespace administration, capacity management, IAM, certificate rotation, workflow health queries, batch operations, plus bottom-up diagnosis of stuck workflows, non-determinism errors, connectivity problems, certificate expirations, worker health issues, and rate limits.

Applies to both **Temporal Cloud** (`tcld` commands) and **self-hosted** (`temporal operator` commands) deployments. Data-plane operations (`temporal workflow`, `temporal batch`, `temporal schedule`) work on both.

> [!WARNING]
> This Skill is currently in Public Preview, and will continue to evolve and improve.
> It runs `temporal` and `tcld` commands against whatever environment your CLIs are authenticated to, and some of those commands have no undo. Before pointing an agent at production, scope the credential you give it — see [**AGENT-PERMISSIONS.md**](AGENT-PERMISSIONS.md).
> We would love to hear your feedback - positive or negative - over in the [Community Slack](https://t.mp/slack), in the [#topic-ai channel](https://temporalio.slack.com/archives/C0818FQPYKY). Bug reports and corrections are also welcome as [GitHub issues](https://github.com/temporalio/skill-temporal-ops/issues).

## Installation

### Via `npx skills` — supports all major coding agents

1. `npx skills add temporalio/skill-temporal-ops`
2. Follow prompts

### Via manually cloning the skill repo

1. `mkdir -p ~/.claude/skills && git clone https://github.com/temporalio/skill-temporal-ops ~/.claude/skills/temporal-ops`

Appropriately adjust the installation directory based on your coding agent.

## Before you point an agent at production

This skill runs `temporal` and `tcld` commands against whatever environment your CLIs are authenticated to, and some of those commands have no undo. The skill tells the agent to establish a blast radius and get your approval before running anything in that category — but that is a behavior, not a boundary.

Since you have to set up CLI authentication anyway, it is worth deciding *which* credential the agent gets: a scoped-down or read-only Service Account limits what any mutating command can reach, no matter what the agent decides to run. [**AGENT-PERMISSIONS.md**](AGENT-PERMISSIONS.md) covers that, plus command denylists and pre-execution hooks, with examples for Claude Code.

## What this skill covers

### Operations

- **Cloud namespace admin** — create, get, list, delete namespaces; add/remove regions; failover; HA config; retention, tags, codec-server, connectivity rules, search attributes, accepted-client-ca, certificate filters, export
- **Cloud capacity** — On-Demand vs Provisioned modes, APS/RPS/OPS, TRUs, capacity updates, throttling, APS management
- **Cloud IAM** — API key lifecycle, user management, user groups, service accounts, roles, namespace permissions
- **Cloud certificates** — mTLS cert generation, CA upload, certificate filters, rotation, auth method switching
- **Cloud export & connectivity** — Workflow History Export (S3/GCS), PrivateLink/PSC, connectivity rules, Cloud Ops API
- **Self-hosted admin** — cluster health, namespace CRUD, search attributes, Nexus endpoints via `temporal operator`
- **Workflow health queries** — List Filter queries to find stuck/hung workflows, task-queue poller status, workflow counts
- **Batch & lifecycle** — cancel, terminate, reset workflows; batch operations; schedules; external activity completion

### Diagnosis

- **Stuck workflows** — Event-History-driven triage that identifies the last event, classifies the cause, and proposes a fix
- **Non-determinism errors** — detection, local replay reproduction, and fix patterns
- **Connectivity failures** — can't connect, TLS handshake errors, endpoint mismatches, DNS issues
- **Certificate problems** — x509 errors, expiry, chain verification, openssl diagnostics
- **Authentication failures** — `UNAUTHENTICATED` vs `PERMISSION_DENIED`, mTLS identity mapping; Cloud adds `tcld login`, API key expiry, and Cloud role/namespace-permission mismatches
- **Worker health** — no pollers, task-queue backlog, worker registration, build-id mismatches
- **Rate limiting** — `RESOURCE_EXHAUSTED`, Cloud APS/RPS/OPS limits, self-hosted `frontend.rps` / `frontend.namespaceRPS` dynamic config, retry-after patterns
- **HA failover** (Temporal Cloud) — multi-region endpoint, DNS staleness, failover verification
- **Ambiguous runtime errors** — `context deadline exceeded`, `workflow is busy`
- **Replay debugging** — using the VS Code debugger extension to step through workflow history

## What this skill does NOT cover

- **Writing workflows/activities** — use [skill-temporal-developer](https://github.com/temporalio/skill-temporal-developer)
- **Worker performance tuning, scaling, capacity planning** — use [skill-temporal-workertuning](https://github.com/temporalio/skill-temporal-workertuning)
- **SDK-specific ergonomics** — use [skill-temporal-developer](https://github.com/temporalio/skill-temporal-developer)

## License

[MIT](LICENSE)
