<script lang="ts">
	import { loadApiConfig } from '$lib/api.svelte';
	import { checkServerConnection, connection } from '$lib/stores.svelte';
	import Icon from '$lib/Icon.svelte';

	let cfg = $state(loadApiConfig());
	let showToken = $state(false);

	const connected = $derived(connection.phase === 'connected');
	const testing = $derived(connection.phase === 'checking');
	const linkLabel = $derived(
		connection.phase === 'checking'
			? 'Checking'
			: connection.phase === 'connected'
				? 'Connected'
				: connection.phase === 'unavailable'
					? 'Unavailable'
					: 'Not configured'
	);
	const resultTitle = $derived(
		connected ? 'Connection established' : testing ? 'Checking connection' : 'Connection failed'
	);

	async function onTest(): Promise<void> {
		if (await checkServerConnection(cfg, true)) cfg = loadApiConfig();
	}
</script>

<svelte:head><title>Link · Transcripter</title></svelte:head>

<section class="page settings-page">
	<header>
		<h1 class="page-title">Server connection</h1>
	</header>

	<form class="connection-panel panel" onsubmit={(event) => { event.preventDefault(); onTest(); }}>
		<div class="panel-heading">
			<div class="antenna" aria-hidden="true"><Icon name="link" size={17} /></div>
			<div><span>SERVER</span><strong>Transcription server</strong></div>
			<div class:connected class="link-state"><i></i>{linkLabel}</div>
		</div>

		<div class="form-body">
			<label>
				<span class="field-label">Server address</span>
				<input type="url" bind:value={cfg.baseUrl} placeholder="http://192.168.1.10:8090" required />
			</label>
			<label>
				<span class="field-label">Bearer token</span>
				<span class="secret-field">
					<input type={showToken ? 'text' : 'password'} bind:value={cfg.token} autocomplete="off" placeholder="Enter access token" />
					<button type="button" onclick={() => (showToken = !showToken)} aria-label={showToken ? 'Hide token' : 'Show token'}>{showToken ? 'Hide' : 'Show'}</button>
				</span>
			</label>

			<button class="test-button" type="submit" disabled={testing}>
				<span class="test-icon" aria-hidden="true"><Icon name="link" size={20} /></span>
				<span><strong>{testing ? 'Testing channel…' : 'Test and save connection'}</strong><small>Verifies health and authorization</small></span>
			</button>
		</div>
	</form>

	{#if connection.phase !== 'unconfigured'}
		<div class:success={connected} class="result" role="status">
			<i aria-hidden="true"></i>
			<div><strong>{resultTitle}</strong><span>{connection.detail}</span></div>
		</div>
	{/if}

	<aside class="security-note">
		<span class="security-icon" aria-hidden="true"><Icon name="shield" size={17} /></span>
		<div><strong>Stored on this device</strong><p>Your connection details stay local. Use this client only on a network you trust.</p></div>
	</aside>
</section>

<style>
	.settings-page { display: flex; flex-direction: column; gap: 16px; }
	.connection-panel { overflow: hidden; }
	.panel-heading { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; padding: 12px; border-bottom: 1px solid var(--line); background: rgba(0,0,0,.16); }
	.panel-heading > div:nth-child(2) span, .panel-heading > div:nth-child(2) strong { display: block; }
	.panel-heading > div:nth-child(2) span { color: var(--red); font-size: 10px; font-weight: 700; }
	.panel-heading > div:nth-child(2) strong { margin-top: 4px; color: #d3c7b7; font-size: 13px; }
	.antenna { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid rgba(215,167,71,.3); border-radius: 50%; color: var(--brass); line-height: 0; }
	.link-state { display: flex; align-items: center; gap: 6px; color: #8b8379; font-size: 10px; font-weight: 650; }
	.link-state i { width: 5px; height: 5px; border-radius: 50%; background: #655f58; }
	.link-state.connected { color: var(--cyan); }
	.link-state.connected i { background: var(--cyan); box-shadow: 0 0 7px var(--cyan); }
	.form-body { display: grid; gap: 15px; padding: 15px; }
	.secret-field { display: grid; grid-template-columns: 1fr 55px; }
	.secret-field input { border-radius: 3px 0 0 3px; }
	.secret-field button { border: 1px solid rgba(231,214,190,.18); border-left: 0; border-radius: 0 3px 3px 0; background: rgba(215,167,71,.06); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.test-button { min-height: 55px; display: grid; grid-template-columns: 31px 1fr; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid rgba(215,167,71,.55); border-radius: 3px; background: linear-gradient(100deg, rgba(111,23,21,.62), rgba(215,167,71,.12)); color: var(--bone); text-align: left; cursor: pointer; }
	.test-button:hover:not(:disabled) { border-color: var(--brass); background: linear-gradient(100deg, rgba(133,27,24,.72), rgba(215,167,71,.17)); }
	.test-icon { width: 31px; height: 31px; display: grid; place-items: center; color: var(--brass); line-height: 0; }
	.test-button strong, .test-button small { display: block; }
	.test-button strong { font-size: 13px; }
	.test-button small { margin-top: 4px; color: #b4a99a; font-size: 10px; }
	.result { display: grid; grid-template-columns: auto 1fr; gap: 9px; padding: 11px; border-left: 2px solid var(--red); background: rgba(213,45,36,.07); }
	.result > i { width: 7px; height: 7px; margin-top: 3px; border-radius: 50%; background: var(--red); }
	.result strong, .result span { display: block; }
	.result strong { color: #e1746b; font-size: 12px; }
	.result span { margin-top: 3px; color: #b5aa9c; font-size: 11px; line-height: 1.45; }
	.result.success { border-color: var(--cyan); background: rgba(112,215,208,.055); }
	.result.success > i { background: var(--cyan); box-shadow: 0 0 8px var(--cyan); }
	.result.success strong { color: var(--cyan); }
	.security-note { display: grid; grid-template-columns: auto 1fr; gap: 10px; padding: 12px; border-top: 1px solid rgba(215,167,71,.2); border-bottom: 1px solid rgba(215,167,71,.2); color: #7d746a; }
	.security-icon { width: 20px; height: 20px; display: grid; place-items: center; color: var(--brass); line-height: 0; }
	.security-note strong { color: #afa397; font-size: 10px; font-weight: 700; }
	.security-note p { margin: 5px 0 0; font-size: 11px; line-height: 1.5; }
</style>
