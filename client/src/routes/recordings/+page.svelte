<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page as appPage } from '$app/state';
	import Icon from '$lib/Icon.svelte';
	import EmptyState from '$lib/EmptyState.svelte';
	import Skeleton from '$lib/Skeleton.svelte';
	import TagChips from '$lib/TagChips.svelte';
	import TypeProfileField from '$lib/TypeProfileField.svelte';
	import { mergeDraftTags } from '$lib/tags';
	import { ensureProfiles } from '$lib/profiles.svelte';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';
	import {
		discardDesktopUpload,
		discardMobileUpload,
		mobilePendingUploads,
		refreshPendingUploads,
		retryDesktopUpload,
		retryMobileUpload,
		retryPendingUploads,
		uploads
	} from '$lib/stores.svelte';
	import { listRecordings, loadApiConfig, uploadDirect, type Recording } from '$lib/api.svelte';
	import { dateLabel, durationLabel } from '$lib/format';

	const PAGE_SIZE = 20;
	const SEARCH_DEBOUNCE_MS = 300;

	let recordings = $state<Recording[]>([]);
	let total = $state(0);
	let page = $state(0);
	let error = $state('');
	let loading = $state(true);
	let query = $state('');
	let filter = $state<'all' | Recording['state']>('all');
	// The poll and page turns must refresh from the APPLIED filters, not the
	// live-bound ones: the 3s tick does not wait for the search debounce and
	// would otherwise fetch with partial text (transient empty state, and
	// page-clamp jumps when the partial query shrinks the result set).
	let appliedQuery = '';
	let appliedFilter: 'all' | Recording['state'] = 'all';
	let pollTimer: ReturnType<typeof globalThis.setInterval> | null = null;
	let searchTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
	// Monotonic request id: a stale response (poll racing a page turn or a
	// debounced search) must not overwrite newer state.
	let refreshSeq = 0;

	// ── Inline import (absorbed from the retired /import page) ────────────────
	const IMPORT_ACCEPT = '.flac,.wav,.mp3,audio/flac,audio/wav,audio/x-wav,audio/mpeg';
	const IMPORT_SIZE_HINT = 500 * 1024 * 1024;

	let importInput = $state<HTMLInputElement | null>(null);
	/** Non-null while the meta form is open for a picked file. */
	let importFile = $state<File | null>(null);
	let importTitle = $state('');
	let importType = $state<string | null>(null);
	let importTagDraft = $state('');
	let importTags = $state<string[]>([]);
	let dragOver = $state(false);
	let importing = $state(false);
	let importError = $state('');
	let tagSuggestions = $derived(tagSuggestionsCache.items);
	const importOversize = $derived(importFile !== null && importFile.size > IMPORT_SIZE_HINT);

	// Open state lives in the URL (`?import=1`, mascot menu deep link) so
	// the panel is reachable from anywhere; goto() keeps it back/refresh
	// safe. ensure* feed the type selector and tag suggestions; both cache.
	const importOpen = $derived(appPage.url.searchParams.get('import') === '1');
	$effect(() => {
		if (importOpen) {
			void ensureProfiles(loadApiConfig());
			void ensureTagSuggestions(loadApiConfig());
		}
	});

	function setImportOpen(open: boolean): void {
		if (!open) resetImportForm();
		void goto(open ? '/recordings?import=1' : '/recordings', {
			replaceState: true,
			keepFocus: true,
			noScroll: true
		});
	}

	function resetImportForm(): void {
		importFile = null;
		importTitle = '';
		importType = null;
		importTagDraft = '';
		importTags = [];
		importError = '';
	}

	function openImportPicker(): void {
		importInput?.click();
	}

	/** Seed the meta form from the file: name without extension as a title
	 * stem; the user edits before confirming. */
	function acceptImportFile(file: File): void {
		importFile = file;
		importTitle = file.name.replace(/\.[^.]+$/, '');
		importType = null;
		importTagDraft = '';
		importTags = [];
		importError = '';
	}

	function onImportPicked(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		input.value = '';
		if (file) acceptImportFile(file);
	}

	function onImportDragOver(event: DragEvent): void {
		event.preventDefault();
		dragOver = true;
	}

	function onImportDrop(event: DragEvent): void {
		event.preventDefault();
		dragOver = false;
		const file = event.dataTransfer?.files?.[0];
		if (file) acceptImportFile(file);
	}

	async function confirmImport(): Promise<void> {
		const file = importFile;
		if (!file || importing) return;
		importing = true;
		importError = '';
		const formTags = mergeDraftTags(importTags, importTagDraft);
		try {
			await uploadDirect(loadApiConfig(), file, importTitle.trim() || null, formTags, null, {
				type: importType ?? undefined
			});
			resetImportForm();
			setImportOpen(false);
			// The new capture enters the poll below within one tick.
			await refresh();
		} catch (error) {
			importError = String(error instanceof Error ? error.message : error);
		} finally {
			importing = false;
		}
	}

	// ── Local outbox (desktop spool + Android takes not yet on the server) ────
	type OutboxRow = {
		key: string;
		title: string;
		tags: string[];
		durationSec: number | null;
		status: 'queued' | 'uploading' | 'failed';
		statusText: string;
		error: string;
		canRetry: boolean;
		onRetry: () => void;
		onDelete: () => void;
	};

	const desktopOutbox = $derived(Object.values(uploads).filter((u) => u.state !== 'done'));
	const localCount = $derived(desktopOutbox.length + mobilePendingUploads.length);
	const outboxRows = $derived.by(() => {
		const rows: OutboxRow[] = [];
		for (const u of desktopOutbox) {
			if (u.state === 'done') continue;
			const status = u.state;
			rows.push({
				key: `desktop-${u.sessionId}`,
				title: u.title || u.sessionId,
				tags: u.tags,
				durationSec: u.durationSec,
				status,
				statusText:
					status === 'uploading'
						? u.total > 0
							? `Uploading ${Math.round((u.committed / u.total) * 100)}%`
							: 'Uploading…'
						: status === 'failed'
							? 'Failed'
							: 'Pending',
				error: u.error ?? '',
				canRetry: status !== 'uploading',
				onRetry: () => void retryDesktopUpload(u.sessionId),
				onDelete: () => void discardDesktopUpload(u.sessionId)
			});
		}
		for (const m of mobilePendingUploads) {
			const status = m.status === 'failed' ? 'failed' : 'uploading';
			rows.push({
				key: `mobile-${m.id}`,
				title: m.title || m.id,
				tags: m.tags,
				durationSec: m.durationSec,
				status,
				statusText: status === 'uploading' ? 'Uploading…' : 'Failed',
				error: m.error,
				canRetry: status !== 'uploading',
				onRetry: () => void retryMobileUpload(m.id),
				onDelete: () => discardMobileUpload(m.id)
			});
		}
		return rows;
	});
	// Rows a "Retry all" would act on: anything not actively uploading.
	const retryableCount = $derived(outboxRows.filter((r) => r.status !== 'uploading').length);

	function retryAllOutbox(): void {
		for (const row of outboxRows) {
			if (row.status !== 'uploading') row.onRetry();
		}
	}

	const pageCount = $derived(Math.max(1, Math.ceil(total / PAGE_SIZE)));

	async function refresh(): Promise<void> {
		const seq = ++refreshSeq;
		try {
			const result = await listRecordings(loadApiConfig(), {
				limit: PAGE_SIZE,
				offset: page * PAGE_SIZE,
				q: appliedQuery,
				state: appliedFilter
			});
			if (seq !== refreshSeq) return;
			// Deletes or rows shifting under the poll can leave the current
			// page out of range — clamp and refetch once.
			if (result.items.length === 0 && result.total > 0 && page > 0) {
				page = Math.min(page, Math.ceil(result.total / PAGE_SIZE) - 1);
				return refresh();
			}
			recordings = result.items;
			total = result.total;
			error = '';
		} catch (caught) {
			if (seq !== refreshSeq) return;
			error = String(caught);
		} finally {
			if (seq === refreshSeq) loading = false;
		}
	}

	function refilter(): void {
		appliedQuery = query;
		appliedFilter = filter;
		page = 0;
		recordings = [];
		total = 0;
		loading = true;
		void refresh();
	}

	function queryChanged(): void {
		if (searchTimer) globalThis.clearTimeout(searchTimer);
		searchTimer = globalThis.setTimeout(refilter, SEARCH_DEBOUNCE_MS);
	}

	function gotoPage(next: number): void {
		if (next < 0 || next >= pageCount) return;
		page = next;
		void refresh();
	}

	onMount(() => {
		refresh();
		// Enqueue every pending spool session (existing behavior) and pull
		// fresh title/tags/duration for the outbox rows below.
		retryPendingUploads().catch(() => {});
		void refreshPendingUploads();
		pollTimer = globalThis.setInterval(refresh, 3000);
		return () => {
			if (pollTimer) globalThis.clearInterval(pollTimer);
			if (searchTimer) globalThis.clearTimeout(searchTimer);
		};
	});

</script>

<svelte:head><title>Archive · Transcriptor Maximus</title></svelte:head>

<section class="page archive-page">
	<div class="archive-tools">
		<label>
			<span class="sr-only">Search recordings</span>
			<input type="search" placeholder="Search recordings" maxlength="200" bind:value={query} oninput={queryChanged} />
		</label>
		<label>
			<span class="sr-only">Filter by state</span>
			<select bind:value={filter} onchange={refilter}>
				<option value="all">All states</option>
				<option value="uploading">Uploading</option>
				<option value="processing">Processing</option>
				<option value="done">Complete</option>
				<option value="failed">Failed</option>
			</select>
		</label>
		<button class="import-toggle" type="button" aria-pressed={importOpen} onclick={() => setImportOpen(!importOpen)}>
			<Icon name="import" size={14} />
			Import
		</button>
	</div>

	{#if importOpen}
		<div class="import-panel panel">
			{#if !importFile}
				<button
					class="import-picker"
					class:drag-over={dragOver}
					type="button"
					onclick={openImportPicker}
					ondragover={onImportDragOver}
					ondragleave={() => (dragOver = false)}
					ondrop={onImportDrop}
				>
					<span class="picker-icon" aria-hidden="true"><Icon name="import" size={22} strokeWidth={1.5} /></span>
					<strong>Choose audio file</strong>
					<small>FLAC, WAV or MP3 — or drop it here</small>
				</button>
			{:else}
				<header class="import-head">
					<Icon name="import" size={16} strokeWidth={1.5} />
					<span class="import-filename" title={importFile.name}>{importFile.name}</span>
					<span class="import-size">{(importFile.size / 1_000_000).toFixed(1)} MB</span>
				</header>
				<label class="import-field">
					<span class="field-label">Recording name</span>
					<input type="text" placeholder="e.g. Doctronic weekly" bind:value={importTitle} />
				</label>
				<TypeProfileField bind:value={importType} />
				<label class="import-field">
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
					<p class="import-error" role="alert">{importError}</p>
				{/if}
				<div class="import-actions">
					<button class="secondary" type="button" onclick={() => (importFile = null)} disabled={importing}>Back</button>
					<button class="primary" type="button" onclick={confirmImport} disabled={importing}>{importing ? 'Uploading…' : 'Upload & Process'}</button>
				</div>
			{/if}
			<input hidden type="file" accept={IMPORT_ACCEPT} bind:this={importInput} onchange={onImportPicked} />
		</div>
	{/if}

	{#if error}
		<div class="archive-error" role="alert"><strong>Archive unavailable</strong><span>{error}</span></div>
	{/if}

	{#if localCount > 0}
		<section class="spool-section" aria-label="Local pending uploads">
			<header class="spool-head">
				<h2>Local outbox ({localCount})</h2>
				{#if retryableCount > 0}
					<button class="spool-retry-all" type="button" onclick={retryAllOutbox}>Retry all</button>
				{/if}
			</header>
			<ul class="spool-list">
				{#each outboxRows as row (row.key)}
					<li class="spool-row">
						<span class="state-mark {row.status}" aria-hidden="true"></span>
						<div class="spool-name">
							<strong>{row.title}</strong>
							<small class="spool-meta">
								{#if row.durationSec !== null}{durationLabel(row.durationSec)} · {/if}
								<span class="state-label {row.status}">{row.statusText}</span>
							</small>
							{#if row.error}
								<span class="spool-error" title={row.error}>{row.error}</span>
							{/if}
							{#if row.tags.length > 0}
								<span class="record-tags">
									{#each row.tags.slice(0, 3) as tag (tag)}<span class="record-tag">{tag}</span>{/each}
									{#if row.tags.length > 3}<span class="record-tag-overflow">+{row.tags.length - 3}</span>{/if}
								</span>
							{/if}
						</div>
						<div class="spool-actions">
							<button type="button" disabled={!row.canRetry} onclick={row.onRetry}>Retry</button>
							<button class="danger" type="button" disabled={row.status === 'uploading'} onclick={row.onDelete}>Delete</button>
						</div>
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	<div class="record-list" aria-live="polite">
		{#if loading}
			<Skeleton variant="record" count={3} />
		{:else}
			{#each recordings as recording (recording.id)}
				<article class="record-card">
					<button class="list-row record-heading" type="button" onclick={() => goto(`/recordings/${recording.id}`)}>
						<span class={`state-mark ${recording.state}`} aria-hidden="true"></span>
						<span class="record-name"><strong>{recording.title || 'Untitled capture'}</strong><small>{dateLabel(recording.created_at)} · {durationLabel(recording.duration_sec)}</small>{#if recording.tags.length > 0}<span class="record-tags">{#each recording.tags.slice(0, 3) as tag (tag)}<span class="record-tag">{tag}</span>{/each}{#if recording.tags.length > 3}<span class="record-tag-overflow">+{recording.tags.length - 3}</span>{/if}</span>{/if}</span>
						<span class={`state-label ${recording.state}`}>{recording.state}</span>
						<span class="record-chevron"><Icon name="collapse" size={14} /></span>
					</button>
				</article>
			{:else}
				<EmptyState icon="empty" title="No matching captures" hint="New recordings appear here after upload begins." tone="red" />
			{/each}
		{/if}
	</div>

	{#if total > PAGE_SIZE}
		<nav class="pager" aria-label="Recordings pages">
			<button class="pager-button" type="button" disabled={page === 0} onclick={() => gotoPage(page - 1)} aria-label="Previous page">
				<Icon name="back" size={15} />
			</button>
			<span class="pager-status">Page {page + 1} of {pageCount} · {total} capture{total === 1 ? '' : 's'}</span>
			<button class="pager-button" type="button" disabled={page >= pageCount - 1} onclick={() => gotoPage(page + 1)} aria-label="Next page">
				<span class="pager-flip"><Icon name="back" size={15} /></span>
			</button>
		</nav>
	{/if}
</section>

<style>
	.archive-page { display: flex; flex-direction: column; gap: 14px; }
	.archive-tools { display: grid; grid-template-columns: 1fr 116px auto; gap: 7px; align-items: stretch; }
	.archive-tools label { display: grid; }
	.archive-tools input, .archive-tools select { height: 42px; }
	.import-toggle { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 42px; padding: 0 12px; border: 1px solid rgba(215, 167, 71, 0.32); border-radius: 3px; background: rgba(215, 167, 71, 0.07); color: var(--brass); font-size: 12px; font-weight: 700; cursor: pointer; transition: border-color 120ms ease, background 120ms ease; }
	.import-toggle:hover, .import-toggle[aria-pressed='true'] { border-color: var(--brass); background: rgba(215, 167, 71, 0.12); }
	/* The import bay is a detachable surface with its own affordance, so the
	   panel perimeter (.panel) is legitimate; the form inside stays quiet. */
	.import-panel { display: flex; flex-direction: column; gap: 10px; padding: 12px; }
	.import-picker { display: grid; justify-items: center; gap: 6px; padding: 24px 16px; border: 1px dashed rgba(215, 167, 71, 0.32); border-radius: 3px; background: transparent; color: var(--brass); font-size: 12px; cursor: pointer; transition: border-color 120ms ease, background 120ms ease; }
	.import-picker:hover, .import-picker.drag-over { border-color: rgba(215, 167, 71, 0.5); background: rgba(215, 167, 71, 0.06); }
	.picker-icon { display: grid; place-items: center; line-height: 0; }
	.import-picker small { color: var(--ash); font-size: 10px; }
	.import-head { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 8px; color: var(--brass); }
	.import-filename { font-size: 12px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--bone); }
	.import-size { font-size: 10px; color: #8d847a; font-variant-numeric: tabular-nums; }
	.import-field { display: block; }
	.import-field .field-label small { margin-left: 6px; color: #6f685f; font-weight: 500; }
	.import-hint { margin: 0; font-size: 10px; color: var(--brass); }
	.import-error { margin: 0; color: var(--red); font-size: 12px; }
	.import-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
	.import-actions button { min-height: 36px; border-radius: 3px; font-size: 12px; font-weight: 700; cursor: pointer; }
	.import-actions .secondary { border: 1px solid var(--line); background: transparent; color: #8e857b; }
	.import-actions .secondary:hover:not(:disabled) { color: var(--bone); border-color: rgba(215, 167, 71, 0.4); }
	.import-actions .primary { border: 1px solid var(--brass); background: rgba(215, 167, 71, 0.12); color: var(--brass); }
	.import-actions .primary:hover:not(:disabled) { background: rgba(215, 167, 71, 0.2); }
	.import-actions button:disabled { opacity: 0.6; cursor: default; }
	/* Local outbox: a ruled manifest on the plate — seams, not cards. */
	.spool-section { display: grid; }
	.spool-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 2px 6px; border-bottom: 1px solid var(--line); }
	.spool-head h2 { margin: 0; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #8d847a; }
	.spool-retry-all { min-height: 26px; padding: 0 10px; border: 1px solid rgba(215, 167, 71, 0.32); border-radius: 3px; background: rgba(215, 167, 71, 0.07); color: var(--brass); font-size: 11px; font-weight: 700; cursor: pointer; }
	.spool-retry-all:hover { border-color: var(--brass); background: rgba(215, 167, 71, 0.12); }
	.spool-list { display: grid; margin: 0; padding: 0; list-style: none; }
	.spool-row { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; padding: 9px 2px; border-bottom: 1px solid var(--line); }
	.spool-row:last-child { border-bottom: 0; }
	.spool-name { min-width: 0; }
	.spool-name strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: #ded3c4; }
	.spool-meta { display: block; margin-top: 3px; font-size: 10px; color: #8b8278; }
	.spool-error { display: block; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #f36b60; font-size: 10px; }
	.spool-actions { display: grid; grid-template-columns: auto auto; gap: 6px; }
	.spool-actions button { min-height: 28px; padding: 0 10px; border: 1px solid var(--line); border-radius: 3px; background: transparent; color: #b8ac9d; font-size: 11px; font-weight: 700; cursor: pointer; }
	.spool-actions button:hover:not(:disabled) { color: var(--bone); border-color: rgba(215, 167, 71, 0.4); }
	.spool-actions button:disabled { opacity: 0.45; cursor: default; }
	.spool-actions .danger { color: #f36b60; border-color: rgba(213, 45, 36, 0.35); }
	.spool-actions .danger:hover:not(:disabled) { color: var(--red); border-color: rgba(213, 45, 36, 0.6); }
	.archive-tools input[type='search'] { -webkit-appearance: none; appearance: none; }
	.archive-tools input[type='search']::-webkit-search-cancel-button { -webkit-appearance: none; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
	.archive-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.archive-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.archive-error span { color: #c6baaa; }
	.record-list { display: grid; }
	.record-card { transition: background 120ms ease; }
	.record-card:not(:last-child) { border-bottom: 1px solid var(--line); }
	.record-heading { grid-template-columns: auto 1fr auto auto; }
	.state-mark { width: 7px; height: 7px; border-radius: 50%; background: #706960; box-shadow: 0 0 0 3px rgba(112,105,96,.12); }
	.state-mark.done { background: var(--cyan); box-shadow: 0 0 9px rgba(112,215,208,.55); }
	.state-mark.processing, .state-mark.uploading { background: var(--brass); box-shadow: 0 0 9px rgba(215,167,71,.5); }
	.state-mark.failed { background: var(--red); box-shadow: 0 0 9px rgba(213,45,36,.6); }
	.record-name { min-width: 0; }
	.record-name strong, .record-name small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.record-name strong { font-size: 13px; color: #ded3c4; }
	.record-name small { margin-top: 4px; font-size: 10px; color: #8b8278; }
	.state-label { color: #968d83; font-size: 9px; font-weight: 700; text-transform: capitalize; }
	.state-label.done { color: var(--cyan); }
	.state-label.processing, .state-label.uploading { color: var(--brass); }
	.state-label.failed { color: #f36b60; }
	.record-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
	.record-tag { padding: 1px 6px; border-radius: 2px; background: rgba(215,167,71,.08); color: var(--brass); font-size: 9px; font-weight: 650; line-height: 1.4; }
	.record-tag-overflow { padding: 1px 6px; border-radius: 2px; background: rgba(255,255,255,.04); color: #8b8278; font-size: 9px; font-weight: 650; line-height: 1.4; }
	.record-heading:hover .record-chevron { color: var(--brass); }
	@media (prefers-reduced-motion: reduce) { .record-chevron { transition: none; } }
	.pager { display: grid; grid-template-columns: 34px 1fr 34px; align-items: center; gap: 8px; }
	.pager-button { height: 34px; display: grid; place-items: center; border: 1px solid rgba(215,167,71,.32); border-radius: 3px; background: rgba(215,167,71,.07); color: var(--brass); cursor: pointer; line-height: 0; }
	.pager-button:hover:not(:disabled) { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.pager-button:disabled { opacity: 0.4; cursor: default; }
	.pager-status { text-align: center; font-size: 10px; color: #8d847a; font-variant-numeric: tabular-nums; }
	.pager-flip { display: grid; place-items: center; transform: scaleX(-1); line-height: 0; }
</style>
