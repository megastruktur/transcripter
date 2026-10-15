<script lang="ts">
	import { dismissWarning, recorder } from '$lib/stores.svelte';
	import Icon from '$lib/Icon.svelte';
</script>

{#each recorder.warnings as warning, index}
	<div class="notice warning" role="status">
		<strong>Signal warning</strong>
		<span>{warning}</span>
		<button type="button" class="warning-dismiss" aria-label="Dismiss warning" title="Dismiss" onclick={() => dismissWarning(index)}>
			<Icon name="close" size={11} />
		</button>
	</div>
{/each}

<style>
	.notice { position: relative; padding-right: 34px; }
	.warning-dismiss {
		position: absolute;
		top: 8px;
		right: 8px;
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		padding: 0;
		border: none;
		background: transparent;
		color: #8d847a;
		cursor: pointer;
		transition: color 120ms ease;
	}
	.warning-dismiss:hover { color: var(--bone); }
</style>
