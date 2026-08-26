---
id: FU-20260826-client-test-runner
title: Client has no test runner — sanitizer behavior unpinned against dependency drift
severity: low
status: open
discovered_in: Phase 7 roborev job 2013 @ 0910696
discovered_at: 2026-08-26
owner: unassigned
related_files:
  - client/src/lib/Markdown.svelte
  - client/package.json
related_plans:
  - .ship-it/plans/artifact-markdown-render-2026-08-26.md
related_followups:
  - _none_
---

# Client has no test runner — sanitizer behavior unpinned against dependency drift

## Symptom

Roborev cycles 2009 → 2010 → 2012 → 2013 each found a hole the previous
sanitizer config left open (default allowlist → FORBID_TAGS a/img →
ALLOWED_TAGS allowlist → data/aria passthrough). Verbatim from job 2013:

> "This is the third consecutive change to the sanitizer expression … and
> each iteration fixed a hole the previous one left open — yet the behavior
> still has no regression test pinning it (`client/package.json` has no test
> runner or scripts entry for one)."

## Reproduction

```bash
grep -c vitest client/package.json   # 0 — no runner, no test script
```

Assumed env: client is Svelte 5 + Vite 5, pnpm. Any future
`pnpm update marked dompurify` can silently change sanitize defaults.

## Root cause analysis

The client never had a unit-test runner (only `check`/`build` scripts).
The Markdown sanitizer in `client/src/lib/Markdown.svelte:15-29` is
security-sensitive config whose correctness depends on DOMPurify defaults
(`ALLOW_DATA_ATTR`, `ALLOW_ARIA_ATTR`, URI regexes) that can drift across
dependency bumps.

## Suggested fix

- **Option A:** Add vitest + happy-dom, one spec asserting sanitize output
  for `[t](url)`, `![a](url)`, `<video src>`, `<source srcset>`,
  `<td background>`, `<div style>`, bare URLs, and the allowed-tag set —
  introduces the project's first client test infrastructure.
- **Option B:** Extract the sanitize config into a pure function and test it
  via a one-off `node --test` script with jsdom — lighter, but non-standard
  for a Vite project.
- **Recommendation:** A — vitest is the ecosystem default for SvelteKit and
  unlocks testing for the rest of the client.

## Acceptance criteria

- `pnpm --dir client test` exits 0 running a spec that fails when any of the
  listed XSS/egress vectors survive sanitization.
- Spec covers at minimum: link tag stripped keeping text, img stripped,
  video/source/style vectors stripped, data-*/aria-* attributes stripped.

## Notes

Deferred at ship-it loop cap: after 3 roborev cycles with LOW-only findings,
remaining items are fixed without further full review or filed here. The
manual XSS probe (browser, marked 18.0.11 + dompurify 3.4.14 pipeline)
covered the same vectors once; it is not repeatable in CI.
