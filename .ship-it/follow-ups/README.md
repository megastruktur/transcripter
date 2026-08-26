# `.ship-it/follow-ups/` — Escapes Ledger

Structured records of issues discovered during ship-it cycles that were
NOT addressed in the cycle that found them. Nothing escapes silently:
every escape gets a file; every file gets resolved or explicitly
accepted as `wontfix` with user approval.

## Filing

- Copy `TEMPLATE.md` → `<slug>-<YYYY-MM-DD>.md` (kebab-case slug).
- Fill EVERY frontmatter field and EVERY body section — incomplete
  files are returned for completion.
- Commit the file in the SAME commit that surfaced the issue.
- Reference the `id` in the verification report and the Phase 8 summary.

## Severity

| Level     | Meaning                                                      |
|-----------|--------------------------------------------------------------|
| `low`     | Cosmetic or narrow edge case; no user impact                |
| `medium`  | Degrades a workflow; workaround exists                       |
| `high`    | Blocks a workflow or produces wrong results; no workaround   |
| `critical`| Data loss, security, or financial impact possible            |

Severity must be honest — `low` is for cosmetic/edge cases only.

## Status lifecycle

```
open ──→ resolved   (fix landed; Resolution block appended)
  └───→ wontfix    (user-approved; Wontfix Rationale block appended)
```

- `open` → `resolved`: after the fixing commit lands, append a
  **Resolution block** (see template footer), then flip the frontmatter.
- `open` → `wontfix`: only with explicit user approval. Append a
  **Wontfix Rationale block** citing the decision.
- **Never delete a follow-up file.** The history is the point — future
  agents searching "did we ever hit this before?" must find it intact.

## Surfacing

```bash
rg "^status: open" .ship-it/follow-ups/ -g '!TEMPLATE.md' -g '!README.md'     # all open
rg "^severity: high" .ship-it/follow-ups/ -g '!TEMPLATE.md' -g '!README.md'   # by severity
ls -lt .ship-it/follow-ups/*.md | grep -v -e TEMPLATE -e README | head -20    # recent

# Fallback if rg is unavailable:
grep -rl "^status: open" .ship-it/follow-ups/ --exclude=TEMPLATE.md --exclude=README.md
```

This directory is the only honest read of project tech debt — keep it
accurate.
