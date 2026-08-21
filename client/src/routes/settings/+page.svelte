<script lang="ts">
	import { loadApiConfig, saveApiConfig, testConnection } from '$lib/api.svelte';

	let cfg = $state(loadApiConfig());
	let status = $state('');

	async function onTest() {
		try {
			const u = new URL(cfg.baseUrl.trim());
			if (u.protocol === 'https:') {
				status =
					'failed: https is unsupported by the uploader in this build (LAN MVP is http-only)';
				return;
			}
		} catch {
			status = 'failed: invalid URL';
			return;
		}
		try {
			const s = await testConnection(cfg);
			status = `ok (${s})`;
			saveApiConfig(cfg);
		} catch (e) {
			status = `failed: ${e}`;
		}
	}
</script>

<section>
	<h1>Settings</h1>
	<label>
		Server URL
		<input type="url" bind:value={cfg.baseUrl} />
	</label>
	<label>
		Token
		<input type="password" bind:value={cfg.token} />
	</label>
	<button onclick={onTest}>Test connection</button>
	{#if status}
		<p class:ok={status.startsWith('ok')} class:fail={!status.startsWith('ok')}>{status}</p>
	{/if}
	<p><small>Settings persist locally after successful test.</small></p>
</section>

<style>
	label {
		display: block;
		margin-bottom: 0.75rem;
	}
	input {
		display: block;
		margin-top: 0.25rem;
		width: 100%;
		max-width: 24rem;
	}
	.ok {
		color: green;
	}
	.fail {
		color: #b00;
	}
</style>
