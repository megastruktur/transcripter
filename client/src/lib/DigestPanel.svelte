<script lang="ts">
	import Icon from '$lib/Icon.svelte';
	import Markdown from '$lib/Markdown.svelte';

	type Props = {
		tag: string;
		loading: boolean;
		generating: boolean;
		error: string;
		note: string;
		missing: boolean;
		text: string | null;
		onregen: () => void;
		onclose?: () => void;
	};

	let { tag, loading, generating, error, note, missing, text, onregen, onclose }: Props = $props();
</script>

<section class="digest-panel" aria-label={`Digest · ${tag}`}>
	<header class="digest-header">
		<span class="digest-title" title={tag}>
			<Icon name="summary" size={11} />
			Digest
			<span class="digest-tag-name">{tag}</span>
		</span>
		<button type="button" class="digest-regen" disabled={generating} onclick={onregen}>
			<Icon name="refresh" size={11} strokeWidth={1.5} />
			Regenerate
		</button>
		{#if onclose}
			<button type="button" class="digest-close" aria-label="Close digest" title="Close digest" onclick={onclose}>
				<Icon name="close" size={11} />
			</button>
		{/if}
	</header>
	{#if loading}
		<p class="tab-placeholder">Retrieving digest…</p>
	{:else if generating}
		<p class="tab-placeholder">Generating…</p>
	{:else if error}
		<p class="tab-error" role="alert">{error}</p>
	{:else if note}
		<p class="tab-placeholder">{note}</p>
	{:else if missing}
		<p class="tab-placeholder">No digest yet — generate first.</p>
	{:else if text !== null}
		<div class="digest-body"><Markdown text={text} /></div>
	{/if}
</section>

<style>
	/* Digest recess: same material cut as the detail page's digest panel.
	   Verbatim from client/src/routes/recordings/[id]/+page.svelte and the
	   tag page's digest-recess (identical rule bodies); .digest-body copied
	   from the tag page — unified here as the Markdown wrapper on both. */
	.digest-panel { flex: 1 1 auto; min-height: 160px; display: flex; flex-direction: column; overflow: hidden; background: rgba(0,0,0,.22); border-radius: 3px; box-shadow: inset 0 1px 3px rgba(0,0,0,.4); }
	.digest-header { display: flex; align-items: center; gap: 6px; padding: 5px 6px 5px 10px; border-bottom: 1px solid var(--line); }
	.digest-title { flex: 1; min-width: 0; display: inline-flex; align-items: center; gap: 6px; overflow: hidden; color: var(--ash); font-size: 9px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
	.digest-tag-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-transform: none; color: var(--bone); font-size: 10px; letter-spacing: 0.02em; }
	.digest-regen { display: inline-flex; align-items: center; gap: 5px; padding: 3px 8px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.digest-regen:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.digest-regen:disabled { opacity: 0.6; cursor: default; }
	.digest-close { width: 22px; height: 22px; flex: 0 0 auto; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #8e857b; cursor: pointer; line-height: 0; }
	.digest-close:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.digest-body { flex: 1; min-height: 0; overflow: auto; padding: 4px 2px 8px; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.tab-placeholder { margin: auto; padding: 18px; color: var(--ash); font-size: 11px; }
	.tab-error { margin: auto; padding: 18px; color: #f36b60; font-size: 11px; }
</style>
