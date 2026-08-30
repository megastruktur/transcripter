<script lang="ts">
	import Icon from './Icon.svelte';

	let { title, hint, backHref, backLabel }: { title: string; hint: string; backHref: string; backLabel: string } = $props();
</script>

<div class="notice-panel">
	<strong>{title}</strong>
	<small>{hint}</small>
	<a class="back-link" href={backHref}><Icon name="back" size={13} /> {backLabel}</a>
</div>

<style>
	.notice-panel { display: grid; justify-items: start; gap: 6px; padding: 18px 14px; }
	.notice-panel strong { color: #b5aa9c; font-size: 13px; }
	.notice-panel small { color: #746d64; font-size: 11px; }
	.back-link { display: inline-flex; align-items: center; gap: 6px; margin-top: 4px; color: var(--brass); font-size: 11px; font-weight: 650; text-decoration: none; }
	.back-link:hover { color: var(--bone); }
</style>
