<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import Icon from '$lib/Icon.svelte';
	import { fetchVault, loadApiConfig, type VaultItem } from '$lib/api.svelte';
	import { dateLabel } from '$lib/format';

	let items = $state<VaultItem[]>([]);
	let loading = $state(true);
	let error = $state('');
	// Monotonic request id: a stale response racing an unmount must not
	// overwrite newer state.
	let fetchSeq = 0;

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
