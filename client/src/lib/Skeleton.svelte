<script lang="ts">
	let { variant, count = 1 }: { variant: 'record' | 'tag' | 'heading' | 'panel-detail' | 'panel-tag'; count?: number } = $props();
</script>

{#if variant === 'heading'}
	<span class="skeleton-bar skeleton-heading" aria-hidden="true"></span>
{:else if variant === 'panel-detail'}
	<div class="skeleton-panel skeleton-panel--detail" aria-hidden="true">
		<span class="skeleton-bar skeleton-meta"></span>
		<span class="skeleton-bar skeleton-strip"></span>
		<span class="skeleton-bar skeleton-player"></span>
		<span class="skeleton-bar skeleton-body"></span>
	</div>
{:else if variant === 'panel-tag'}
	<div class="skeleton-panel skeleton-panel--tag" aria-hidden="true">
		<span class="skeleton-bar skeleton-strip"></span>
		<span class="skeleton-bar skeleton-body"></span>
	</div>
{:else if variant === 'record'}
	{#each Array(count) as _, i (i)}
		<div class="record-card skeleton-card" aria-hidden="true">
			<span class="skeleton-bar skeleton-mark"></span>
			<span class="skeleton-lines">
				<span class="skeleton-bar skeleton-title"></span>
				<span class="skeleton-bar skeleton-meta"></span>
			</span>
			<span class="skeleton-bar skeleton-label"></span>
		</div>
	{/each}
{:else}
	{#each Array(count) as _, i (i)}
		<div class="tag-card skeleton-card" aria-hidden="true">
			<span class="skeleton-lines">
				<span class="skeleton-bar skeleton-title"></span>
				<span class="skeleton-bar skeleton-meta"></span>
			</span>
			<span class="skeleton-bar skeleton-lamp"></span>
		</div>
	{/each}
{/if}

<style>
	/* Card skeletons: recordings page uses an auto/1fr/auto mark grid, the
	   vault shelf uses a 1fr/auto lamp grid — same class name in both
	   sources, so the card variant qualifies the grid. */
	.record-card.skeleton-card { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; padding: 11px; }
	.tag-card.skeleton-card { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 9px; padding: 11px; }
	.skeleton-lines { display: grid; gap: 6px; }
	.skeleton-bar { display: block; border-radius: 2px; background: var(--iron-raised); animation: skeleton-pulse 150ms ease-in-out infinite alternate; }
	.skeleton-mark { width: 7px; height: 7px; border-radius: 50%; }
	.skeleton-title { width: 62%; height: 11px; }
	.skeleton-card .skeleton-meta { width: 38%; height: 8px; }
	.skeleton-label { width: 46px; height: 18px; }
	.skeleton-lamp { width: 46px; height: 18px; }
	.skeleton-heading { width: 55%; height: 18px; }
	.skeleton-panel { display: grid; gap: 10px; padding: 0; }
	.skeleton-panel .skeleton-meta { width: 46%; height: 11px; }
	.skeleton-strip { width: 82%; height: 18px; }
	.skeleton-player { width: 100%; height: 36px; }
	.skeleton-panel--detail .skeleton-body { width: 100%; height: 220px; }
	.skeleton-panel--tag .skeleton-body { width: 100%; height: 90px; }
	@keyframes skeleton-pulse { from { opacity: 0.55; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .skeleton-bar { animation: none; opacity: 0.75; } }
</style>
