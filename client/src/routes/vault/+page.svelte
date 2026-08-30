<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import Icon from '$lib/Icon.svelte';
	import {
		fetchVault,
		fetchGlobalSearch,
		loadApiConfig,
		type VaultItem,
		type GlobalSearchResponse
	} from '$lib/api.svelte';
	import { dateLabel } from '$lib/format';
	import SearchRecess from '$lib/SearchRecess.svelte';
	import EmptyState from '$lib/EmptyState.svelte';
	import Skeleton from '$lib/Skeleton.svelte';

	let items = $state<VaultItem[]>([]);
	let loading = $state(true);
	let error = $state('');
	// Monotonic request id: a stale response racing an unmount must not
	// overwrite newer state.
	let fetchSeq = 0;

	// Phase 3.75 global search: cross-tag KNN over every per-tag index,
	// same server-side embedding as the tag page's search. A hit opens
	// the recording at its timestamp (?t= seconds — the detail page seeks).
	let searchQuery = $state('');
	let searchResults = $state<GlobalSearchResponse | null>(null);
	let searchLoading = $state(false);
	let searchError = $state('');
	let searchNote = $state('');
	let searchSeq = 0;

	async function runSearch(): Promise<void> {
		const q = searchQuery.trim();
		if (!q || searchLoading) return;
		const seq = ++searchSeq;
		searchLoading = true;
		searchError = '';
		searchNote = '';
		try {
			const result = await fetchGlobalSearch(loadApiConfig(), q);
			if (seq !== searchSeq) return;
			searchResults = result;
		} catch (caught) {
			if (seq !== searchSeq) return;
			searchResults = null;
			const err = caught as { status?: number; reason?: string; message?: string };
			if (err.status === 503) {
				searchNote = err.reason || 'Semantic search unavailable.';
			} else {
				searchError = `Search failed: ${err.message ?? String(caught)}`;
			}
		} finally {
			if (seq === searchSeq) searchLoading = false;
		}
	}

	function clearSearch(): void {
		searchQuery = '';
		searchResults = null;
		searchError = '';
		searchNote = '';
	}

	function openHit(recordingId: string, tsStart: number): void {
		void goto(`/recordings/${recordingId}?t=${Math.max(0, Math.floor(tsStart))}`);
	}

	// One lamp per digest state. Cyan is reserved for verified-ready, brass
	// for attention (stale), ash for absent — matching the status-lamp idiom
	// (grey unknown / brass waiting / cyan ready).
	const DIGEST_LAMP: Record<VaultItem['digest'], string> = {
		ready: 'ready',
		stale: 'stale',
		none: 'none'
	};

	async function refresh(): Promise<void> {
		const seq = ++fetchSeq;
		try {
			const result = await fetchVault(loadApiConfig());
			if (seq !== fetchSeq) return;
			items = result.items;
			error = '';
		} catch (caught) {
			if (seq !== fetchSeq) return;
			error = String(caught);
		} finally {
			if (seq === fetchSeq) loading = false;
		}
	}

	onMount(() => {
		refresh();
	});
</script>

<svelte:head><title>Vault · Transcriptor Maximus</title></svelte:head>

<section class="page vault-page">
	<SearchRecess
		ariaLabel="Semantic search · all tags"
		placeholder="Search all sessions…"
		showTag
		bind:query={searchQuery}
		loading={searchLoading}
		error={searchError}
		note={searchNote}
		results={searchResults}
		onsearch={runSearch}
		onclear={clearSearch}
		onopenhit={openHit}
	/>

	{#if error}
		<div class="vault-error" role="alert"><strong>Vault unavailable</strong><span>{error}</span></div>
	{/if}

	<div class="tag-list" aria-live="polite">
		{#if loading}
			<Skeleton variant="tag" count={3} />
		{:else}
			{#each items as item (item.tag)}
				<article class="tag-card">
					<button class="tag-heading" type="button" onclick={() => goto(`/vault/${encodeURIComponent(item.tag)}`)}>
						<span class={`digest-lamp ${DIGEST_LAMP[item.digest]}`} aria-hidden="true"></span>
						<span class="tag-name">
							<strong>{item.tag}</strong>
							<small>{item.sessions} session{item.sessions === 1 ? '' : 's'} · {item.entities} entit{item.entities === 1 ? 'y' : 'ies'}</small>
						</span>
						<span class="tag-side">
							<small class="tag-last">{dateLabel(item.last_activity)}</small>
							<span class={`digest-text ${item.digest}`}>{item.digest === 'ready' ? 'digest ready' : item.digest === 'stale' ? 'digest stale' : 'no digest'}</span>
						</span>
						<span class="tag-chevron"><Icon name="collapse" size={14} /></span>
					</button>
				</article>
			{:else}
				<EmptyState icon="vault" title="Vault is empty" hint="Tag recordings in the Library — each tag becomes a shelf here." />
			{/each}
		{/if}
	</div>
</section>

<style>
	.vault-page { display: flex; flex-direction: column; gap: 14px; }
	.vault-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.vault-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.vault-error span { color: #c6baaa; }
	.tag-list { display: grid; }
	.tag-card { transition: background 120ms ease; }
	.tag-card:not(:last-child) { border-bottom: 1px solid var(--line); }
	.tag-heading { width: 100%; display: grid; grid-template-columns: auto 1fr auto auto; align-items: center; gap: 9px; padding: 11px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
	.tag-heading:hover { background: rgba(255,255,255,.02); }
	/* Digest lamp: brass = fresh note, dim brass hollow = stale, ash = none.
	   Cyan stays reserved for verified state; the text label carries the
	   meaning so color is never the only signal. */
	.digest-lamp { width: 7px; height: 7px; border-radius: 50%; background: var(--brass); box-shadow: 0 0 9px rgba(215,167,71,.5); }
	.digest-lamp.stale { background: transparent; border: 1px solid rgba(215,167,71,.55); box-shadow: none; }
	.digest-lamp.none { background: #706960; box-shadow: none; }
	.tag-name { min-width: 0; }
	.tag-name strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; color: #ded3c4; }
	.tag-name small { display: block; margin-top: 4px; font-size: 10px; color: #8b8278; }
	.tag-side { display: grid; justify-items: end; gap: 3px; }
	.tag-last { font-size: 10px; color: #8b8278; font-variant-numeric: tabular-nums; white-space: nowrap; }
	.digest-text { font-size: 9px; font-weight: 700; }
	.digest-text.ready { color: var(--brass); }
	.digest-text.stale { color: #8b8278; }
	.digest-text.none { color: #706960; }
	.tag-heading:hover .tag-chevron { color: var(--brass); }
</style>
