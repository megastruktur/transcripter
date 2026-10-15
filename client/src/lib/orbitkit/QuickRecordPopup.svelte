<script lang="ts">
	import { onMount } from 'svelte';
	import { closePopup } from '@orbitkit/ui';
	import { commands } from '$lib/tauri';
	import type { AudioDeviceInfo, PreFlightReport } from '$lib/tauri';
	import Icon from '$lib/Icon.svelte';
	import TagChips from '$lib/TagChips.svelte';
	import { loadApiConfig } from '$lib/api.svelte';
	import { ANDROID_MIC_ID, isAndroidTauri } from '$lib/mobile-recorder';
	import { recorder, startRecording, stopRecording, syncRecordingState } from '$lib/stores.svelte';
	import {
		buildRecordingTitle,
		getActivePreset,
		loadPresets,
		presetState,
		selectPreset
	} from '$lib/presets.svelte';
	import { ensureProfiles, profilesCache } from '$lib/profiles.svelte';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';
	import { mergeDraftTags } from '$lib/tags';

	// Android has one system microphone and no desktop capture stack: the
	// device selectors and preflight panel are desktop-only UI.
	const android = isAndroidTauri();

	// ── Audio devices (desktop) ───────────────────────────────────────────────
	let microphones = $state<AudioDeviceInfo[]>([]);
	let systemOutputs = $state<AudioDeviceInfo[]>([]);
	let selectedMic = $state<string | null>(null);
	let selectedSystemOutput = $state<string | null>(null);
	let captureSystem = $state(false);
	let devicesError = $state('');

	// ── Preflight (desktop) ───────────────────────────────────────────────────
	let preflightReport = $state<PreFlightReport | null>(null);
	let preflightLoading = $state(false);
	let preflightError = $state('');

	// ── Presets + extra tags ──────────────────────────────────────────────────
	let extraTags = $state<string[]>([]);
	let tagDraft = $state('');

	// ── Capture state ─────────────────────────────────────────────────────────
	let engaging = $state(false);
	let engageError = $state('');

	const activePreset = $derived(getActivePreset());
	const tagSuggestions = $derived(tagSuggestionsCache.items);
	const CAPTURE_RATE = 48_000;
	const elapsed = $derived(Math.floor(recorder.frames / CAPTURE_RATE));
	function fmt(sec: number): string {
		const h = Math.floor(sec / 3600);
		const m = Math.floor((sec % 3600) / 60);
		const s = sec % 60;
		return h ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	function presetTypeLabel(recType: string): string {
		return profilesCache.items.find((profile) => profile.type === recType)?.display_name ?? recType;
	}

	onMount(async () => {
		// This popup is its own webview window (own module instances): seed
		// the preset set here too, and probe for a session another window
		// already started so Stop is offered instead of a second start.
		loadPresets();
		if (android) return;
		void syncRecordingState();
		void ensureProfiles(loadApiConfig());
		void ensureTagSuggestions(loadApiConfig());
		await loadDevices();
		await runPreflight();
	});

	async function loadDevices() {
		devicesError = '';
		try {
			const devs = await commands.listAudioDevices();
			microphones = devs.microphones;
			systemOutputs = devs.system_outputs;
			// Select defaults
			if (devs.default_microphone && !selectedMic) {
				selectedMic = devs.default_microphone;
			}
			if (devs.default_system_output && !selectedSystemOutput) {
				selectedSystemOutput = devs.default_system_output;
			}
		} catch (e) {
			devicesError = String(e);
		}
	}

	async function runPreflight() {
		if (!selectedMic) return;
		preflightLoading = true;
		preflightError = '';
		preflightReport = null;
		try {
			preflightReport = await commands.preFlight(
				true,
				selectedMic,
				captureSystem ? selectedSystemOutput : null,
				captureSystem
			);
		} catch (e) {
			preflightError = String(e);
		} finally {
			preflightLoading = false;
		}
	}

	// Derived audio readiness from preflight
	const audioReady = $derived(
		preflightReport !== null &&
		!preflightReport.error &&
		preflightReport.mic_state === 'ready' &&
		(!captureSystem || captureSystem && preflightReport.system_state === 'ready')
	);

	const preflightLabel = $derived.by(() => {
		if (preflightLoading) return 'Checking audio…';
		if (preflightError) return `Error: ${preflightError}`;
		if (!preflightReport) return 'Not checked';
		if (preflightReport.error) return preflightReport.error;
		if (preflightReport.mic_state === 'ready') {
			if (captureSystem && preflightReport.system_state !== 'ready') return 'System audio needs attention';
			return 'Audio ready';
		}
		return 'Audio needs attention';
	});

	const preflightTone = $derived.by<'idle' | 'issue' | 'ready'>(() => {
		if (preflightLoading) return 'idle';
		if (preflightError || !preflightReport) return 'idle';
		if (preflightReport.error) return 'issue';
		if (preflightReport.mic_state === 'ready' && (!captureSystem || preflightReport.system_state === 'ready')) return 'ready';
		return 'issue';
	});

	async function engageCapture() {
		engageError = '';
		engaging = true;
		const preset = getActivePreset();
		// Preset tags first, popup extras on top; the in-progress draft is
		// flushed so a chip the user did not press Enter for is not dropped.
		const tags = mergeDraftTags([...preset.tags, ...extraTags], tagDraft);
		try {
			await startRecording(
				buildRecordingTitle('', preset),
				tags,
				android ? ANDROID_MIC_ID : selectedMic,
				android || !captureSystem ? null : selectedSystemOutput,
				!android && captureSystem,
				preset.recType || null
			);
			// startRecording reports blockers (denied prompt, no mic) as store
			// warnings instead of throws: only close when a session is live.
			if (!recorder.recording) {
				engageError = recorder.warnings[recorder.warnings.length - 1] ?? 'could not start recording';
				return;
			}
			await closePopup('orbitkit-popup-quick_record');
		} catch (e) {
			engageError = String(e);
		} finally {
			engaging = false;
		}
	}

	async function stopAndClose() {
		engageError = '';
		try {
			await stopRecording();
			await closePopup('orbitkit-popup-quick_record');
		} catch (e) {
			engageError = String(e);
		}
	}

	async function handleClose() {
		await closePopup('orbitkit-popup-quick_record');
	}
</script>

<div class="popup" role="dialog" aria-modal="true" aria-label="Quick Record">
	<header class="popup-header">
		<span class="popup-title">Quick Record</span>
		<button
			type="button"
			class="close-btn"
			aria-label="Close"
			title="Close"
			onclick={handleClose}
		>
			<Icon name="close" size={14} />
		</button>
	</header>

	<div class="hazard-rule" aria-hidden="true"></div>

	<div class="popup-body">
		{#if recorder.recording}
			<div class="recording-line" role="status">
				<span class="rec-dot" aria-hidden="true"></span>
				<span class="recording-text">Recording</span>
				<span class="recording-time">{fmt(elapsed)}</span>
			</div>
			<p class="recording-hint">
				The session keeps running when this popup closes — stop here or from the Record page.
			</p>
		{:else}
			<!-- Named presets -->
			<div class="field">
				<span class="field-label">Preset</span>
				<div class="preset-grid" role="listbox" aria-label="Capture presets">
					{#each presetState.presets as preset (preset.id)}
						<button
							type="button"
							role="option"
							aria-selected={preset.id === activePreset.id}
							class="preset-chip"
							class:selected={preset.id === activePreset.id}
							onclick={() => selectPreset(preset.id)}
						>
							<span class="preset-name">{preset.name}</span>
							{#if preset.tags.length}
								<span class="preset-tags">
									{#each preset.tags as tag (tag)}<span>#{tag}</span>{/each}
								</span>
							{/if}
						</button>
					{/each}
				</div>
			</div>

			<div class="active-preset-line">
				<span>{buildRecordingTitle('', activePreset)}</span>
				{#if activePreset.recType}
					<span class="active-preset-type">{presetTypeLabel(activePreset.recType)}</span>
				{/if}
			</div>

			<!-- Extra tags on top of the preset -->
			<div class="field">
				<span class="field-label">Extra tags</span>
				<TagChips
					tags={extraTags}
					bind:draft={tagDraft}
					suggestions={tagSuggestions}
					placeholder="e.g. follow-up"
					onChange={(next) => (extraTags = next)}
				/>
				<span class="field-hint">Added on top of “{activePreset.name}” tags</span>
			</div>

			{#if android}
				<div class="preflight-panel">
					<div class="preflight-lamp-row">
						<span class="lamp" aria-hidden="true"></span>
						<span class="preflight-label">System microphone · keeps recording in background</span>
					</div>
				</div>
			{:else}
				<!-- Microphone select -->
				<div class="field">
					<label class="field-label" for="mic-select">Microphone</label>
					<div class="select-wrap">
						<select
							id="mic-select"
							class="select"
							bind:value={selectedMic}
							onchange={runPreflight}
						>
							{#if microphones.length === 0}
								<option value="">No microphones found</option>
							{/if}
							{#each microphones as mic (mic.id)}
								<option value={mic.id}>{mic.label || mic.id}</option>
							{/each}
						</select>
						<span class="select-arrow" aria-hidden="true">▾</span>
					</div>
				</div>

				<!-- System output select -->
				<div class="field">
					<label class="field-label" for="system-select">System Output</label>
					<div class="select-wrap">
						<select
							id="system-select"
							class="select"
							bind:value={selectedSystemOutput}
							disabled={!captureSystem}
							onchange={runPreflight}
						>
							<option value="">None</option>
							{#each systemOutputs as out (out.id)}
								<option value={out.id}>{out.label || out.id}</option>
							{/each}
						</select>
						<span class="select-arrow" aria-hidden="true">▾</span>
					</div>
				</div>

				<!-- System capture checkbox -->
				<label class="checkbox-row">
					<input
						type="checkbox"
						class="checkbox"
						bind:checked={captureSystem}
						onchange={runPreflight}
					/>
					<span class="checkbox-label">Capture system audio</span>
				</label>

				<!-- Preflight status / VU meter -->
				<div class="preflight-panel">
					<div class="preflight-lamp-row">
						<span
							class="lamp"
							class:lamp--ready={preflightTone === 'ready'}
							class:lamp--issue={preflightTone === 'issue'}
							aria-hidden="true"
						></span>
						<span class="preflight-label">{preflightLabel}</span>
					</div>
					{#if preflightReport}
						<div class="preflight-details">
							<span class="detail-item">Mic: <strong class={preflightReport.mic_state === 'ready' ? 'text--ready' : 'text--issue'}>{preflightReport.mic_state}</strong></span>
							{#if captureSystem}
								<span class="detail-item">System: <strong class={preflightReport.system_state === 'ready' ? 'text--ready' : 'text--issue'}>{preflightReport.system_state}</strong></span>
							{/if}
						</div>
						<span class="signal-line" class:signal--detected={preflightReport.mic_signal} class:signal--none={!preflightReport.mic_signal}>
							{preflightReport.mic_signal ? 'Signal detected' : 'No signal'}
						</span>
					{/if}
				</div>
			{/if}
		{/if}
	</div>

	{#if engageError}
		<div class="engage-error" role="alert">{engageError}</div>
	{/if}

	<footer class="popup-footer">
		{#if recorder.recording}
			<button
				type="button"
				class="engage-btn engage-stop"
				disabled={recorder.stopping}
				onclick={stopAndClose}
			>
				{recorder.stopping ? 'Stopping…' : 'Stop Recording'}
			</button>
		{:else}
			<button
				type="button"
				class="engage-btn"
				disabled={(android ? false : !audioReady) || engaging}
				onclick={engageCapture}
			>
				{#if engaging}
					Starting…
				{:else}
					Start Recording
				{/if}
			</button>
		{/if}
	</footer>
</div>

<style>
	.popup {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: #160f0d;
		color: #e9dfcf;
		font-family: Inter, ui-sans-serif, system-ui, sans-serif;
		font-size: 13px;
		overflow: hidden;
	}

	/* ── Header ─────────────────────────────────────────────────────────────── */
	.popup-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 12px 8px;
		flex-shrink: 0;
	}

	.popup-title {
		font-size: 15px;
		font-weight: 600;
		letter-spacing: 0.04em;
		color: #d7a747;
		text-transform: uppercase;
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: 1px solid rgba(231, 214, 190, 0.14);
		border-radius: 2px;
		color: #9e9183;
		cursor: pointer;
		transition: color 120ms, border-color 120ms;
	}
	.close-btn:hover {
		color: #e9dfcf;
		border-color: rgba(231, 214, 190, 0.4);
	}
	.close-btn:focus-visible {
		outline: 2px solid #70d7d0;
		outline-offset: 1px;
	}

	/* ── Hazard rule ────────────────────────────────────────────────────────── */
	.hazard-rule {
		height: 1px;
		background: repeating-linear-gradient(
			90deg,
			#d7a747 0px,
			#d7a747 6px,
			transparent 6px,
			transparent 10px
		);
		opacity: 0.4;
		flex-shrink: 0;
	}

	/* ── Body ──────────────────────────────────────────────────────────────── */
	.popup-body {
		flex: 1;
		overflow-y: auto;
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	/* ── Field ────────────────────────────────────────────────────────────── */
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.field-label {
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: #9e9183;
	}

	.select-wrap {
		position: relative;
		display: flex;
		align-items: center;
	}

	.select {
		appearance: none;
		width: 100%;
		height: 38px;
		padding: 0 32px 0 10px;
		background: #2a1d17;
		border: 1px solid rgba(231, 214, 190, 0.14);
		border-radius: 2px;
		color: #e9dfcf;
		font-family: inherit;
		font-size: 13px;
		cursor: pointer;
		transition: border-color 120ms;
	}
	.select:focus {
		outline: none;
		border-color: #d7a747;
	}
	.select:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.select-arrow {
		position: absolute;
		right: 8px;
		pointer-events: none;
		color: #9e9183;
	}

	/* ── Checkbox ──────────────────────────────────────────────────────────── */
	.checkbox-row {
		display: flex;
		align-items: center;
		gap: 8px;
		cursor: pointer;
		height: 32px;
	}

	.checkbox {
		appearance: none;
		width: 16px;
		height: 16px;
		border: 1px solid rgba(231, 214, 190, 0.3);
		border-radius: 2px;
		background: #2a1d17;
		cursor: pointer;
		flex-shrink: 0;
		position: relative;
		transition: border-color 120ms, background 120ms;
	}
	.checkbox:checked {
		background: #d7a747;
		border-color: #d7a747;
	}
	.checkbox:checked::after {
		content: '';
		position: absolute;
		left: 3px;
		top: 1px;
		width: 5px;
		height: 8px;
		border: 2px solid #160f0d;
		border-top: none;
		border-left: none;
		transform: rotate(45deg);
	}
	.checkbox:focus-visible {
		outline: 2px solid #70d7d0;
		outline-offset: 2px;
	}

	.checkbox-label {
		color: #e9dfcf;
		font-size: 13px;
	}

	/* ── Preflight panel ──────────────────────────────────────────────────── */
	.preflight-panel {
		background: #2a1d17;
		border: 1px solid rgba(231, 214, 190, 0.1);
		border-radius: 2px;
		padding: 8px 10px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.preflight-lamp-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.lamp {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: #9e9183;
		flex-shrink: 0;
		transition: background 200ms;
	}
	.lamp--ready {
		background: #70d7d0;
		box-shadow: 0 0 4px #70d7d0;
	}
	.lamp--issue {
		background: #d52d24;
		box-shadow: 0 0 4px #d52d24;
	}

	.preflight-label {
		font-size: 12px;
		color: #9e9183;
	}

	.preflight-details {
		display: flex;
		gap: 12px;
	}

	.detail-item {
		font-size: 11px;
		color: #9e9183;
		font-family: ui-monospace, monospace;
	}

	.text--ready {
		color: #70d7d0;
	}
	.text--issue {
		color: #d52d24;
	}

	.signal-line {
		font-size: 11px;
		font-family: ui-monospace, monospace;
	}
	.signal--detected {
		color: #70d7d0;
	}
	.signal--none {
		color: #9e9183;
	}

	/* ── Footer ───────────────────────────────────────────────────────────── */
	.field-hint {
		font-size: 10px;
		color: #9e9183;
		letter-spacing: 0.04em;
	}

	/* ── Footer ───────────────────────────────────────────────────────────── */
	.popup-footer {
		padding: 10px 12px;
		border-top: 1px solid rgba(231, 214, 190, 0.1);
		flex-shrink: 0;
	}

	.engage-btn {
		width: 100%;
		height: 42px;
		background: #d52d24;
		border: 1px solid #6f1715;
		border-radius: 2px;
		color: #e9dfcf;
		font-family: inherit;
		font-size: 14px;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		cursor: pointer;
		transition: background 120ms, opacity 120ms;
	}
	.engage-btn:hover:not(:disabled) {
		background: #c02820;
	}
	.engage-btn:focus-visible {
		outline: 2px solid #70d7d0;
		outline-offset: 2px;
	}
	.engage-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* ── Named presets ─────────────────────────────────────────────────────── */
	.preset-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
		gap: 6px;
	}

	.preset-chip {
		display: grid;
		gap: 3px;
		justify-items: start;
		padding: 7px 9px;
		background: #2a1d17;
		border: 1px solid rgba(231, 214, 190, 0.14);
		border-radius: 2px;
		color: #e9dfcf;
		text-align: left;
		cursor: pointer;
		transition: border-color 120ms, background 120ms;
	}
	.preset-chip:hover {
		border-color: rgba(231, 214, 190, 0.4);
	}
	.preset-chip.selected {
		border-color: #d7a747;
		background: rgba(215, 167, 71, 0.1);
	}

	.preset-name {
		font-size: 12px;
		font-weight: 600;
	}

	.preset-tags {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		font-size: 10px;
		color: #9e9183;
	}

	.active-preset-line {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
		font-size: 11px;
		color: #9e9183;
	}
	.active-preset-type {
		color: #d7a747;
	}

	/* ── Live recording state ─────────────────────────────────────────────── */
	.recording-line {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px;
		background: #2a1d17;
		border: 1px solid rgba(213, 45, 36, 0.4);
		border-radius: 2px;
	}
	.rec-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: #d52d24;
		box-shadow: 0 0 6px #d52d24;
		animation: rec-pulse 1.4s ease-in-out infinite;
		flex-shrink: 0;
	}
	.recording-text {
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: #ff8b7c;
	}
	.recording-time {
		margin-left: auto;
		font-family: ui-monospace, monospace;
		font-variant-numeric: tabular-nums;
		font-size: 13px;
		color: #e9dfcf;
	}
	.recording-hint {
		margin: 0;
		font-size: 11px;
		line-height: 1.5;
		color: #9e9183;
	}
	@keyframes rec-pulse {
		50% {
			opacity: 0.35;
		}
	}

	.engage-stop {
		background: transparent;
		border-color: #d52d24;
		color: #ff8b7c;
	}
	.engage-stop:hover:not(:disabled) {
		background: rgba(213, 45, 36, 0.14);
	}

	/* ── Error ────────────────────────────────────────────────────────────── */
	.engage-error {
		padding: 6px 12px;
		background: rgba(213, 45, 36, 0.12);
		border-top: 1px solid rgba(213, 45, 36, 0.3);
		color: #d52d24;
		font-size: 12px;
		flex-shrink: 0;
	}
</style>
