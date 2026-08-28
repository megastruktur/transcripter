<script lang="ts">
	import { onMount } from 'svelte';
	import { loadApiConfig } from '$lib/api.svelte';
	import {
		audioDevices,
		checkAudioDevices,
		checkServerConnection,
		connection,
		ensureAudioDevices,
		preflight,
		recorder,
		selectAudioDevices,
		SYSTEM_AUDIO_OFF
	} from '$lib/stores.svelte';
	import Icon from '$lib/Icon.svelte';
	import { isAndroidTauri } from '$lib/mobile-recorder';

	let cfg = $state(loadApiConfig());
	const android = isAndroidTauri();
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

	onMount(() => {
		// Instant from the shared cache on remounts; enumerates and checks in
		// the background only when there is no report for this selection yet.
		void ensureAudioDevices();
	});

	function sourceLabel(state: 'disabled' | 'ready' | 'silent' | 'permission_denied' | 'unavailable' | 'failed'): string {
		if (state === 'ready') return 'Ready';
		if (state === 'silent') return 'No signal';
		if (state === 'permission_denied') return 'Permission denied';
		if (state === 'disabled') return 'Off';
		return 'Unavailable';
	}

	const micState = $derived(!preflight.current ? 'Not checked' : sourceLabel(preflight.current.mic_state));
	const systemState = $derived(audioDevices.selectedSystemOutput === SYSTEM_AUDIO_OFF ? 'Off' : !preflight.current ? 'Not checked' : sourceLabel(preflight.current.system_state));
	const audioLabel = $derived(
		audioDevices.checking
			? 'Checking'
			: !preflight.current
				? 'Not checked'
				: preflight.current.error || ['silent', 'permission_denied', 'unavailable', 'failed'].includes(preflight.current.mic_state) || ['silent', 'permission_denied', 'unavailable', 'failed'].includes(preflight.current.system_state)
					? 'Needs attention'
					: 'Ready'
	);

	function microphoneChanged(): void {
		selectAudioDevices({ microphone: audioDevices.selectedMicrophone });
	}

	function systemOutputChanged(): void {
		selectAudioDevices({ systemOutput: audioDevices.selectedSystemOutput });
	}

	async function onTest(): Promise<void> {
		if (await checkServerConnection(cfg, true)) cfg = loadApiConfig();
	}
</script>

<svelte:head><title>Settings · Transcriptor Maximus</title></svelte:head>

<section class="page settings-page">
	<header>
		<h1 class="page-title">Settings</h1>
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

	<div class="device-panel panel">
		<div class="panel-heading">
			<div class="antenna" aria-hidden="true"><Icon name="microphone" size={17} /></div>
			<div><span>AUDIO</span><strong>Capture devices</strong></div>
			<div class:connected={audioLabel === 'Ready'} class="link-state"><i></i>{audioLabel}</div>
		</div>

		<div class="form-body">
			{#if android}
				<div class="notice android-mic-note" role="status">
					<strong>System microphone</strong>
					<span
						>Android manages the input device and audio routing. The microphone
						permission prompt appears when you start a recording.</span
					>
				</div>
			{:else}
			{#if preflight.current?.error}
				<div class="notice error" role="alert"><strong>Pre-flight failed</strong><span>{preflight.current.error}</span></div>
			{/if}
			{#each recorder.warnings as warning (warning)}
				<div class="notice warning" role="status"><strong>Signal warning</strong><span>{warning}</span></div>
			{/each}

			<div class="device-control">
				<div class="device-heading">
					<span class="device-icon" aria-hidden="true"><Icon name="microphone" size={18} /></span>
					<label for="microphone-device">Microphone</label>
					<span class:ready={micState === 'Ready'} class:issue={micState === 'No signal' || micState === 'Unavailable' || micState === 'Permission denied'} class="device-status">{micState}</span>
				</div>
				<select id="microphone-device" bind:value={audioDevices.selectedMicrophone} onchange={microphoneChanged} disabled={audioDevices.loading || recorder.recording}>
					{#if audioDevices.devices.microphones.length === 0}<option value="" disabled>{audioDevices.loading ? 'Loading microphones…' : 'No microphones found'}</option>{/if}
					{#each audioDevices.devices.microphones as device (device.id)}
						<option value={device.id}>{device.label}{device.is_default ? ' — default' : ''}</option>
					{/each}
				</select>
			</div>

			<div class="device-control">
				<div class="device-heading">
					<span class="device-icon" aria-hidden="true"><Icon name="monitor" size={18} /></span>
					<label for="system-output-device">System audio</label>
					<span class:ready={systemState === 'Ready'} class:issue={systemState === 'No signal' || systemState === 'Unavailable' || systemState === 'Permission denied'} class="device-status">{systemState}</span>
				</div>
				<select id="system-output-device" bind:value={audioDevices.selectedSystemOutput} onchange={systemOutputChanged} disabled={audioDevices.loading || recorder.recording}>
					<option value={SYSTEM_AUDIO_OFF}>Off</option>
					{#each audioDevices.devices.system_outputs as device (device.id)}
						<option value={device.id}>{device.label}{device.is_default ? ' — default' : ''}</option>
					{/each}
				</select>
			</div>

			<button class="check-devices" type="button" disabled={audioDevices.checking || audioDevices.loading || recorder.recording || !audioDevices.selectedMicrophone} onclick={() => checkAudioDevices()}>
				<Icon name="refresh" size={15} />
				{audioDevices.checking ? 'Checking selected devices…' : 'Check selected devices'}
			</button>
			{#if audioDevices.error}<p class="device-error" role="alert">Could not load audio devices: {audioDevices.error}</p>{/if}
			{/if}
		</div>
	</div>
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
	.notice { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--brass); background: rgba(215, 167, 71, 0.07); font-size: 12px; line-height: 1.4; }
	.notice.error { border-color: var(--red); background: rgba(213, 45, 36, 0.08); }
	.notice strong { font-size: 10px; font-weight: 700; color: var(--brass); }
	.notice.error strong { color: var(--red); }
	.notice span { color: #b5aa9c; font-size: 11px; }
	.device-panel { overflow: hidden; }
	.device-control { display: grid; gap: 6px; }
	.device-heading { display: grid; grid-template-columns: 22px 1fr auto; align-items: center; gap: 7px; min-width: 0; }
	.device-heading label { color: #c9bdad; font-size: 11px; font-weight: 650; }
	.device-icon { width: 22px; height: 22px; display: grid; place-items: center; color: var(--brass); line-height: 0; }
	.device-status { max-width: 100px; overflow: hidden; color: #8d847a; font-size: 10px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
	.device-status.ready { color: var(--cyan); }
	.device-status.issue { color: var(--brass); }
	.check-devices { min-height: 34px; display: flex; align-items: center; justify-content: center; gap: 8px; border: 1px solid rgba(215,167,71,.32); border-radius: 3px; background: rgba(215,167,71,.07); color: var(--brass); font-size: 11px; font-weight: 700; cursor: pointer; }
	.check-devices:hover:not(:disabled) { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.device-error { margin: 0; color: #df756b; font-size: 10px; line-height: 1.4; }
	.android-mic-note { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--line); background: rgba(0,0,0,.18); font-size: 12px; line-height: 1.4; }
	.android-mic-note strong { font-size: 10px; font-weight: 700; color: #8d847a; }
</style>
