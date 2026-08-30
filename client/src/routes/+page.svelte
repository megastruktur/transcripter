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
	import { loadApiConfig, uploadDirect } from '$lib/api.svelte';
	import Icon from '$lib/Icon.svelte';
	import TagChips from '$lib/TagChips.svelte';
	import SignalWarnings from '$lib/SignalWarnings.svelte';
	import TypeProfileField from '$lib/TypeProfileField.svelte';
	import { mergeDraftTags } from '$lib/tags';
	import { ensureProfiles } from '$lib/profiles.svelte';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';
	import {
		isAndroidTauri,
		startMobileRecorder,
		startRecordingKeepalive,
		stopRecordingKeepalive
	} from '$lib/mobile-recorder';
	import type { MobileRecorder } from '$lib/mobile-recorder';

	// Mirrors CAPTURE_RATE in src-tauri/src/capture.rs; recorder.frames is the
	// session's written-frame count, so elapsed time survives window collapse.
	const CAPTURE_RATE = 48_000;
	let title = $state('');
	let tagDraft = $state('');
	let tags = $state<string[]>([]);
	let starting = $state(false);
	let android = $state(false);
	let mobile: MobileRecorder | null = null;
	let mobileFramesTimer: ReturnType<typeof setInterval> | null = null;
	let mobileStartedAt = 0;
	/** Failed mobile upload kept in memory for a manual retry — unlike the
	 * desktop spool there is no on-disk persistence yet (PoC), so leaving
	 * the page still loses it; the notice copy says as much. */
	let failedUpload: { blob: Blob; title: string; tags: string[]; durationSec: number } | null =
		$state(null);
	let retrying = $state(false);
	/** Selected pipeline type slug; null = default pipeline. */
	let recType = $state<string | null>(null);
	let tagSuggestions = $derived(tagSuggestionsCache.items);

	onMount(() => {
		// Instant from the shared cache on remounts; enumerates and checks in
		// the background only when there is no report for this selection yet.
		void ensureAudioDevices();
		// Type selector source + freehand-tag suggestions (GET /tags with a
		// recordings fallback); failures leave free-form entry working.
		void ensureProfiles(loadApiConfig());
		void ensureTagSuggestions(loadApiConfig());
		// A remount (window re-expanded mid-recording) must not restart the
		// clock: seed frames immediately instead of waiting for the poller.
		if (recorder.recording) {
			commands.recordingFrames().then(
				(frames) => (recorder.frames = frames),
				() => {}
			);
		}
		android = isAndroidTauri();
		return () => {
			if (mobileFramesTimer) {
				clearInterval(mobileFramesTimer);
				mobileFramesTimer = null;
			}
			// Mobile capture lives in THIS component (unlike the desktop
			// Rust-side session that survives remounts): leaving the page
			// mid-recording must tear the MediaRecorder down, or the store
			// keeps saying "recording" with no reachable handle to stop it.
			if (mobile) {
				mobile.cancel();
				mobile = null;
				recorder.recording = false;
				recorder.warnings.push('capture cancelled — left the page mid-recording');
				void stopRecordingKeepalive();
			}
		};
	});
	const elapsed = $derived(Math.floor(recorder.frames / CAPTURE_RATE));
	function fmt(sec: number): string {
		const h = Math.floor(sec / 3600);
		const m = Math.floor((sec % 3600) / 60);
		const s = sec % 60;
		return h ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	async function beginRecording(): Promise<void> {
		if (android) {
			await beginMobileRecording();
			return;
		}
		if (!audioDevices.selectedMicrophone) {
			recorder.warnings.push('no microphone available');
			return;
		}
		starting = true;
		clearWarnings();
		// Flush the draft before the recording starts so any in-progress
		// chip the user did not press Enter for is not silently dropped.
		tags = mergeDraftTags(tags, tagDraft);
		tagDraft = '';
		try {
			await startRecording(
				title,
				tags,
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

	async function beginMobileRecording(): Promise<void> {
		if (mobile || recorder.recording || recorder.stopping) return;
		starting = true;
		clearWarnings();
		tags = mergeDraftTags(tags, tagDraft);
		tagDraft = '';
		let handle: MobileRecorder;
		try {
			handle = startMobileRecorder({});
		} catch (error) {
			recorder.warnings.push(String(error));
			starting = false;
			return;
		}
		mobile = handle;
		// Gate the "recording" UI on the mic ACTUALLY streaming: a denied
		// system prompt must surface as a warning immediately, not as a
		// running clock that dies on Stop.
		try {
			await handle.ready;
		} catch (error) {
			if (mobile === handle) mobile = null;
			recorder.warnings.push(String(error));
			starting = false;
			return;
		}
		// Keepalive AFTER the getUserMedia grant, still before the recording UI
		// flips: a mic-type FGS requires RECORD_AUDIO to be ALREADY granted at
		// startForeground time ("anyOf […, RECORD_AUDIO]" in the platform
		// error), and the grant only exists once getUserMedia resolves. The app
		// is still guaranteed foreground here, which is the other FGS
		// requirement. Fail-loud: without the service a backgrounded recording
		// dies silently.
		try {
			await startRecordingKeepalive();
		} catch (error) {
			if (mobile === handle) mobile = null;
			handle.cancel();
			recorder.warnings.push(`Не удалось запустить фоновую запись: ${String(error)}`);
			starting = false;
			return;
		}
		// The page may have unmounted (cleanup cancelled the handle) while
		// the permission prompt was open — a late grant must NOT flip the
		// shared store or start a ticker nobody will clear.
		if (mobile !== handle) return;
		recorder.recording = true;
		recorder.frames = 0;
		mobileStartedAt = Date.now();
		// Mirror the desktop poller: tick `recorder.frames` at the same 500 ms cadence
		// so `elapsed = floor(frames / CAPTURE_RATE)` drives the visible timer without
		// a second source of truth. CAPTURE_RATE mirrors src-tauri/src/capture.rs.
		mobileFramesTimer = setInterval(() => {
			if (!recorder.recording) return;
			const elapsedSec = (Date.now() - mobileStartedAt) / 1000;
			recorder.frames = Math.floor(elapsedSec * CAPTURE_RATE);
		}, 500);
		starting = false;
	}

	async function stopMobileRecording(): Promise<void> {
		if (!mobile || recorder.stopping) return;
		recorder.stopping = true;
		if (mobileFramesTimer) {
			clearInterval(mobileFramesTimer);
			mobileFramesTimer = null;
		}
		const handle = mobile;
		mobile = null;
		// The FGS guarded the capture; once the user pressed Stop it must go
		// down regardless of how the MediaRecorder/upload paths end.
		await stopRecordingKeepalive();
		let blob: Blob;
		try {
			blob = await handle.stop();
		} catch (error) {
			recorder.stopping = false;
			recorder.recording = false;
			recorder.warnings.push(`stop failed: ${String(error)}`);
			return;
		}
		const durationSec = Math.max(0, (Date.now() - mobileStartedAt) / 1000);
		recorder.recording = false;
		const cfg = loadApiConfig();
		if (!cfg.baseUrl || !cfg.token) {
			recorder.warnings.push('no server configured — recording not uploaded');
			recorder.stopping = false;
			return;
		}
		try {
			const result = await uploadDirect(cfg, blob, title, tags, durationSec, {
				type: recType ?? undefined
			});
			recorder.warnings.push(`recording queued for processing (${result.id.slice(0, 8)}…)`);
		} catch (error) {
			// Do NOT discard the audio: the desktop path survives via the
			// on-disk spool; the mobile PoC keeps the blob in memory and
			// offers a manual retry instead of silently losing the take.
			failedUpload = { blob, title, tags: [...tags], durationSec };
			recorder.warnings.push(`upload failed: ${String(error)}`);
		} finally {
			recorder.stopping = false;
		}
	}

	async function retryFailedUpload(): Promise<void> {
		const pending = failedUpload;
		if (!pending || retrying) return;
		retrying = true;
		try {
			const result = await uploadDirect(
				loadApiConfig(),
				pending.blob,
				pending.title,
				pending.tags,
				pending.durationSec,
				{ type: recType ?? undefined }
			);
			failedUpload = null;
			recorder.warnings.push(`recording queued for processing (${result.id.slice(0, 8)}…)`);
		} catch {
			// Block stays; the user can retry again. No stacked warnings —
			// the persistent notice already carries the failure state.
		} finally {
			retrying = false;
		}
	}

</script>

<svelte:head><title>Capture · Transcriptor Maximus</title></svelte:head>

<section class="page capture-page">
	<SignalWarnings />

	{#if failedUpload}
		<div class="notice warning upload-pending" role="status">
			<strong>Upload pending</strong>
			<span
				>the take is kept in memory only — retry when the network is back; leaving this page
				discards it</span
			>
			<button type="button" class="retry-upload" onclick={retryFailedUpload} disabled={retrying}>
				{retrying ? 'Retrying…' : 'Retry upload'}
			</button>
		</div>
	{/if}

	<div class:active={recorder.recording} class="recorder-core">
		<div class="meter" aria-hidden="true">
			{#each [12, 22, 34, 18, 42, 28, 48, 20, 38, 16, 30, 10] as height, index (index)}
				<i style={`--bar-height: ${height}px; --delay: ${index * -74}ms`}></i>
			{/each}
		</div>
		<div class="timer" aria-live="polite">{fmt(elapsed)}</div>
		<div class="capture-meta">
			<span>{recorder.recording ? (android ? 'Recording in WebM' : 'Recording in FLAC') : 'Ready to record'}</span>
			<span>{recorder.recording ? `${fmt(elapsed)} captured` : 'Processed after you stop'}</span>
		</div>
	</div>

	<div class="capture-fields">
		<TypeProfileField bind:value={recType} disabled={recorder.recording} />

		<label class="title-field">
			<span class="field-label">Recording name</span>
			<input type="text" placeholder="e.g. Product sync — August 22" bind:value={title} disabled={recorder.recording} />
		</label>

		<label class="tags-field" class:disabled={recorder.recording}>
			<span class="field-label">Tags <small>Press Enter or comma to add</small></span>
			<TagChips
				{tags}
				bind:draft={tagDraft}
				disabled={recorder.recording}
				suggestions={tagSuggestions}
				placeholder="e.g. doctronic, dnd dark castle"
				onChange={(next) => (tags = next)}
			/>
		</label>
	</div>

	{#if recorder.recording}
		<button class="record-control stop" type="button" disabled={recorder.stopping} onclick={() => android ? stopMobileRecording() : stopRecording()}>
			<span class="control-symbol" aria-hidden="true"><Icon name="stop" size={12} /></span>
			<span><strong>{recorder.stopping ? 'Sealing archive…' : 'Stop recording'}</strong><small>Finish and send for processing</small></span>
		</button>
	{:else}
		<button class="record-control start" type="button" disabled={starting} onclick={beginRecording}>
			<span class="control-symbol" aria-hidden="true"><Icon name="dot" size={12} /></span>
			<span><strong>{starting ? 'Running pre-flight…' : 'Start recording'}</strong><small>Checks devices before capture</small></span>
		</button>
	{/if}


</section>

<style>
.capture-page { display: flex; flex-direction: column; min-height: 100%; }
.recorder-core { position: relative; overflow: hidden; padding: 14px 12px 0; background: rgba(0,0,0,0.26); border-radius: 3px 3px 0 0; }
.recorder-core::before { content: ''; position: absolute; inset: 0 auto 0 0; width: 2px; background: #534b43; }
.recorder-core.active::before { background: var(--red); box-shadow: 0 0 16px var(--red); }
.meter { height: 40px; display: flex; align-items: center; justify-content: center; gap: 5px; padding: 0 8px; }
.meter i { width: 3px; height: calc(var(--bar-height) * 0.42); background: #4c4640; border-radius: 1px; transform-origin: center; }
.active .meter i { background: linear-gradient(to top, var(--red), var(--brass)); animation: signal 780ms ease-in-out infinite alternate; animation-delay: var(--delay); box-shadow: 0 0 7px rgba(213, 45, 36, 0.25); }
.timer { margin-top: 2px; text-align: center; font: 300 36px/1 "SFMono-Regular", Consolas, monospace; font-variant-numeric: tabular-nums; letter-spacing: 0.08em; color: var(--bone); }
.capture-meta { display: flex; justify-content: space-between; gap: 8px; margin-top: 5px; padding: 8px 12px 10px; font-size: 10px; color: #8d847a; }
.capture-fields { flex: 1; display: grid; gap: 12px; align-content: start; padding: 14px 12px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.title-field { display: block; }
.tags-field { display: block; }
.tags-field.disabled { opacity: 0.55; }
.tags-field .field-label small { margin-left: 6px; color: #6f685f; font-weight: 500; }
.record-control { width: 100%; margin-top: auto; min-height: 48px; display: grid; grid-template-columns: 36px 1fr; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid var(--red); border-radius: 3px; background: linear-gradient(105deg, #7f1715, #c72b23 72%, #e34737); color: white; text-align: left; cursor: pointer; box-shadow: 0 8px 24px rgba(111, 23, 21, 0.25), inset 0 1px rgba(255,255,255,0.17); transition: transform 120ms ease, filter 120ms ease; }
.record-control:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
.record-control.stop { background: rgba(213, 45, 36, 0.08); color: #ff8b7c; box-shadow: inset 0 0 18px rgba(213, 45, 36, 0.06); }
.control-symbol { width: 30px; height: 30px; display: grid; place-items: center; border: 1px solid rgba(255,255,255,0.42); border-radius: 50%; line-height: 0; }
.record-control strong, .record-control small { display: block; }
.record-control strong { font-size: 14px; letter-spacing: 0.01em; }
.record-control small { margin-top: 4px; font-size: 10px; opacity: 0.74; }
.upload-pending { grid-template-columns: 1fr auto; align-items: center; }
.retry-upload { align-self: center; min-height: 34px; padding: 0 14px; border: 1px solid var(--brass); border-radius: 3px; background: transparent; color: var(--brass); font-size: 11px; font-weight: 700; cursor: pointer; transition: background 120ms ease, color 120ms ease; }
.retry-upload:hover:not(:disabled) { background: var(--brass); color: #17110b; }
@keyframes signal { to { height: var(--bar-height); } }
</style>
