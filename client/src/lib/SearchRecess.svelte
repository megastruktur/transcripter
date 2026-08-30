<script lang="ts">
	import Icon from '$lib/Icon.svelte';
	import {
		type SearchHit,
		type SearchResponse,
		type GlobalSearchResponse
	} from '$lib/api.svelte';

	// Both response shapes share the SearchHit fields; GlobalSearchHit adds
	// the source tag, rendered only when showTag is set (global search).
	type Hit = SearchHit & { tag?: string };

	type Props = {
		ariaLabel: string;
		placeholder: string;
		showTag?: boolean;
		query: string;
		loading: boolean;
		error: string;
		note: string;
		results: SearchResponse | GlobalSearchResponse | null;
		onsearch: () => void;
		onclear: () => void;
		onopenhit: (recordingId: string, tsStart: number) => void;
	};

	let {
		ariaLabel,
		placeholder,
		showTag = false,
		query = $bindable(),
		loading,
		error,
		note,
		results,
		onsearch,
		onclear,
		onopenhit
	}: Props = $props();

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

	const hits: Hit[] = $derived(results?.hits ?? []);
</script>

<section class="search-recess" aria-label={ariaLabel}>
	<form
		class="search-form"
		onsubmit={(event) => {
			event.preventDefault();
			onsearch();
		}}
	>
		<input
			class="search-input"
			type="search"
			placeholder={placeholder}
			aria-label="Search query"
			bind:value={query}
			disabled={loading}
		/>
		<button class="search-go" type="submit" disabled={loading || !query.trim()}>
			<Icon name="search" size={12} strokeWidth={1.6} />
			{loading ? 'Searching…' : 'Search'}
		</button>
		{#if results !== null || note || error}
			<button class="search-clear" type="button" onclick={onclear} aria-label="Clear search results">
				<Icon name="close" size={12} strokeWidth={1.6} />
			</button>
		{/if}
	</form>
	{#if error}
		<p class="search-error" role="alert">{error}</p>
	{:else if note}
		<p class="search-note">{note}</p>
	{:else if results !== null}
		{#if hits.length === 0}
			<p class="search-note">No matching segments.</p>
		{:else}
			<div class="hit-list">
				{#each hits as hit (showTag ? `${hit.tag}${hit.recording_id}${hit.ts_start}` : `${hit.recording_id}${hit.ts_start}`)}
					<button
						class="hit-row"
						type="button"
						title={`Open ${hit.session_title || 'session'} at ${tsLabel(hit.ts_start)}`}
						onclick={() => onopenhit(hit.recording_id, hit.ts_start)}
					>
						<span class="hit-ts">{tsLabel(hit.ts_start)}</span>
						<span class="hit-main">
							<strong>{hit.session_title || 'Untitled capture'}</strong>
							<small>
								{#if showTag && hit.tag}<span class="hit-tag">{hit.tag}</span> · {/if}{#if hit.speaker}{hit.speaker} · {/if}{hit.snippet}
							</small>
						</span>
					</button>
				{/each}
			</div>
		{/if}
	{/if}
</section>

<style>
	/* Semantic search (3.5): recess panel + brass controls — same material
	   cut as the digest recess, same control idiom as the digest-regen
	   family. Hit rows are a ruled manifest on the recess surface.
	   Verbatim from the vault pages (their copies were identical). */
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
</style>
