<script lang="ts">
	import { onMount } from 'svelte';
	import EmptyState from '$lib/EmptyState.svelte';
	import Icon from '$lib/Icon.svelte';
	import {
		deleteTagDef,
		fetchTagDef,
		loadApiConfig,
		updateTag,
		type TagDef
	} from '$lib/api.svelte';

	let { tag, ondeleted = () => {}, ontimeline = () => {} }: { tag: string; ondeleted?: () => void; ontimeline?: () => void } = $props();

	let def = $state<TagDef | null>(null);
	let error = $state('');
	let loading = $state(true);
	// Auto-save editors (2026-09-07): every mutation PATCHes immediately —
	// the old explicit "Save tag" button was a UX trap (words lived only in
	// local state; a missed click silently lost them on navigation).
	// Vocabulary edits and context blur both flush the CURRENT state as one
	// atomic PATCH (full-list semantics, same as recording tags PATCH).
	let words = $state<string[]>([]);
	let newWord = $state('');
	let contextText = $state('');
	let contextSaved = $state('');
	let saving = $state(false);
	let saveError = $state('');
	let savedAt = $state('');
	let pendingFlush = $state(false);
	let confirmingDelete = $state(false);
	let deleteError = $state('');

	async function flush(): Promise<void> {
		if (saving) {
			// A PATCH is in flight; remember to re-send the CURRENT state
			// when it lands (the in-flight body may already be stale).
			pendingFlush = true;
			return;
		}
		saving = true;
		saveError = '';
		try {
			const contextSent = contextText;
			def = await updateTag(loadApiConfig(), tag, {
				vocabulary: words,
				context: contextSent
			});
			words = [...def.vocabulary];
			// Sync context ONLY when untouched since the request — never
			// clobber typing that happened while the PATCH was in flight.
			if (contextText === contextSent) contextText = def.context;
			contextSaved = def.context;
			savedAt = new Date().toLocaleTimeString();
		} catch (caught) {
			saveError = String(caught);
		} finally {
			saving = false;
			if (pendingFlush) {
				pendingFlush = false;
				void flush();
			}
		}
	}

	function addWord(): void {
		const w = newWord.trim();
		if (!w) return;
		// Same normalization as the server: casefold-dedup, first spelling wins.
		if (words.some((x) => x.toLowerCase() === w.toLowerCase())) {
			newWord = '';
			return;
		}
		words = [...words, w.slice(0, 64)];
		newWord = '';
		void flush();
	}

	function removeWord(index: number): void {
		words = words.filter((_, i) => i !== index);
		void flush();
	}

	function contextChanged(): boolean {
		return contextText !== contextSaved;
	}

	async function refresh(): Promise<void> {
		try {
			def = await fetchTagDef(loadApiConfig(), tag);
			words = [...def.vocabulary];
			contextText = def.context;
			contextSaved = def.context;
			error = '';
		} catch (caught) {
			error = String(caught);
		} finally {
			loading = false;
		}
	}

	async function remove(): Promise<void> {
		try {
			await deleteTagDef(loadApiConfig(), tag);
			ondeleted();
		} catch (caught) {
			const status = (caught as { status?: number }).status;
			deleteError =
				status === 409
					? 'Tag has recordings — detach it from them first'
					: String(caught);
			confirmingDelete = false;
		}
	}

	onMount(refresh);
</script>

{#if loading}
	<div class="tagdef-loading">Loading…</div>
{:else if error}
	<EmptyState icon="tags" title="Tag not found" hint={error} />
{:else if def}
	<header class="tagdef-head">
		<div class="tagdef-meta">
			<span>{def.recordings} recordings</span>
			{#if def.recordings > 0}
				<a href="#timeline" onclick={(e) => { e.preventDefault(); ontimeline(); }}>Open sessions →</a>
			{/if}
		</div>
	</header>

	<div class="vocab-section">
		<div class="vocab-heading">
			<strong>Vocabulary</strong>
			<span class="vocab-hint">Hot words bias recognition and summaries — applied on the next transcription or summary run.</span>
		</div>

		<form class="vocab-add" onsubmit={(e) => { e.preventDefault(); addWord(); }}>
			<label>
				<span class="sr-only">New word or phrase</span>
				<input
					type="text"
					placeholder="Name, term, phrase…"
					maxlength="64"
					bind:value={newWord}
				/>
			</label>
			<button type="submit" disabled={!newWord.trim()}>Add</button>
		</form>

		<ul class="vocab-list" aria-live="polite">
			{#each words as word, i (word)}
				<li class="vocab-row">
					<span class="vocab-word">{word}</span>
					<button class="vocab-remove" type="button" onclick={() => removeWord(i)} aria-label="Remove {word}">
						<Icon name="trash" size={13} />
					</button>
				</li>
			{:else}
				<li class="vocab-empty">No words yet — add names and terms that come up in this tag's sessions.</li>
			{/each}
		</ul>
	</div>

	<div class="vocab-section">
		<div class="vocab-heading">
			<strong>Context</strong>
			<span class="vocab-hint">What the LLM should know about this series — players, characters, projects, tone. Applied to summaries, extraction and digests on the next run.</span>
		</div>
		<textarea
			class="context-input"
			rows="6"
			placeholder="Setting, who is who, standing instructions…"
			bind:value={contextText}
			onblur={() => { if (contextChanged()) void flush(); }}
		></textarea>
	</div>
	<div class="vocab-actions">
		{#if saveError}
			<span class="vocab-error" role="alert">{saveError}</span>
		{:else if saving}
			<span class="vocab-saving">Saving…</span>
		{:else if savedAt}
			<span class="vocab-saved">Saved {savedAt}</span>
		{/if}
	</div>

	<div class="danger-section">
		{#if !confirmingDelete}
			<button class="danger-toggle" type="button" onclick={() => (confirmingDelete = true)} disabled={def.recordings > 0}>
				Delete tag
			</button>
			{#if def.recordings > 0}
				<span class="danger-note">Recordings carry this tag — detach them first.</span>
			{/if}
		{:else}
			<div class="danger-confirm">
				<span>Delete the registry entry? The vocabulary is lost; recordings and tag memory stay.</span>
				<button class="danger-yes" type="button" onclick={() => void remove()}>Delete</button>
				<button class="danger-no" type="button" onclick={() => (confirmingDelete = false)}>Keep</button>
			</div>
		{/if}
		{#if deleteError}
			<div class="vocab-error" role="alert">{deleteError}</div>
		{/if}
	</div>
{/if}

<style>
	.tagdef-loading { color: #8b8278; font-size: 12px; }
	.tagdef-head { display: grid; gap: 6px; }
	.tagdef-meta { display: flex; gap: 12px; align-items: baseline; font-size: 11px; color: #8b8278; }
	.tagdef-meta a { color: var(--brass); font-weight: 650; text-decoration: none; cursor: pointer; }
	.tagdef-meta a:hover { color: var(--bone); }

	.vocab-section { display: flex; flex-direction: column; gap: 10px; }
	.vocab-heading { display: grid; gap: 2px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
	.vocab-heading strong { color: #b5aa9c; font-size: 12px; }
	.vocab-hint { color: #746d64; font-size: 10px; }
	.context-input { min-height: 42px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 3px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 12px; font-family: inherit; line-height: 1.45; resize: vertical; }
	.context-input::placeholder { color: #746d64; }
	.context-input:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }

	.vocab-add { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
	.vocab-add input { min-height: 42px; }
	.vocab-add button { min-height: 42px; }

	.vocab-list { display: grid; list-style: none; margin: 0; padding: 0; }
	.vocab-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; padding: 8px 2px; border-bottom: 1px solid var(--line); }
	.vocab-word { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ded3c4; font-size: 12px; }
	.vocab-remove { display: grid; place-items: center; width: 28px; height: 28px; background: none; border: 1px solid transparent; border-radius: 3px; color: #8b8278; cursor: pointer; }
	.vocab-remove:hover { color: var(--red); border-color: rgba(213,45,36,.4); }
	.vocab-empty { padding: 10px 2px; color: #746d64; font-size: 11px; border-bottom: 1px solid var(--line); }

	.vocab-actions { display: flex; gap: 10px; align-items: center; }
	.vocab-saved { color: var(--brass); font-size: 10px; font-weight: 650; }
	.vocab-saving { color: #8b8278; font-size: 10px; font-weight: 650; }
	.vocab-error { color: var(--red); font-size: 11px; }

	.vocab-add button { border: 1px solid var(--brass); background: rgba(215, 167, 71, 0.12); color: var(--brass); border-radius: 3px; padding: 0 14px; font-size: 12px; font-weight: 700; cursor: pointer; }
	.vocab-add button:hover:not(:disabled) { color: var(--bone); border-color: var(--bone); }
	.vocab-add button:disabled { cursor: not-allowed; }
	.danger-section { display: flex; gap: 10px; align-items: center; border-top: 1px solid var(--line); padding-top: 12px; }
	.danger-toggle { color: #8b8278; background: none; border: 1px solid var(--line); border-radius: 3px; min-height: 34px; padding: 0 12px; cursor: pointer; font-size: 11px; }
	.danger-toggle:hover:not(:disabled) { color: var(--red); border-color: rgba(213,45,36,.5); }
	.danger-toggle:disabled { opacity: 0.5; cursor: not-allowed; }
	.danger-note { color: #746d64; font-size: 10px; }
	.danger-confirm { display: grid; gap: 8px; font-size: 11px; color: #c6baaa; }
	.danger-confirm span { color: #c6baaa; }
	.danger-yes { color: var(--red); background: none; border: 1px solid rgba(213,45,36,.5); border-radius: 3px; min-height: 34px; padding: 0 12px; cursor: pointer; font-size: 11px; }
	.danger-yes:hover { background: rgba(213,45,36,.12); }
	.danger-no { color: #8b8278; background: none; border: 1px solid var(--line); border-radius: 3px; min-height: 34px; padding: 0 12px; cursor: pointer; font-size: 11px; }
</style>
