<script lang="ts">
	import { onMount } from 'svelte';
	import { loadApiConfig, uploadDirect } from '$lib/api.svelte';
	import Icon from '$lib/Icon.svelte';
	import TagChips from '$lib/TagChips.svelte';
	import { mergeDraftTags } from '$lib/tags';
	import { ensureProfiles, profilesCache } from '$lib/profiles.svelte';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';

	const IMPORT_ACCEPT = '.flac,.wav,.mp3,audio/flac,audio/wav,audio/x-wav,audio/mpeg';
	const IMPORT_SIZE_HINT = 500 * 1024 * 1024;

	let importInput = $state<HTMLInputElement | null>(null);
	/** Non-null while the meta form is open for a picked file. */
	let importFile = $state<File | null>(null);
	let importTitle = $state('');
	/** datetime-local value (local time, minutes precision). */
	let importWhen = $state('');
	let importType = $state<string | null>(null);
	let importTagDraft = $state('');
	let importTags = $state<string[]>([]);
	let importing = $state(false);
	let importError = $state('');
	/** Last successful import's short id — one-line receipt under the picker. */
	let queuedId = $state('');
	const importOversize = $derived(importFile !== null && importFile.size > IMPORT_SIZE_HINT);
	const tagSuggestions = $derived(tagSuggestionsCache.items);
	const matchedProfile = $derived(
		importType === null
			? null
			: (profilesCache.items.find((profile) => profile.type === importType) ?? null)
	);

	function openImportPicker(): void {
		importInput?.click();
	}

	function onImportPicked(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		input.value = '';
		if (!file) return;
		importFile = file;
		// Default the meta form from the file: name without extension as a
		// title seed, "now" as the recorded time. The user edits before
		// confirming.
		importTitle = file.name.replace(/\.[^.]+$/, '');
		const now = new Date();
		now.setSeconds(0, 0);
		importWhen = toLocalDatetimeValue(now);
		importType = null;
		importTagDraft = '';
		importTags = [];
		importError = '';
		queuedId = '';
	}

	function cancelImport(): void {
		importFile = null;
		importError = '';
	}

	function toLocalDatetimeValue(date: Date): string {
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
	}

	async function confirmImport(): Promise<void> {
		const file = importFile;
		if (!file || importing) return;
		importing = true;
		importError = '';
		const formTags = mergeDraftTags(importTags, importTagDraft);
		// datetime-local carries no timezone: interpret it in the user's
		// local zone, exactly like a native picker would.
		const when = importWhen ? new Date(importWhen) : null;
		try {
			const result = await uploadDirect(loadApiConfig(), file, importTitle.trim() || null, formTags, null, {
				type: importType ?? undefined,
				recordedAt: when !== null && !Number.isNaN(when.getTime()) ? when.toISOString() : undefined
			});
			importFile = null;
			queuedId = result.id.slice(0, 8);
		} catch (error) {
			importError = String(error instanceof Error ? error.message : error);
		} finally {
			importing = false;
		}
	}

	onMount(() => {
		// Type selector source + freehand-tag suggestions (GET /tags with a
		// recordings fallback); failures leave free-form entry working.
		void ensureProfiles(loadApiConfig());
		void ensureTagSuggestions(loadApiConfig());
	});
</script>

<svelte:head><title>Import · Transcriptor Maximus</title></svelte:head>

<section class="page import-page">
	<header>
		<h1 class="page-title">Import audio</h1>
	</header>

	{#if !importFile}
		<button class="import-picker panel" type="button" onclick={openImportPicker}>
			<span class="picker-icon" aria-hidden="true"><Icon name="import" size={22} strokeWidth={1.5} /></span>
			<strong>Choose audio file</strong>
			<small>FLAC, WAV or MP3 — transcoded to FLAC on the server</small>
		</button>
		{#if queuedId}
			<p class="import-receipt">Import queued for processing ({queuedId}…)</p>
		{/if}
	{/if}

	{#if importFile}
		<div class="import-sheet panel">
			<header class="import-head">
				<Icon name="import" size={16} strokeWidth={1.5} />
				<span class="import-filename" title={importFile.name}>{importFile.name}</span>
				<span class="import-size">{(importFile.size / 1_000_000).toFixed(1)} MB</span>
			</header>
			<label class="title-field">
				<span class="field-label">Recording name</span>
				<input type="text" placeholder="e.g. Doctronic weekly" bind:value={importTitle} />
			</label>
			<label class="type-field">
				<span class="field-label">Type</span>
				<select bind:value={importType}>
					<option value={null}>None — default pipeline</option>
					{#each profilesCache.items as profile (profile.id)}
						<option value={profile.type}>{profile.display_name}</option>
					{/each}
				</select>
				{#if matchedProfile}
					<small class="type-hint">Profile: {matchedProfile.display_name}{matchedProfile.has_enrich ? ' · memory extraction on' : ''}</small>
				{/if}
			</label>
			<label class="title-field">
				<span class="field-label">Recorded at</span>
				<input type="datetime-local" bind:value={importWhen} />
			</label>
			<label class="tags-field">
				<span class="field-label">Tags <small>Press Enter or comma to add</small></span>
				<TagChips
					tags={importTags}
					bind:draft={importTagDraft}
					suggestions={tagSuggestions}
					placeholder="e.g. doctronic, personal"
					onChange={(next) => (importTags = next)}
				/>
			</label>
			{#if importOversize}
				<p class="import-hint">Large file — converting to FLAC or MP3 first makes the upload noticeably faster.</p>
			{/if}
			{#if importError}
				<p class="inline-error" role="alert">{importError}</p>
			{/if}
			<footer class="import-actions">
				<button type="button" class="secondary" onclick={cancelImport} disabled={importing}>Cancel</button>
				<button type="button" class="primary" onclick={() => void confirmImport()} disabled={importing}>
					{importing ? 'Uploading…' : 'Import'}
				</button>
			</footer>
		</div>
	{/if}

	<input
		type="file"
		accept={IMPORT_ACCEPT}
		class="visually-hidden"
		bind:this={importInput}
		onchange={onImportPicked}
		aria-hidden="true"
		tabindex={-1}
	/>
</section>

<style>
	.import-page { display: flex; flex-direction: column; gap: 10px; }
	/* The picker is a control whose outline is the affordance, so the panel
	   treatment is legitimate here; the meta sheet below stays a quiet form. */
	.import-picker { display: grid; justify-items: center; gap: 6px; padding: 28px 16px; border-color: var(--line); background: transparent; color: var(--brass); font-size: 12px; cursor: pointer; transition: border-color 120ms ease, background 120ms ease; }
	.import-picker:hover { border-color: rgba(215, 167, 71, 0.4); background: rgba(215, 167, 71, 0.06); }
	.picker-icon { display: grid; place-items: center; line-height: 0; }
	.import-picker small { color: var(--ash); font-size: 10px; }
	.import-receipt { margin: 0; color: var(--ash); font-size: 11px; }
	.import-sheet { display: flex; flex-direction: column; gap: 10px; padding: 12px; }
	.import-head { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 8px; color: var(--brass); }
	.import-filename { font-size: 12px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--bone); }
	.import-size { font-size: 10px; color: #8d847a; font-variant-numeric: tabular-nums; }
	.type-field { display: block; margin-bottom: 0; }
	.type-field select { width: 100%; padding: 7px 8px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0, 0, 0, 0.25); color: var(--bone); font-size: 12px; }
	.type-hint { display: block; margin-top: 4px; font-size: 10px; color: #8d847a; }
	.title-field { display: block; margin-bottom: 0; }
	.tags-field { display: block; margin-bottom: 0; }
	.tags-field .field-label small { margin-left: 6px; color: #6f685f; font-weight: 500; }
	.import-hint { font-size: 10px; color: var(--brass); }
	.inline-error { color: var(--red); font-size: 12px; }
	.import-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 2px; }
	.import-actions button { min-height: 36px; border-radius: 3px; font-size: 12px; font-weight: 700; cursor: pointer; }
	.import-actions .secondary { border: 1px solid var(--line); background: transparent; color: #8e857b; }
	.import-actions .secondary:hover:not(:disabled) { color: var(--bone); border-color: rgba(215, 167, 71, 0.4); }
	.import-actions .primary { border: 1px solid var(--brass); background: rgba(215, 167, 71, 0.12); color: var(--brass); }
	.import-actions .primary:hover:not(:disabled) { background: rgba(215, 167, 71, 0.2); }
	.import-actions button:disabled { opacity: 0.6; cursor: default; }
	.import-sheet :global(.field-label) { display: block; margin-bottom: 4px; }
	.visually-hidden { position: absolute; width: 1px; height: 1px; margin: -1px; clip: rect(0 0 0 0); clip-path: inset(50%); overflow: hidden; white-space: nowrap; }
</style>
