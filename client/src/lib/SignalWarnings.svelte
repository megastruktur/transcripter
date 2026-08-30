<script lang="ts">
	import { recorder } from '$lib/stores.svelte';
</script>

{#each recorder.warnings as warning (warning)}
	<div class="notice warning" role="status"><strong>Signal warning</strong><span>{warning}</span></div>
{/each}
