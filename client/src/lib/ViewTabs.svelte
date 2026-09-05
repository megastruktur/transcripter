<script lang="ts">
	import Icon from '$lib/Icon.svelte';
	import type { IconName } from '$lib/Icon.svelte';

	/** One underline tab row: the shared view-switching idiom (Vault's
	 * Timeline|Entities|Digest, the recording detail's artifact views).
	 * Keys are generic strings — pages own their tab vocabulary and
	 * what each key loads; the component owns only the idiom. */
	let {
		tabs,
		active,
		ariaLabel,
		onchange
	}: {
		tabs: { key: string; label: string; icon?: IconName }[];
		active: string;
		ariaLabel: string;
		onchange: (key: string) => void;
	} = $props();
</script>

<div class="view-tabs" role="tablist" aria-label={ariaLabel}>
	{#each tabs as tab (tab.key)}
		<button
			class="view-tab"
			type="button"
			role="tab"
			aria-label={tab.label}
			title={tab.label}
			aria-selected={active === tab.key}
			class:active={active === tab.key}
			onclick={() => onchange(tab.key)}
		>
			{#if tab.icon}
				<Icon name={tab.icon} size={12} />
			{/if}
			<span class="view-tab-label">{tab.label}</span>
		</button>
	{/each}
</div>

<style>
	/* Brass underline tabs: the same control idiom as the digest-button
	   family (brass border/underline for the selected control). The strip
	   scrolls horizontally when the vocabulary outgrows the viewport
	   (5 artifact tabs on a 360px phone) instead of breaking the grid. */
	.view-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); overflow-x: auto; scrollbar-width: none; flex-shrink: 0; }
	.view-tab { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 5px; padding: 8px 7px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: #8e857b; font-size: 11px; font-weight: 650; cursor: pointer; transition: color 120ms ease, border-color 120ms ease; }
	.view-tab:hover { color: var(--bone); }
	.view-tab.active { color: var(--brass); border-bottom-color: var(--brass); }
	/* Narrow frames (360px phones, the 440px desktop shell): five tabs with
	   labels outgrow the viewport — drop to the icon row. The label span
	   stays in the DOM for aria; only the ink is hidden. */
	@media (max-width: 480px) {
		.view-tab { padding: 8px 10px; }
		.view-tab-label { display: none; }
	}
</style>
