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

	/** Seconds → "mm:ss" / "h:mm:ss" (same label as the tag page's hits;
	 * the seek target stays in raw seconds). */
	function tsLabel(seconds: number): string {
		const total = Math.max(0, Math.floor(seconds));
		const h = Math.floor(total / 3600);
		const m = Math.floor((total % 3600) / 60);
		const s = total % 60;
		const mm = String(m).padStart(h > 0 ? 2 : 1, '0');
		return `${h > 0 ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`;
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
	<header>
		<h1 class="page-title">Vault</h1>
	</header>

	<section class="search-recess" aria-label="Semantic search · all tags">
		<form
			class="search-form"
			onsubmit={(event) => {
				event.preventDefault();
				void runSearch();
			}}
		>
			<input
				class="search-input"
				type="search"
				placeholder="Search all sessions…"
				aria-label="Search query"
				bind:value={searchQuery}
				disabled={searchLoading}
			/>
			<button class="search-go" type="submit" disabled={searchLoading || !searchQuery.trim()}>
				<Icon name="search" size={12} strokeWidth={1.6} />
				{searchLoading ? 'Searching…' : 'Search'}
			</button>
			{#if searchResults !== null || searchNote || searchError}
				<button class="search-clear" type="button" onclick={clearSearch} aria-label="Clear search results">
					<Icon name="close" size={12} strokeWidth={1.6} />
				</button>
			{/if}
		</form>
		{#if searchError}
			<p class="search-error" role="alert">{searchError}</p>
		{:else if searchNote}
			<p class="search-note">{searchNote}</p>
		{:else if searchResults !== null}
			{#if searchResults.hits.length === 0}
				<p class="search-note">No matching segments.</p>
			{:else}
				<div class="hit-list">
					{#each searchResults.hits as hit (hit.tag + hit.recording_id + hit.ts_start)}
						<button
							class="hit-row"
							type="button"
							title={`Open ${hit.session_title || 'session'} at ${tsLabel(hit.ts_start)}`}
							onclick={() => openHit(hit.recording_id, hit.ts_start)}
						>
							<span class="hit-ts">{tsLabel(hit.ts_start)}</span>
							<span class="hit-main">
								<strong>{hit.session_title || 'Untitled capture'}</strong>
								<small>
									{#if hit.tag}<span class="hit-tag">{hit.tag}</span> · {/if}{#if hit.speaker}{hit.speaker} · {/if}{hit.snippet}
								</small>
							</span>
						</button>
					{/each}
				</div>
			{/if}
		{/if}
	</section>

	{#if error}
		<div class="vault-error" role="alert"><strong>Vault unavailable</strong><span>{error}</span></div>
	{/if}

	<div class="tag-list" aria-live="polite">
		{#if loading}
			{#each [0, 1, 2] as skeletonIndex (skeletonIndex)}
				<div class="tag-card skeleton-card" aria-hidden="true">
					<span class="skeleton-lines">
						<span class="skeleton-bar skeleton-title"></span>
						<span class="skeleton-bar skeleton-meta"></span>
					</span>
					<span class="skeleton-bar skeleton-lamp"></span>
				</div>
			{/each}
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
				<div class="empty">
					<span class="empty-icon" aria-hidden="true"><Icon name="vault" size={30} /></span>
					<strong>Vault is empty</strong>
					<small>Tag recordings in the Library — each tag becomes a shelf here.</small>
				</div>
			{/each}
		{/if}
	</div>
</section>

<style>
	.vault-page { display: flex; flex-direction: column; gap: 14px; }
	.vault-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.vault-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.vault-error span { color: #c6baaa; }
	/* Global search (3.75): recess panel + brass controls — the same
	   material cut and control idiom as the tag page's search recess. */
	.search-recess { display: flex; flex-direction: column; background: rgba(0,0,0,.22); border-radius: 3px; box-shadow: inset 0 1px 3px rgba(0,0,0,.4); }
	.search-form { display: grid; grid-template-columns: 1fr auto auto; align-items: stretch; gap: 6px; padding: 6px; }
	.search-input { min-width: 0; height: 30px; padding: 0 9px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 12px; }
	.search-input::placeholder { color: var(--ash); }
	.search-input:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }
	.search-input:disabled { opacity: 0.6; }
	.search-go { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 11px; font-weight: 700; cursor: pointer; }
	.search-go:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.search-go:disabled { opacity: 0.6; cursor: default; }
	.search-clear { display: inline-flex; align-items: center; justify-content: center; width: 30px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); cursor: pointer; }
	.search-clear:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.search-note { margin: 0; padding: 0 10px 8px; color: var(--ash); font-size: 11px; line-height: 1.45; overflow-wrap: anywhere; }
	.search-error { margin: 0; padding: 0 10px 8px; color: #f36b60; font-size: 11px; }
	.hit-list { display: grid; max-height: 240px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.hit-row { display: grid; grid-template-columns: auto 1fr; align-items: baseline; gap: 4px 9px; padding: 8px 10px; border: 0; border-top: 1px solid var(--line); background: transparent; text-align: left; cursor: pointer; }
	.hit-row:first-child { border-top: 0; }
	.hit-row:hover { background: rgba(255,255,255,.02); }
	.hit-ts { font: 10px/1.4 "SFMono-Regular", Consolas, monospace; color: var(--brass); font-variant-numeric: tabular-nums; }
	.hit-main { min-width: 0; display: grid; gap: 3px; }
	.hit-main strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: #ded3c4; }
	.hit-main small { overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; color: #8b8278; font-size: 10px; line-height: 1.4; }
	/* Tag eyebrow inside the hit's meta line: brass uppercase chip-less
	   label — the namespace the segment came from. */
	.hit-tag { color: var(--brass); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
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
	.empty { display: grid; justify-items: center; gap: 5px; padding: 28px 16px; color: #746d64; text-align: center; }
	.empty-icon { display: grid; place-items: center; color: var(--brass); line-height: 0; }
	.empty strong { color: #b5aa9c; font-size: 13px; }
	.empty small { font-size: 11px; }
	.skeleton-card { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 9px; padding: 11px; }
	.skeleton-lines { display: grid; gap: 6px; }
	.skeleton-bar { display: block; border-radius: 2px; background: var(--iron-raised); animation: skeleton-pulse 150ms ease-in-out infinite alternate; }
	.skeleton-title { width: 62%; height: 11px; }
	.skeleton-meta { width: 38%; height: 8px; }
	.skeleton-lamp { width: 46px; height: 18px; }
	@keyframes skeleton-pulse { from { opacity: 0.55; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .skeleton-bar { animation: none; opacity: 0.75; } }
</style>
