<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import BackButton from '$lib/BackButton.svelte';
	import EmptyState from '$lib/EmptyState.svelte';
	import Icon from '$lib/Icon.svelte';
	import {
		deleteTagDef,
		fetchTagDef,
		loadApiConfig,
		updateTagVocabulary,
		type TagDef
	} from '$lib/api.svelte';

	const tag = $derived(decodeURIComponent(page.params.tag ?? ''));

	let def = $state<TagDef | null>(null);
	let error = $state('');
	let loading = $state(true);
	// Vocabulary editor state: local list while editing, PATCHed as a whole
	// list on save (full-list semantics, same as recording tags PATCH).
	let words = $state<string[]>([]);
	let newWord = $state('');
	let saving = $state(false);
	let saveError = $state('');
	let savedAt = $state('');
	let confirmingDelete = $state(false);
	let deleteError = $state('');

	async function refresh(): Promise<void> {
		try {
			def = await fetchTagDef(loadApiConfig(), tag);
			words = [...def.vocabulary];
			error = '';
		} catch (caught) {
			error = String(caught);
		} finally {
			loading = false;
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
	}

	function removeWord(index: number): void {
		words = words.filter((_, i) => i !== index);
	}

	async function save(): Promise<void> {
		if (saving) return;
		saving = true;
		saveError = '';
		try {
			def = await updateTagVocabulary(loadApiConfig(), tag, words);
			words = [...def.vocabulary];
			savedAt = new Date().toLocaleTimeString();
		} catch (caught) {
			saveError = String(caught);
		} finally {
			saving = false;
		}
	}

	async function remove(): Promise<void> {
		try {
			await deleteTagDef(loadApiConfig(), tag);
			history.back();
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

<svelte:head><title>{tag} · Transcriptor Maximus</title></svelte:head>

<section class="page tagdef-page">
	<BackButton href="/tags" label="Tags" />

	{#if loading}
		<div class="tagdef-loading">Loading…</div>
	{:else if error}
		<EmptyState icon="tags" title="Tag not found" hint={error} />
	{:else if def}
		<header class="tagdef-head">
			<h2>{def.name}</h2>
			<div class="tagdef-meta">
				<span>{def.recordings} recordings</span>
				{#if def.recordings > 0}
					<a href="/vault/{encodeURIComponent(def.name)}">Open in Vault →</a>
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

			<div class="vocab-actions">
				<button class="vocab-save" type="button" disabled={saving} onclick={() => void save()}>
					{saving ? 'Saving…' : 'Save vocabulary'}
				</button>
				{#if savedAt}
					<span class="vocab-saved">Saved {savedAt}</span>
				{/if}
				{#if saveError}
					<span class="vocab-error" role="alert">{saveError}</span>
				{/if}
			</div>
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
</section>

<style>
	.tagdef-page { display: flex; flex-direction: column; gap: 16px; }
	.tagdef-loading { color: #8b8278; font-size: 12px; }
	.tagdef-head { display: grid; gap: 6px; }
	.tagdef-head h2 { margin: 0; color: var(--bone); font-size: 16px; font-weight: 700; letter-spacing: 0.02em; overflow-wrap: anywhere; }
	.tagdef-meta { display: flex; gap: 12px; align-items: baseline; font-size: 11px; color: #8b8278; }
	.tagdef-meta a { color: var(--brass); font-weight: 650; text-decoration: none; }
	.tagdef-meta a:hover { color: var(--bone); }

	.vocab-section { display: flex; flex-direction: column; gap: 10px; }
	.vocab-heading { display: grid; gap: 2px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
	.vocab-heading strong { color: #b5aa9c; font-size: 12px; }
	.vocab-hint { color: #746d64; font-size: 10px; }

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
	.vocab-save { min-height: 42px; }
	.vocab-saved { color: var(--brass); font-size: 10px; font-weight: 650; }
	.vocab-error { color: var(--red); font-size: 11px; }

	.vocab-add button { border: 1px solid var(--brass); background: rgba(215, 167, 71, 0.12); color: var(--brass); border-radius: 3px; padding: 0 14px; font-size: 12px; font-weight: 700; cursor: pointer; }
	.vocab-add button:hover:not(:disabled) { color: var(--bone); border-color: var(--bone); }
	.vocab-add button:disabled { cursor: not-allowed; }
	.vocab-save { border: 1px solid var(--brass); background: rgba(215, 167, 71, 0.12); color: var(--brass); border-radius: 3px; padding: 0 14px; font-size: 12px; font-weight: 700; cursor: pointer; }
	.vocab-save:hover:not(:disabled) { color: var(--bone); border-color: var(--bone); }
	.vocab-save:disabled { cursor: not-allowed; }
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
