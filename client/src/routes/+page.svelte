<script lang="ts">
	import { onMount } from 'svelte';
	import {
		audioDevices,
		clearWarnings,
		ensureAudioDevices,
		recorder,
		startRecording,
		stopRecording,
		SYSTEM_AUDIO_OFF
	} from '$lib/stores.svelte';
	import { commands } from '$lib/tauri';
	import Icon from '$lib/Icon.svelte';

	// Mirrors CAPTURE_RATE in src-tauri/src/capture.rs; recorder.frames is the
	// session's written-frame count, so elapsed time survives window collapse.
	const CAPTURE_RATE = 48_000;
	let title = $state('');
	let starting = $state(false);

	onMount(() => {
		// Instant from the shared cache on remounts; enumerates and checks in
		// the background only when there is no report for this selection yet.
		void ensureAudioDevices();
		// A remount (window re-expanded mid-recording) must not restart the
		// clock: seed frames immediately instead of waiting for the poller.
		if (recorder.recording) {
			commands.recordingFrames().then(
				(frames) => (recorder.frames = frames),
				() => {}
			);
		}
	});

	const elapsed = $derived(Math.floor(recorder.frames / CAPTURE_RATE));

	function fmt(sec: number): string {
		const h = Math.floor(sec / 3600);
		const m = Math.floor((sec % 3600) / 60);
		const s = sec % 60;
		return h ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	async function beginRecording(): Promise<void> {
		if (!audioDevices.selectedMicrophone) {
			recorder.warnings.push('no microphone available');
			return;
		}
		starting = true;
		clearWarnings();
		try {
			await startRecording(
				title,
				audioDevices.selectedMicrophone,
				audioDevices.selectedSystemOutput === SYSTEM_AUDIO_OFF ? null : audioDevices.selectedSystemOutput,
				audioDevices.selectedSystemOutput !== SYSTEM_AUDIO_OFF
			);
		} catch (error) {
			recorder.warnings.push(String(error));
		} finally {
			starting = false;
		}
	}
</script>

<svelte:head><title>Capture · Transcriptor Maximus</title></svelte:head>

<section class="page capture-page">
	<header>
		<h1 class="page-title">Record audio</h1>
	</header>

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
</section>

<style>
	.capture-page { display: flex; flex-direction: column; gap: 10px; }
	.notice { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--brass); background: rgba(215, 167, 71, 0.07); font-size: 12px; line-height: 1.4; }
	.notice strong { font-size: 10px; font-weight: 700; color: var(--brass); }
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
	@keyframes signal { to { height: var(--bar-height); } }
</style>
