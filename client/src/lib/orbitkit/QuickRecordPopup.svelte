<script lang="ts">
	import { onMount } from 'svelte';
	import { closePopup } from '@orbitkit/ui';
	import { commands } from '$lib/tauri';
	import type { AudioDeviceInfo, PreFlightReport } from '$lib/tauri';
	import Icon from '$lib/Icon.svelte';

	// ── Audio devices ──────────────────────────────────────────────────────────
	let microphones = $state<AudioDeviceInfo[]>([]);
	let systemOutputs = $state<AudioDeviceInfo[]>([]);
	let selectedMic = $state<string | null>(null);
	let selectedSystemOutput = $state<string | null>(null);
	let captureSystem = $state(false);
	let devicesError = $state('');

	// ── Preflight ──────────────────────────────────────────────────────────────
	let preflightReport = $state<PreFlightReport | null>(null);
	let preflightLoading = $state(false);
	let preflightError = $state('');

	// ── Capture draft ─────────────────────────────────────────────────────────
	let title = $state('');
	let tags = $state('');
	let engaging = $state(false);
	let engageError = $state('');

	onMount(async () => {
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
		if (!audioReady) return;
		engageError = '';
		engaging = true;
		try {
			const tagList = tags
				.split(',')
				.map((t) => t.trim())
				.filter(Boolean);
			await commands.startRecording(
				title || null,
				tagList.length ? tagList : null,
				selectedMic,
				captureSystem ? selectedSystemOutput : null,
				captureSystem
			);
			await closePopup('orbitkit-popup-quick_record');
		} catch (e) {
			engageError = String(e);
		} finally {
			engaging = false;
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

		<!-- Title input -->
		<div class="field">
			<label class="field-label" for="rec-title">Recording Title</label>
			<input
				id="rec-title"
				type="text"
				class="input"
				placeholder="Call with Alex · 2025-01-15"
				bind:value={title}
				autocomplete="off"
			/>
		</div>

		<!-- Tags input -->
		<div class="field">
			<label class="field-label" for="rec-tags">Tags</label>
			<input
				id="rec-tags"
				type="text"
				class="input"
				placeholder="client, follow-up"
				bind:value={tags}
				autocomplete="off"
			/>
			<span class="field-hint">Comma-separated</span>
		</div>
	</div>

	{#if engageError}
		<div class="engage-error" role="alert">{engageError}</div>
	{/if}

	<footer class="popup-footer">
		<button
			type="button"
			class="engage-btn"
			disabled={!audioReady || engaging}
			onclick={engageCapture}
		>
			{#if engaging}
				Engaging…
			{:else}
				Engage Capture
			{/if}
		</button>
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
	.input {
		height: 38px;
		padding: 0 10px;
		background: #2a1d17;
		border: 1px solid rgba(231, 214, 190, 0.14);
		border-radius: 2px;
		color: #e9dfcf;
		font-family: inherit;
		font-size: 13px;
		transition: border-color 120ms;
	}
	.input::placeholder {
		color: #9e9183;
	}
	.input:focus {
		outline: none;
		border-color: #d7a747;
	}

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
