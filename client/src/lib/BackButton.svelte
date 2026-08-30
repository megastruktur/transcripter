<script lang="ts">
	import { goto } from '$app/navigation';
	import Icon from './Icon.svelte';

	let { href, label }: { href: string; label: string } = $props();
</script>

<button class="back-button" type="button" onclick={() => goto(href)} aria-label={label} title={label}>
	<Icon name="back" size={16} />
</button>

<style>
	.back-button { width: 32px; height: 32px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #8e857b; cursor: pointer; line-height: 0; }
	.back-button:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
</style>
