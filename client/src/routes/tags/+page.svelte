<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import EmptyState from '$lib/EmptyState.svelte';
	import Skeleton from '$lib/Skeleton.svelte';
	import { createTag, fetchTags, loadApiConfig, type TagCount } from '$lib/api.svelte';

	let tags = $state<TagCount[]>([]);
	let error = $state('');
	let loading = $state(true);
	// Create form: name + immediate register (vocabulary edits live on the
	// tag page — keep this surface a one-field manifest header).
	let newName = $state('');
	let creating = $state(false);
	let createError = $state('');

	async function refresh(): Promise<void> {
		try {
			tags = await fetchTags(loadApiConfig());
			error = '';
		} catch (caught) {
			error = String(caught);
		} finally {
			loading = false;
		}
	}

	async function submit(): Promise<void> {
		const name = newName.trim();
		if (!name || creating) return;
		creating = true;
		createError = '';
		try {
			await createTag(loadApiConfig(), name);
			newName = '';
			await refresh();
		} catch (caught) {
			const status = (caught as { status?: number }).status;
			createError =
				status === 409 ? 'Tag already exists' : status === 400 ? 'Invalid tag name' : String(caught);
		} finally {
			creating = false;
		}
	}

	onMount(refresh);
</script>

<svelte:head><title>Tags · Transcriptor Maximus</title></svelte:head>

<section class="page tags-page">
	<form class="tag-create" onsubmit={(e) => { e.preventDefault(); void submit(); }}>
		<label>
			<span class="sr-only">New tag name</span>
			<input
				type="text"
				placeholder="New tag name"
				maxlength="64"
				bind:value={newName}
				disabled={creating}
			/>
		</label>
		<button type="submit" disabled={!newName.trim() || creating}>Register tag</button>
	</form>
	{#if createError}
		<div class="tag-error" role="alert">{createError}</div>
	{/if}

	{#if error}
		<div class="tag-error" role="alert"><strong>Tags unavailable</strong><span>{error}</span></div>
	{/if}

	<div class="tag-manifest" aria-live="polite">
		{#if loading}
			<Skeleton variant="record" count={3} />
		{:else if tags.length === 0}
			<EmptyState icon="tags" title="No tags yet" hint="Register a tag before recording, or tag a recording and it appears here." />
		{:else}
			{#each tags as tag (tag.tag)}
				<button class="tag-row" type="button" onclick={() => goto(`/tags/${encodeURIComponent(tag.tag)}`)}>
					<span class="tag-cell tag-name">{tag.tag}</span>
					<span class="tag-cell tag-count">{tag.count} rec</span>
					<span class="tag-cell tag-vocab" class:none={!tag.vocabulary_count}>{tag.vocabulary_count ?? 0} words</span>
				</button>
			{/each}
		{/if}
	</div>
</section>

<style>
	.tags-page { display: flex; flex-direction: column; gap: 12px; }
	.tag-create { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: stretch; }
	.tag-create input { min-height: 42px; }
	.tag-create button { min-height: 42px; }
	.tag-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; color: #c6baaa; }
	.tag-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.tag-manifest { display: grid; }
	.tag-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 10px; align-items: center; width: 100%; padding: 10px 2px; background: none; border: 0; border-bottom: 1px solid var(--line); color: inherit; text-align: left; cursor: pointer; }
	.tag-row:hover { background: rgba(255,255,255,0.025); }
	.tag-create button { border: 1px solid var(--brass); background: rgba(215, 167, 71, 0.12); color: var(--brass); border-radius: 3px; padding: 0 14px; font-size: 12px; font-weight: 700; cursor: pointer; }
	.tag-create button:hover:not(:disabled) { color: var(--bone); border-color: var(--bone); }
	.tag-create button:disabled { cursor: not-allowed; }
	.tag-row:not(:last-child) { border-bottom: 1px solid var(--line); }
	.tag-cell { font-size: 12px; color: #ded3c4; }
	.tag-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
	.tag-count { color: #8b8278; font-variant-numeric: tabular-nums; }
	.tag-vocab { color: #8b8278; font-variant-numeric: tabular-nums; }
	.tag-vocab.none { color: #706960; }
</style>
