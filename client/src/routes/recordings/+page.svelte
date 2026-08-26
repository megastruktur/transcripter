<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import Icon from '$lib/Icon.svelte';
	import { retryPendingUploads } from '$lib/stores.svelte';
	import { listRecordings, loadApiConfig, type Recording } from '$lib/api.svelte';
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
		retryPendingUploads().catch(() => {});
		pollTimer = globalThis.setInterval(refresh, 3000);
		return () => {
			if (pollTimer) globalThis.clearInterval(pollTimer);
			if (searchTimer) globalThis.clearTimeout(searchTimer);
		};
	});

</script>

<svelte:head><title>Archive · Transcriptor Maximus</title></svelte:head>

<section class="page archive-page">
	<header>
		<h1 class="page-title">Recordings</h1>
	</header>

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
	</div>

	{#if error}
		<div class="archive-error" role="alert"><strong>Archive unavailable</strong><span>{error}</span></div>
	{/if}

	<div class="record-list" aria-live="polite">
		{#if loading}
			{#each [0, 1, 2] as skeletonIndex (skeletonIndex)}
				<div class="record-card panel skeleton-card" aria-hidden="true">
					<span class="skeleton-bar skeleton-mark"></span>
					<span class="skeleton-lines">
						<span class="skeleton-bar skeleton-title"></span>
						<span class="skeleton-bar skeleton-meta"></span>
					</span>
					<span class="skeleton-bar skeleton-label"></span>
				</div>
			{/each}
		{:else}
			{#each recordings as recording (recording.id)}
				<article class="record-card panel">
					<button class="record-heading" type="button" onclick={() => goto(`/recordings/${recording.id}`)}>
						<span class={`state-mark ${recording.state}`} aria-hidden="true"></span>
						<span class="record-name"><strong>{recording.title || 'Untitled capture'}</strong><small>{dateLabel(recording.created_at)} · {durationLabel(recording.duration_sec)}</small></span>
						<span class={`state-label ${recording.state}`}>{recording.state}</span>
						<span class="record-chevron"><Icon name="collapse" size={14} /></span>
					</button>
				</article>
			{:else}
				<div class="empty panel"><span class="empty-icon" aria-hidden="true"><Icon name="empty" size={30} /></span><strong>No matching captures</strong><small>New recordings appear here after upload begins.</small></div>
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
	.archive-tools { display: grid; grid-template-columns: 1fr 116px; gap: 7px; align-items: stretch; }
	.archive-tools label { display: grid; }
	.archive-tools input, .archive-tools select { height: 42px; }
	.archive-tools input[type='search'] { -webkit-appearance: none; appearance: none; }
	.archive-tools input[type='search']::-webkit-search-cancel-button { -webkit-appearance: none; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
	.archive-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.archive-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.archive-error span { color: #c6baaa; }
	.record-list { display: grid; gap: 8px; }
	.record-card { overflow: hidden; transition: border-color 120ms ease, background 120ms ease; }
	.record-heading { width: 100%; display: grid; grid-template-columns: auto 1fr auto auto; align-items: center; gap: 9px; padding: 11px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
	.record-heading:hover { background: rgba(255,255,255,.02); }
	.state-mark { width: 7px; height: 7px; border-radius: 50%; background: #706960; box-shadow: 0 0 0 3px rgba(112,105,96,.12); }
	.state-mark.done { background: var(--cyan); box-shadow: 0 0 9px rgba(112,215,208,.55); }
	.state-mark.processing, .state-mark.uploading { background: var(--brass); box-shadow: 0 0 9px rgba(215,167,71,.5); }
	.state-mark.failed { background: var(--red); box-shadow: 0 0 9px rgba(213,45,36,.6); }
	.record-name { min-width: 0; }
	.record-name strong, .record-name small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.record-name strong { font-size: 13px; color: #ded3c4; }
	.record-name small { margin-top: 4px; font-size: 10px; color: #8b8278; }
	.state-label { padding: 5px 7px; border: 1px solid var(--line); border-radius: 2px; color: #968d83; font-size: 9px; font-weight: 700; text-transform: capitalize; }
	.state-label.done { color: var(--cyan); border-color: rgba(112,215,208,.25); }
	.state-label.processing, .state-label.uploading { color: var(--brass); border-color: rgba(215,167,71,.25); }
	.state-label.failed { color: #f36b60; border-color: rgba(213,45,36,.35); }
	.record-chevron { display: grid; place-items: center; color: #6d665d; transform: rotate(-90deg); transition: color 120ms ease; line-height: 0; }
	.record-heading:hover .record-chevron { color: var(--brass); }
	@media (prefers-reduced-motion: reduce) { .record-chevron { transition: none; } }
	.empty { display: grid; justify-items: center; gap: 5px; padding: 28px 16px; color: #746d64; text-align: center; }
	.empty-icon { display: grid; place-items: center; color: var(--red); line-height: 0; }
	.empty strong { color: #b5aa9c; font-size: 13px; }
	.empty small { font-size: 11px; }
	.skeleton-card { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; padding: 11px; }
	.skeleton-lines { display: grid; gap: 6px; }
	.skeleton-bar { display: block; border-radius: 2px; background: var(--iron-raised); animation: skeleton-pulse 150ms ease-in-out infinite alternate; }
	.skeleton-mark { width: 7px; height: 7px; border-radius: 50%; }
	.skeleton-title { width: 62%; height: 11px; }
	.skeleton-meta { width: 38%; height: 8px; }
	.skeleton-label { width: 46px; height: 18px; }
	@keyframes skeleton-pulse { from { opacity: 0.55; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .skeleton-bar { animation: none; opacity: 0.75; } }
	.pager { display: grid; grid-template-columns: 34px 1fr 34px; align-items: center; gap: 8px; }
	.pager-button { height: 34px; display: grid; place-items: center; border: 1px solid rgba(215,167,71,.32); border-radius: 3px; background: rgba(215,167,71,.07); color: var(--brass); cursor: pointer; line-height: 0; }
	.pager-button:hover:not(:disabled) { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.pager-button:disabled { opacity: 0.4; cursor: default; }
	.pager-status { text-align: center; font-size: 10px; color: #8d847a; font-variant-numeric: tabular-nums; }
	.pager-flip { display: grid; place-items: center; transform: scaleX(-1); line-height: 0; }
</style>
