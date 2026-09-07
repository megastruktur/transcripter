# Project: TRANSCRIPTER MAXIMUS

## General

The idea of the project is to give user (me) the ability to record, save, transcript, diarize and summarize calls.
Reference project: https://github.com/EzyGang/actavoces

Read ./SPECS.md for current app specs.

## Client design

Before creating, changing, or reviewing client UI, read and follow
[`DESIGN_GUIDELINES.md`](./DESIGN_GUIDELINES.md). It is the canonical contract
for the interface's visual language, interaction states, copy, and fixed-window
layout.

## Tooling hazards (omp edit tool on this repo)

The harness `edit` tool corrupts Svelte markup and Python in this repo at a high
rate (~1/2 of multi-block PUTs this session; historical rate ~1/3). Observed
failure modes:

- **Boundary echo**: the body's first line(s) get duplicated — an inserted
  block appears twice, or a replaced range leaves its old copy behind as an
  orphan.
- **Eaten neighbors**: a `PUT N.=M:` meant to insert before a line silently
  consumes an adjacent construct — `{/if}` closers, `let` declarations,
  function headers, closing braces. Symptom: `Cannot find name 'data'` /
  `attempted to close an element that was not open` from svelte-check.
- **Lost closing line**: single-line PUTs replacing a declaration sometimes
  drop the line that followed it.
- `write` on Python files is WORSE than `edit`: full-file writes have silently
  removed imports, changed types (`Mapped[str]`→`Mapped[int]`), and corrupted
  Cyrillic strings with visually-near-identical glyphs that pass the AST.

Defenses that keep velocity (mandatory on this repo):

1. **One edit → one check.** Never batch two edits before running the checker.
   Svelte/TS: `pnpm check` in `client/`. Python: `uv run ruff check` +
   `python -m py_compile` per edited file in `server/api` (or `server/worker`).
2. **Re-read ±10 lines after every edit** — the tool's success message is not
   proof; verify the anchor region actually contains what you intended.
3. **When an edit lands mid-construct, re-read the whole surrounding
   function/block and restore it in ONE complete PUT** — never patch the patch
   with two more micro-edits.
4. **Prefer small hunks over `write`** for existing files. `write` is only for
   genuinely new files.
5. **Verify with `git diff` after mutations**, expecting additive-only diffs on
   insertions; a removed import or changed literal you did not order = restore
   it immediately.
6. A failed `pnpm check` listing `Cannot find name 'X'` right after an edit is
   the signature of an eaten declaration — diff first, rewrite the block, do
   not "fix" by redeclaring elsewhere.
