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

`edit` corrupts Svelte/Python here in ~1/3–1/2 of multi-block PUTs; `write` on
Python is worse. Rules:

- Failure modes: PUT boundary echo (block duplicated / old copy left behind);
  eaten neighbors (`{/if}` closers, `let` declarations, function headers,
  closing braces — svelte-check says `Cannot find name 'X'` or `attempted to
  close an element that was not open`); Python `write` silently drops imports,
  changes types, corrupts Cyrillic glyphs that pass the AST.
- One edit → one check: `pnpm check` (client), `uv run ruff check` +
  `python -m py_compile` (server). Never batch edits before checking.
- Re-read ±10 lines after every edit; the success message is not proof.
- Edit landed mid-construct? Re-read the whole block, restore in ONE complete
  PUT — never patch the patch with micro-edits.
- `write` only for new files; existing files get small `edit` hunks.
- `git diff` after mutations: insertions must be additive-only; a removed
  import or changed literal you did not order = restore immediately.
