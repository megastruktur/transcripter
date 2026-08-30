<script lang="ts">
	import { profilesCache } from '$lib/profiles.svelte';

	let { value = $bindable(), disabled = false }: { value: string | null; disabled?: boolean } = $props();

	/** The type hint shows which profile the selected type routes to. */
	const matchedProfile = $derived(
		value === null ? null : profilesCache.items.find((profile) => profile.type === value) ?? null
	);
</script>

<label class="type-field">
	<span class="field-label">Type</span>
	<select bind:value={value} disabled={disabled}>
		<option value={null}>None — default pipeline</option>
		{#each profilesCache.items as profile (profile.id)}
			<option value={profile.type}>{profile.display_name}</option>
		{/each}
	</select>
	{#if matchedProfile}
		<small class="type-hint">Profile: {matchedProfile.display_name}{matchedProfile.has_enrich ? ' · memory extraction on' : ''}</small>
	{/if}
</label>

<style>
	.type-field { display: block; }
	.type-field select { width: 100%; padding: 7px 8px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,0.25); color: var(--bone); font-size: 12px; }
	.type-hint { display: block; margin-top: 4px; font-size: 10px; color: #8d847a; }
</style>
