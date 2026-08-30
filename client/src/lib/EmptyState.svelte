<script lang="ts">
	import Icon, { type IconName } from './Icon.svelte';

	let { icon, title, hint, tone = 'brass' }: { icon: string; title: string; hint: string; tone?: 'red' | 'brass' } = $props();
</script>

<div class="empty">
	<span class="empty-icon" class:empty-icon--red={tone === 'red'} aria-hidden="true"><Icon name={icon as IconName} size={30} /></span>
	<strong>{title}</strong>
	<small>{hint}</small>
</div>

<style>
	.empty { display: grid; justify-items: center; gap: 5px; padding: 28px 16px; color: #746d64; text-align: center; }
	.empty-icon { display: grid; place-items: center; color: var(--brass); line-height: 0; }
	.empty-icon--red { color: var(--red); }
	.empty strong { color: #b5aa9c; font-size: 13px; }
	.empty small { font-size: 11px; }
</style>
