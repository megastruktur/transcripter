---
name: transcripter-add-preset
description: >-
  Add a new capture preset and/or recording type to Transcripter Maximus:
  the YAML recognition profile (server/profiles), the recording-type slug
  contract, the client-side preset seed (presets.svelte.ts DEFAULT_PRESETS),
  verification gates, and deployment to dev/prod stacks. Use when asked to
  add a preset, add a recording/recognition type, create a domain profile
  (e.g. vet consultation, meeting, TTRPG), make a new summarize/enrich
  pipeline variant, or when a new profile does not appear in the client's
  Type dropdown.
metadata:
  version: "1.0"
---

# transcripter-add-preset

A **preset** is a one-tap capture bundle: `{name, recType, tags}`. A
**recording type** (`recType`) is a free-form slug that routes the pipeline
to a YAML **profile** on the server. There is NO enum or registry of types
in code — adding a type means adding a profile file; the client builds its
Type dropdown dynamically from `GET /profiles`.

Two paths:

- **A. Preset on an existing type** (`meeting`, `ttrpg`, `vet-consultation`,
  or no type) → client-only, §3. No server change.
- **B. New domain type** → profile YAML + test (§2), then optionally a
  preset seed (§3), smoke assert + SPECS bullet (§4).

## 1. The type-slug contract (read first)

`^[a-z0-9][a-z0-9-]{0,31}$` — lowercase, starts alphanumeric, then
alphanumerics/**dashes**, ≤32 chars. **Underscores are REJECTED with 400.**
`vet_consultation` is invalid; `vet-consultation` is valid.

Three synchronized mirrors — if you ever change the shape, change ALL:

| File | Symbol |
|---|---|
| `server/api/app/routes/recordings.py` | `TYPE_RE` (~line 64) |
| `server/api/app/routes/profiles.py` | `_SAFE_TYPE` (~line 33) |
| `server/worker/worker/profiles.py` | `_SAFE_TYPE` (~line 42) |

Unknown-but-well-shaped types are STORED as-is (pipeline matches no profile
→ default summarize). Changing a done recording's type via
`PATCH /recordings/{id}` auto-regenerates summarize+enrich.

## 2. New recognition profile (path B)

Create `server/profiles/<id>.yaml`. Full contract: `server/profiles/README.md`;
validator: `server/worker/worker/profiles.py` (pydantic, warn+skip on bad
files — a broken profile silently disappears from `GET /profiles`).
Fresh worked example: `vet-consultation.yaml` (commit 82f8807).

Required keys:

```yaml
id: my-domain            # [a-z0-9._-]+, unique; filename should match
version: 1.0.0
min_host_version: 0.10.0 # must be <= worker HOST_VERSION (0.10.0)
display_name: My Domain  # shown in the client Type dropdown
description: "One line"  # QUOTE if it contains a colon
type: my-domain          # the recording-type slug, §1 contract
summarize:
  prompt: |              # block scalar; MUST contain {transcript}
    ...{title}...        # {title} optional
    ТРАНСКРИПТ:
    {transcript}
  output_artifact: my-domain.md   # [a-z0-9._-]+; renames summary in Vault export
enrich:                  # optional; omit for summarize-only profiles
  known_entities: true   # true|false|positive int; true REQUIRES {known_entities} in prompt
  prompt: |
    ... JSON schema {"events":[...],"entities":[...],"relations":[...]} ...
    {known_entities}
    {tag_context}
    {corrections}
    ТРАНСКРИПТ:
    {transcript}
```

Rules that bite:

- **`{transcript}` mandatory** in both summarize and enrich prompts.
- **`known_entities: true` without `{known_entities}` placeholder → profile
  rejected on load** (cross-field validator). The reverse (placeholder
  without the flag) is legal — renders empty.
- `{tag_context}` / `{corrections}` are optional enrich placeholders (tag
  memory + operator corrections); include them for domain profiles.
- Unknown top-level fields are warn-ignored (forward-compat), NOT errors —
  a typo'd key silently does nothing. Legacy `tags:` routing is dead.
- Profile `type` collisions: sorted-first id wins + warning. Keep types unique.
- Prompt content language: match the operator's language (Russian for this
  deployment); **system names** (entity types, event kinds, relation types,
  JSON keys) stay English. Instruct output in the transcript's language.
- No timecodes in summaries; empty sections must say so; stick to transcript
  facts. For medical/vet domains add a HARD rule: dosages/medications ONLY
  as spoken, never invent or substitute standard doses (see vet-consultation).
- Enrich JSON schema is fixed in code (`worker/enrich.py`):
  `events[{ts,kind,summary,entities}]`, `entities[{slug,label,type}]`,
  `relations[{from_slug,to_slug,type}]`. The profile steers the DOMAIN via
  the prompt's type/kind/relation vocabularies — define each briefly.

Add/extend the shipped-profiles guard test
`server/worker/tests/test_shipped_profiles.py`: it asserts every shipped
`*.yaml`/`*.yml` loads with zero warn+skip (count parity with the loader's
glob), so a broken new profile fails CI instead of vanishing silently.
Add type-specific assertions (type slug, enrich presence, `{transcript}`).

## 3. Client preset seed (paths A and B)

`client/src/lib/presets.svelte.ts` — `DEFAULT_PRESETS` (~line 21):

```ts
{ id: 'preset-my-domain', name: 'My Domain', recType: 'my-domain', tags: ['mytag'] }
```

- `id`: `preset-` prefix + unique suffix (`newPresetId()` style).
- `recType`: `''` = no type (default pipeline); otherwise a §1 slug that a
  server profile serves. No client validation — a typo silently means
  "no profile matched".
- `tags`: lowercase normalized on save (`normalizeTag`); one tag per preset
  in the editor UI. The tag gives the recording a Vault namespace
  (vocabulary → ASR hot words, context → enrich `{tag_context}`, digest).

**Seeding gotcha:** `loadPresets()` seeds `DEFAULT_PRESETS` ONLY when
localStorage (`transcripter.presets.v1`) is empty/invalid. On an existing
installation a new seed NEVER appears automatically — the user creates it
once via `+ Preset` (Record tab). localStorage is per-device (desktop ↔
Android do not share presets until the preset-sync feature lands). Do not
"fix" this by re-seeding non-empty stores: deleted presets would resurrect.

No other client change is needed for a new type: `TypeProfileField.svelte`,
Recording Detail type editor, and QuickRecordPopup all render from
`GET /profiles` (`profiles.svelte.ts` cache, refreshed per mount).

## 4. Docs + smoke (path B)

- `server/scripts/e2e_smoke.sh` step 3b: add a `GET /profiles` listing
  assert for the new profile id (one jq line, ttrpg pattern).
- `SPECS.md`: dated bullet describing the type, profile, and output artifact.

## 5. Verify

```bash
cd server/worker && uv run pytest tests/test_shipped_profiles.py tests/test_profiles.py -q
uv run ruff check tests/test_shipped_profiles.py
cd ../../client && pnpm check          # if presets.svelte.ts touched
bash -n ../server/scripts/e2e_smoke.sh # if smoke touched
```

Live check (dev stack up, see `transcripter-stack-up`):

```bash
curl -sf -H "authorization: Bearer $TRANSCRIPTER_TOKEN" http://localhost:8090/profiles | jq '.[].id'
# new id must be listed; then PATCH a recording's type and watch auto-regen:
curl -sf -X PATCH -H "authorization: Bearer $TRANSCRIPTER_TOKEN" \
  -H 'content-type: application/json' -d '{"type":"my-domain"}' \
  http://localhost:8090/recordings/<id>
```

Full gates: `transcripter-test-suite`; end-to-end: `transcripter-e2e-smoke`.

## 6. Deploy

Profiles are bind-mounted read-only into api+worker and re-read on EVERY
stage run / `GET /profiles` call — **no restart, no rebuild, no migration**.

- **dev stack:** `server/profiles/` is mounted directly
  (`docker-compose.dev.yml`) — the file is live as soon as it exists.
- **prod stack:** copy the yaml to `/srv/transcripter/profiles/` on the
  prod host (compose mounts that path, not the checkout).
- Client seed changes ship with the next client build/release
  (`transcripter-release-ops`).

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| 400 `type must match ...` on upload/PATCH | Underscore or uppercase in slug (§1). Use dashes. |
| Profile missing from Type dropdown | YAML failed validation → warn+skip. Check api/worker logs for `profile:` warnings; run the shipped-profiles test locally. Usual suspects: missing `{transcript}`, `known_entities` without placeholder, `min_host_version` too new, colon in unquoted description. |
| Profile listed but summary is generic | Recording's `type` doesn't equal profile `type` (PATCH it), or a second profile with the same type sorts first (check logs for multi-match warning). |
| New preset seed not visible | Existing non-empty localStorage (§3 gotcha) — create via `+ Preset`. |
| Vault export still `summary.md` | `output_artifact` not set / profile not matched at export time (`worker/export.py` matches by recording type). |
