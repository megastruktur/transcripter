---
id: FU-<YYYYMMDD>-<short-slug>
title: <one-line title of the escape>
severity: low        # low | medium | high | critical
status: open         # open | resolved | wontfix
discovered_in: <phase @ HEAD-sha-at-discovery, e.g. "Phase 5 Gate 1 @ abc1234">
discovered_at: <YYYY-MM-DD>
owner: unassigned    # unassigned | <person/agent>
related_files:
  - <path/to/file.ext>
related_plans:
  - .ship-it/plans/<slug>-<date>.md
related_followups:
  - FU-<id>          # or _none_
---

# <Title>

## Symptom

<Verbatim error output or observed misbehavior. Copy-paste the actual
text — do not paraphrase.>

## Reproduction

<Copy-pasteable commands plus assumed environment:

```bash
<command one>
<command two>
```

Assumed env: <versions, env vars, platform, anything non-obvious>>

## Root cause analysis

<Evidence-based analysis. Cite line numbers and the commit where the
regression entered, where applicable. If not determined, write exactly:
"Root cause unknown after N minutes — flagged for triage.">

## Suggested fix

<Concrete options with a recommendation. Do NOT implement the fix as
part of filing — this file records the escape, the fix belongs to a
future cycle.

- **Option A:** <description> — <tradeoff>
- **Option B:** <description> — <tradeoff>
- **Recommendation:** <A or B, one-line why>>

## Acceptance criteria

<Falsifiable conditions that prove resolution:

- <criterion that a test or command can verify>
- <criterion>>

## Notes

<Anything else: links, prior art, hunches. Or _none_.>

<!-- Append below when resolved — never delete this file.

## Resolution

- **Resolved in:** .ship-it/plans/<slug>-<date>.md (commit <sha>)
- **Resolved at:** <YYYY-MM-DD>
- **How:** <one paragraph — what actually fixed it vs. the suggestion>

-->

<!-- Append below only with explicit user approval.

## Wontfix Rationale

- **Decided by:** <user, date>
- **Reason:** <why this is accepted as-is>

-->
