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
	import { mergeDraftTags } from '$lib/tags';
	import { ensureProfiles, profilesCache } from '$lib/profiles.svelte';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';
	import { isAndroidTauri, startMobileRecorder } from '$lib/mobile-recorder';
	import type { MobileRecorder } from '$lib/mobile-recorder';

	// Mirrors CAPTURE_RATE in src-tauri/src/capture.rs; recorder.frames is the
	// session's written-frame count, so elapsed time survives window collapse.
	const CAPTURE_RATE = 48_000;
	const IMPORT_ACCEPT = '.flac,.wav,.mp3,audio/flac,audio/wav,audio/x-wav,audio/mpeg';
	const IMPORT_SIZE_HINT = 500 * 1024 * 1024;
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
	/** The type hint shows which profile the selected type routes to. */
	const matchedProfile = $derived(
		recType === null ? null : profilesCache.items.find((profile) => profile.type === recType) ?? null
	);

	// ---- Import surface -------------------------------------------------
	let importInput = $state<HTMLInputElement | null>(null);
	/** Non-null while the import form is open for a picked file. */
	let importFile = $state<File | null>(null);
	let importTitle = $state('');
	/** datetime-local value (local time, minutes precision). */
	let importWhen = $state('');
	let importType = $state<string | null>(null);
	let importTagDraft = $state('');
	let importTags = $state<string[]>([]);
	let importing = $state(false);
	let importError = $state('');
	const importOversize = $derived(importFile !== null && importFile.size > IMPORT_SIZE_HINT);

	function openImportPicker(): void {
		importInput?.click();
	}

	function onImportPicked(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		input.value = '';
		if (!file) return;
		importFile = file;
		// Default the meta form from the file: name without extension as a
		// title seed, "now" as the recorded time, the recorder's current
		// type selection. The user edits before confirming.
		importTitle = title || file.name.replace(/\.[^.]+$/, '');
		const now = new Date();
		now.setSeconds(0, 0);
		importWhen = toLocalDatetimeValue(now);
		importType = recType;
		importTagDraft = '';
		importTags = [...tags];
		importError = '';
	}

	function cancelImport(): void {
		importFile = null;
		importError = '';
	}

	function toLocalDatetimeValue(date: Date): string {
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
	}

	async function confirmImport(): Promise<void> {
		const file = importFile;
		if (!file || importing) return;
		importing = true;
		importError = '';
		const formTags = mergeDraftTags(importTags, importTagDraft);
		// datetime-local carries no timezone: interpret it in the user's
		// local zone, exactly like a native picker would.
		const when = importWhen ? new Date(importWhen) : null;
		try {
			const result = await uploadDirect(loadApiConfig(), file, importTitle.trim() || null, formTags, null, {
				type: importType ?? undefined,
				recordedAt: when !== null && !Number.isNaN(when.getTime()) ? when.toISOString() : undefined
			});
			importFile = null;
			recorder.warnings.push(`import queued for processing (${result.id.slice(0, 8)}…)`);
		} catch (error) {
			importError = String(error instanceof Error ? error.message : error);
		} finally {
			importing = false;
		}
	}

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
	<header>
		<h1 class="page-title">Record audio</h1>
	</header>

	{#each recorder.warnings as warning (warning)}
		<div class="notice warning" role="status"><strong>Signal warning</strong><span>{warning}</span></div>
	{/each}

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

	<div class:active={recorder.recording} class="recorder-core panel">
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

		<label class="type-field">
			<span class="field-label">Type</span>
			<select bind:value={recType} disabled={recorder.recording}>
				<option value={null}>None — default pipeline</option>
				{#each profilesCache.items as profile (profile.id)}
					<option value={profile.type}>{profile.display_name}</option>
				{/each}
			</select>
			{#if matchedProfile}
				<small class="type-hint">Profile: {matchedProfile.display_name}{matchedProfile.has_enrich ? ' · memory extraction on' : ''}</small>
			{/if}
		</label>

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

		{#if recorder.recording}
			<button class="record-control stop" type="button" disabled={recorder.stopping} onclick={() => android ? stopMobileRecording() : stopRecording()}>
				<span class="control-symbol" aria-hidden="true"><Icon name="stop" size={12} /></span>
				<span><strong>{recorder.stopping ? 'Sealing archive…' : 'Stop recording'}</strong><small>Finish and send for processing</small></span>
			</button>
		{:else}
			<div class="capture-actions">
				<button class="record-control start" type="button" disabled={starting} onclick={beginRecording}>
					<span class="control-symbol" aria-hidden="true"><Icon name="dot" size={12} /></span>
					<span><strong>{starting ? 'Running pre-flight…' : 'Start recording'}</strong><small>Checks devices before capture</small></span>
				</button>
				<button class="import-button" type="button" disabled={starting} onclick={openImportPicker} title="Import a finished audio file">
					<Icon name="import" size={16} strokeWidth={1.5} />
					<span>Import audio</span>
				</button>
			</div>
			<input
				type="file"
				accept={IMPORT_ACCEPT}
				class="visually-hidden"
				bind:this={importInput}
				onchange={onImportPicked}
				aria-hidden="true"
				tabindex={-1}
			/>
		{/if}
	</div>

	{#if importFile}
		<div class="import-sheet panel" role="dialog" aria-label="Import audio">
			<header class="import-head">
				<Icon name="import" size={16} strokeWidth={1.5} />
				<span class="import-filename" title={importFile.name}>{importFile.name}</span>
				<span class="import-size">{(importFile.size / 1_000_000).toFixed(1)} MB</span>
			</header>
			<label class="title-field">
				<span class="field-label">Recording name</span>
				<input type="text" placeholder="e.g. Doctronic weekly" bind:value={importTitle} />
			</label>
			<label class="type-field">
				<span class="field-label">Type</span>
				<select bind:value={importType}>
					<option value={null}>None — default pipeline</option>
					{#each profilesCache.items as profile (profile.id)}
						<option value={profile.type}>{profile.display_name}</option>
					{/each}
				</select>
			</label>
			<label class="title-field">
				<span class="field-label">Recorded at</span>
				<input type="datetime-local" bind:value={importWhen} />
			</label>
			<label class="tags-field">
				<span class="field-label">Tags <small>Press Enter or comma to add</small></span>
				<TagChips
					tags={importTags}
					bind:draft={importTagDraft}
					suggestions={tagSuggestions}
					placeholder="e.g. doctronic, personal"
					onChange={(next) => (importTags = next)}
				/>
			</label>
			{#if importOversize}
				<p class="import-hint">Large file — converting to FLAC or MP3 first makes the upload noticeably faster.</p>
			{/if}
			{#if importError}
				<p class="inline-error" role="alert">{importError}</p>
			{/if}
			<footer class="import-actions">
				<button type="button" class="secondary" onclick={cancelImport} disabled={importing}>Cancel</button>
				<button type="button" class="primary" onclick={() => void confirmImport()} disabled={importing}>
					{importing ? 'Uploading…' : 'Import'}
				</button>
			</footer>
		</div>
	{/if}
</section>

<style>
	.capture-page { display: flex; flex-direction: column; gap: 10px; }
	/* Android: the record page is single-purpose, so the panel chrome around
	   the recorder is dissolved (display:contents drops the panel box but
	   keeps child order) and the record button pins to the bottom of the
	   viewport for thumb reach. Desktop keeps the framed instrument panel. */
	:global(.shell--android) .capture-page { min-height: 100%; }
	:global(.shell--android) .recorder-core { display: contents; }
	:global(.shell--android) .recorder-core::before { display: none; }
	:global(.shell--android) .record-control { margin-top: auto; position: sticky; bottom: 10px; z-index: 2; }
	.notice { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--brass); background: rgba(215, 167, 71, 0.07); font-size: 12px; line-height: 1.4; }
	.notice strong { font-size: 10px; font-weight: 700; color: var(--brass); }
	.upload-pending { grid-template-columns: 1fr auto; align-items: center; }
	.upload-pending strong { grid-column: 1 / -1; }
	.retry-upload { padding: 6px 10px; border: 1px solid var(--brass); border-radius: 2px; background: transparent; color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.retry-upload:hover:not(:disabled) { background: rgba(215, 167, 71, 0.12); }
	.retry-upload:disabled { opacity: 0.6; cursor: default; }
	.recorder-core { padding: 12px; box-shadow: inset 0 1px rgba(255,255,255,0.025); position: relative; overflow: hidden; }
	.recorder-core::before { content: ''; position: absolute; inset: 0 auto 0 0; width: 2px; background: #534b43; }
	.recorder-core.active::before { background: var(--red); box-shadow: 0 0 16px var(--red); }
	.meter { height: 40px; display: flex; align-items: center; justify-content: center; gap: 5px; padding: 0 8px; }
	.meter i { width: 3px; height: calc(var(--bar-height) * 0.42); background: #4c4640; border-radius: 1px; transform-origin: center; }
	.active .meter i { background: linear-gradient(to top, var(--red), var(--brass)); animation: signal 780ms ease-in-out infinite alternate; animation-delay: var(--delay); box-shadow: 0 0 7px rgba(213, 45, 36, 0.25); }
	.timer { margin-top: 2px; text-align: center; font: 300 36px/1 "SFMono-Regular", Consolas, monospace; font-variant-numeric: tabular-nums; letter-spacing: 0.08em; color: var(--bone); }
	.capture-meta { display: flex; justify-content: space-between; gap: 8px; margin: 5px 0 8px; padding-bottom: 8px; border-bottom: 1px solid var(--line); font-size: 10px; color: #8d847a; }
	.type-field { display: block; margin-bottom: 10px; }
	.type-field select { width: 100%; padding: 7px 8px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,0.25); color: var(--bone); font-size: 12px; }
	.type-hint { display: block; margin-top: 4px; font-size: 10px; color: #8d847a; }
	.title-field { display: block; margin-bottom: 10px; }
	.tags-field { display: block; margin-bottom: 10px; }
	.tags-field.disabled { opacity: 0.55; }
	.tags-field .field-label small { margin-left: 6px; color: #6f685f; font-weight: 500; }
	.capture-actions { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: stretch; }
	.import-button { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; min-width: 64px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 3px; background: transparent; color: var(--brass); font-size: 9px; font-weight: 700; letter-spacing: 0.04em; cursor: pointer; }
	.import-button:hover:not(:disabled) { border-color: rgba(215,167,71,.4); background: rgba(215,167,71,0.07); }
	.import-button:disabled { opacity: 0.6; cursor: default; }
	.record-control { width: 100%; min-height: 48px; display: grid; grid-template-columns: 36px 1fr; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid var(--red); border-radius: 3px; background: linear-gradient(105deg, #7f1715, #c72b23 72%, #e34737); color: white; text-align: left; cursor: pointer; box-shadow: 0 8px 24px rgba(111, 23, 21, 0.25), inset 0 1px rgba(255,255,255,0.17); transition: transform 120ms ease, filter 120ms ease; }
	.record-control:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
	.record-control.stop { background: rgba(213, 45, 36, 0.08); color: #ff8b7c; box-shadow: inset 0 0 18px rgba(213, 45, 36, 0.06); }
	.control-symbol { width: 30px; height: 30px; display: grid; place-items: center; border: 1px solid rgba(255,255,255,0.42); border-radius: 50%; line-height: 0; }
	.record-control strong, .record-control small { display: block; }
	.record-control strong { font-size: 14px; letter-spacing: 0.01em; }
	.record-control small { margin-top: 4px; font-size: 10px; opacity: 0.74; }
	@keyframes signal { to { height: var(--bar-height); } }
	.visually-hidden { position: absolute; width: 1px; height: 1px; margin: -1px; clip: rect(0 0 0 0); clip-path: inset(50%); overflow: hidden; white-space: nowrap; }
	.import-sheet { display: flex; flex-direction: column; gap: 10px; padding: 12px; }
	.import-head { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 8px; color: var(--brass); }
	.import-filename { font-size: 12px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--bone); }
	.import-size { font-size: 10px; color: #8d847a; font-variant-numeric: tabular-nums; }
	.import-hint { font-size: 10px; color: var(--brass); }
	.import-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 2px; }
	.import-actions button { min-height: 36px; border-radius: 3px; font-size: 12px; font-weight: 700; cursor: pointer; }
	.import-actions .secondary { border: 1px solid var(--line); background: transparent; color: #8e857b; }
	.import-actions .secondary:hover:not(:disabled) { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.import-actions .primary { border: 1px solid var(--brass); background: rgba(215,167,71,0.12); color: var(--brass); }
	.import-actions .primary:hover:not(:disabled) { background: rgba(215,167,71,0.2); }
	.import-actions button:disabled { opacity: 0.6; cursor: default; }
	.import-sheet :global(.field-label) { display: block; margin-bottom: 4px; }
	.inline-error { color: var(--red); font-size: 12px; }
</style>
