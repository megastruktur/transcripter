# Tag Registry + Vocabularies (2026-09-04)

Feature: tags as first-class entities with hot-word vocabularies that bias ASR and summarize.

## Schema
- `tag_defs` table: `name` PK (normalized trim+lower), `vocabulary TEXT[]` (JSON variant on SQLite), `context TEXT NOT NULL DEFAULT ''` (added 2026-09-05, idempotent `ALTER TABLE ... IF NOT EXISTS` for existing Postgres at API startup), created_at/updated_at. Created by API `create_all`; mirrored in worker `db.py` (worker READS only; keep in sync).

## API (server/api/app/routes/tags.py + db_helpers.py)
- `POST /tags {name, vocabulary?, context?}` → 201; 409 exists; 400 bad name (`_TAG_RE`).
- `GET /tags/{tag}` → {name, vocabulary, context, recordings, created_at}; 404 unregistered.
- `PATCH /tags/{tag} {vocabulary?, context?}` → partial-PATCH (absent = unchanged, at least one required else 422); vocabulary full-list, context full-text (both whitespace-stripped via `_normalize_context`); UPSERT (legacy tag gains row, absent field keeps default).
- `DELETE /tags/{tag}` → 204 registry row only; 409 while recordings carry it.
- `GET /tags` unions derived (recordings) + registry; rows carry `registered`, `vocabulary_count`; zero-recording registered tags list with count 0.
- Auto-registration: `register_tag_defs()` in db_helpers.py — dialect-branched INSERT ON CONFLICT DO NOTHING, called in recordings.py create/direct/PATCH inside the caller's transaction.
- Vocabulary normalization: trim, casefold-dedup (first spelling wins), casing preserved, ≤200 entries × 64 chars.

## Worker (activities.py)
- `_hotword_prompt(s, tags)`: union of TagDef vocabularies → `"Термины и имена, которые могут прозвучать: …"`, 900-char cap, whole-word truncate. Passed as `prompt=` to every `_transcribe_file` call (chunks, stereo channels, whole-file). Suspect-chunk reset keeps hotwords (`prompt=hotwords, reset_context=reset` — replaced the old `prompt=""` reset marker; condition_on_previous_text stays the reset signal).
- `_glossary_block(s, tags)`: same union → `"; "`-joined; summarize appends it in the system message after the recap block (same single-system-message rail — llama-server template constraint).
- `_context_block(s, tags)` (2026-09-05): union of `tag_defs.context` in recording tag order → `### <tag>\n<text>` sections joined by blank lines, None when all empty. Rides: summarize system message (between recap and glossary), enrich `{tag_context}` opt-in placeholder (7th arg to `extract_from_transcript`), digest prompt `context_section` (collapses when empty).
- No retroactive regeneration: applies on next transcribe/summarize/enrich/digest run.

## Client
- Rail item `Tags` (icon 'tags', between Library and Vault); `/tags` manifest page; `/tags/[tag]` editor (vocabulary add/remove + context textarea, one Save PATCHes BOTH, delete with confirm). api.svelte.ts: createTag/fetchTagDef/updateTag/deleteTagDef + TagCount{registered?, vocabulary_count?}; TagDef now carries `context: string`.

## Tests
- api: tests/test_tag_registry.py (17) + phase0 GET /tags contract updated (registered/vocabulary_count fields).
- worker: tests/test_tag_vocab.py (9); test_chunked_stages suspect test updated to new prompt contract.

Note: deploy needs api+worker image rebuild (both sides changed); tag_defs table auto-creates on API startup.