<script lang="ts">
	import { onMount } from 'svelte';
	import { checkAudio, clearWarnings, recorder, preflight, startRecording, stopRecording } from '$lib/stores.svelte';
	import { commands, type AudioDevices } from '$lib/tauri';
	import Icon from '$lib/Icon.svelte';

	const SYSTEM_AUDIO_OFF = '__off__';
	let title = $state('');
	let elapsed = $state(0);
	let starting = $state(false);
	let checkingAudio = $state(false);
	let loadingDevices = $state(true);
	let deviceError = $state('');
	let devices = $state<AudioDevices>({ microphones: [], system_outputs: [], default_microphone: null, default_system_output: null });
	let selectedMicrophone = $state('');
	let selectedSystemOutput = $state(SYSTEM_AUDIO_OFF);

	function sourceLabel(state: 'disabled' | 'ready' | 'silent' | 'permission_denied' | 'unavailable' | 'failed'): string {
		if (state === 'ready') return 'Ready';
		if (state === 'silent') return 'No signal';
		if (state === 'permission_denied') return 'Permission denied';
		if (state === 'disabled') return 'Off';
		return 'Unavailable';
	}

	const micState = $derived(!preflight.current ? 'Not checked' : sourceLabel(preflight.current.mic_state));
	const systemState = $derived(selectedSystemOutput === SYSTEM_AUDIO_OFF ? 'Off' : !preflight.current ? 'Not checked' : sourceLabel(preflight.current.system_state));

	onMount(() => {
		void loadAudioDevices();
	});

	$effect(() => {
		if (!recorder.recording) {
			elapsed = 0;
			return;
		}
		const startedAt = Date.now();
		const timer = globalThis.setInterval(() => {
			elapsed = Math.floor((Date.now() - startedAt) / 1000);
		}, 1000);
		return () => globalThis.clearInterval(timer);
	});

	function fmt(sec: number): string {
		const h = Math.floor(sec / 3600);
		const m = Math.floor((sec % 3600) / 60);
		const s = sec % 60;
		return h ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	async function loadAudioDevices(): Promise<void> {
		loadingDevices = true;
		deviceError = '';
		try {
			devices = await commands.listAudioDevices();
			const savedMicrophone = localStorage.getItem('transcripter.microphone');
			const savedSystemOutput = localStorage.getItem('transcripter.system-output');
			selectedMicrophone = savedMicrophone && devices.microphones.some((device) => device.id === savedMicrophone)
				? savedMicrophone
				: (devices.default_microphone ?? devices.microphones[0]?.id ?? '');
			selectedSystemOutput = savedSystemOutput && (savedSystemOutput === SYSTEM_AUDIO_OFF || devices.system_outputs.some((device) => device.id === savedSystemOutput))
				? savedSystemOutput
				: (devices.default_system_output ?? devices.system_outputs[0]?.id ?? SYSTEM_AUDIO_OFF);
		} catch (error) {
			deviceError = String(error);
		} finally {
			loadingDevices = false;
		}
	}

	function deviceSelectionChanged(): void {
		preflight.current = null;
		localStorage.setItem('transcripter.microphone', selectedMicrophone);
		localStorage.setItem('transcripter.system-output', selectedSystemOutput);
	}

	async function checkAudioDevices(): Promise<void> {
		if (!selectedMicrophone) {
			recorder.warnings.push('no microphone available');
			return;
		}
		checkingAudio = true;
		clearWarnings();
		try {
			await checkAudio(
				selectedMicrophone,
				selectedSystemOutput === SYSTEM_AUDIO_OFF ? null : selectedSystemOutput,
				selectedSystemOutput !== SYSTEM_AUDIO_OFF
			);
		} catch (error) {
			recorder.warnings.push(String(error));
		} finally {
			checkingAudio = false;
		}
	}

	async function beginRecording(): Promise<void> {
		if (!selectedMicrophone) {
			recorder.warnings.push('no microphone available');
			return;
		}
		starting = true;
		clearWarnings();
		try {
			await startRecording(
				title,
				selectedMicrophone,
				selectedSystemOutput === SYSTEM_AUDIO_OFF ? null : selectedSystemOutput,
				selectedSystemOutput !== SYSTEM_AUDIO_OFF
			);
		} catch (error) {
			recorder.warnings.push(String(error));
		} finally {
			starting = false;
		}
	}
</script>

<svelte:head><title>Capture · Transcripter</title></svelte:head>

<section class="page capture-page">
	<header>
		<h1 class="page-title">Record audio</h1>
	</header>

	{#if preflight.current?.error}
		<div class="notice error" role="alert"><strong>Pre-flight failed</strong><span>{preflight.current.error}</span></div>
	{/if}
	{#each recorder.warnings as warning (warning)}
		<div class="notice warning" role="status"><strong>Signal warning</strong><span>{warning}</span></div>
	{/each}

	<div class:active={recorder.recording} class="recorder-core panel">
		<div class="meter" aria-hidden="true">
			{#each [12, 22, 34, 18, 42, 28, 48, 20, 38, 16, 30, 10] as height, index (index)}
				<i style={`--bar-height: ${height}px; --delay: ${index * -74}ms`}></i>
			{/each}
		</div>
		<div class="timer" aria-live="polite">{fmt(elapsed)}</div>
		<div class="capture-meta">
			<span>{recorder.recording ? 'Recording in FLAC' : 'Ready to record'}</span>
			<span>{recorder.recording ? `${recorder.frames} frames captured` : 'Processed after you stop'}</span>
		</div>

		<label class="title-field">
			<span class="field-label">Recording name</span>
			<input type="text" placeholder="e.g. Product sync — August 22" bind:value={title} disabled={recorder.recording} />
		</label>

		{#if recorder.recording}
			<button class="record-control stop" type="button" disabled={recorder.stopping} onclick={() => stopRecording()}>
				<span class="control-symbol" aria-hidden="true"><Icon name="stop" size={12} /></span>
				<span><strong>{recorder.stopping ? 'Sealing archive…' : 'Stop recording'}</strong><small>Finish and send for processing</small></span>
			</button>
		{:else}
			<button class="record-control start" type="button" disabled={starting} onclick={beginRecording}>
				<span class="control-symbol" aria-hidden="true"><Icon name="dot" size={12} /></span>
				<span><strong>{starting ? 'Running pre-flight…' : 'Start recording'}</strong><small>Checks devices before capture</small></span>
			</button>
		{/if}
	</div>

	<div class="device-panel panel">
		<div class="device-control">
			<div class="device-heading">
				<span class="device-icon" aria-hidden="true"><Icon name="microphone" size={18} /></span>
				<label for="microphone-device">Microphone</label>
				<span class:ready={micState === 'Ready'} class:issue={micState === 'No signal' || micState === 'Unavailable' || micState === 'Permission denied'} class="device-status">{micState}</span>
			</div>
			<select id="microphone-device" bind:value={selectedMicrophone} onchange={deviceSelectionChanged} disabled={loadingDevices || recorder.recording}>
				{#if devices.microphones.length === 0}<option value="" disabled>{loadingDevices ? 'Loading microphones…' : 'No microphones found'}</option>{/if}
				{#each devices.microphones as device (device.id)}
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
			<select id="system-output-device" bind:value={selectedSystemOutput} onchange={deviceSelectionChanged} disabled={loadingDevices || recorder.recording}>
				<option value={SYSTEM_AUDIO_OFF}>Off</option>
				{#each devices.system_outputs as device (device.id)}
					<option value={device.id}>{device.label}{device.is_default ? ' — default' : ''}</option>
				{/each}
			</select>
		</div>

		<button class="check-devices" type="button" disabled={checkingAudio || loadingDevices || recorder.recording || !selectedMicrophone} onclick={checkAudioDevices}>
			<Icon name="refresh" size={15} />
			{checkingAudio ? 'Checking selected devices…' : 'Check selected devices'}
		</button>
		{#if deviceError}<p class="device-error" role="alert">Could not load audio devices: {deviceError}</p>{/if}
	</div>
</section>

<style>
	.capture-page { display: flex; flex-direction: column; gap: 10px; }
	.notice { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--brass); background: rgba(215, 167, 71, 0.07); font-size: 12px; line-height: 1.4; }
	.notice.error { border-color: var(--red); background: rgba(213, 45, 36, 0.08); }
	.notice strong { font-size: 10px; font-weight: 700; color: var(--brass); }
	.notice.error strong { color: var(--red); }
	.notice span { color: #c6baaa; }
	.recorder-core { padding: 12px; box-shadow: inset 0 1px rgba(255,255,255,0.025); position: relative; overflow: hidden; }
	.recorder-core::before { content: ''; position: absolute; inset: 0 auto 0 0; width: 2px; background: #534b43; }
	.recorder-core.active::before { background: var(--red); box-shadow: 0 0 16px var(--red); }
	.meter { height: 40px; display: flex; align-items: center; justify-content: center; gap: 5px; padding: 0 8px; }
	.meter i { width: 3px; height: calc(var(--bar-height) * 0.42); background: #4c4640; border-radius: 1px; transform-origin: center; }
	.active .meter i { background: linear-gradient(to top, var(--red), var(--brass)); animation: signal 780ms ease-in-out infinite alternate; animation-delay: var(--delay); box-shadow: 0 0 7px rgba(213, 45, 36, 0.25); }
	.timer { margin-top: 2px; text-align: center; font: 300 36px/1 "SFMono-Regular", Consolas, monospace; font-variant-numeric: tabular-nums; letter-spacing: 0.08em; color: var(--bone); }
	.capture-meta { display: flex; justify-content: space-between; gap: 8px; margin: 5px 0 8px; padding-bottom: 8px; border-bottom: 1px solid var(--line); font-size: 10px; color: #8d847a; }
	.title-field { display: block; margin-bottom: 10px; }
	.record-control { width: 100%; min-height: 48px; display: grid; grid-template-columns: 36px 1fr; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid var(--red); border-radius: 3px; background: linear-gradient(105deg, #7f1715, #c72b23 72%, #e34737); color: white; text-align: left; cursor: pointer; box-shadow: 0 8px 24px rgba(111, 23, 21, 0.25), inset 0 1px rgba(255,255,255,0.17); transition: transform 120ms ease, filter 120ms ease; }
	.record-control:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
	.record-control.stop { background: rgba(213, 45, 36, 0.08); color: #ff8b7c; box-shadow: inset 0 0 18px rgba(213, 45, 36, 0.06); }
	.control-symbol { width: 30px; height: 30px; display: grid; place-items: center; border: 1px solid rgba(255,255,255,0.42); border-radius: 50%; line-height: 0; }
	.record-control strong, .record-control small { display: block; }
	.record-control strong { font-size: 14px; letter-spacing: 0.01em; }
	.record-control small { margin-top: 4px; font-size: 10px; opacity: 0.74; }
	.device-panel { display: grid; gap: 10px; padding: 10px; }
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
	@keyframes signal { to { height: var(--bar-height); } }
</style>
