<script lang="ts">
	import { onMount } from 'svelte';
	import {
		audioDevices,
		captureDraft,
		clearWarnings,
		discardMobileUpload,
		ensureAudioDevices,
		mobilePendingUploads,
		recorder,
		retryMobileUpload,
		startRecording,
		stopRecording,
		SYSTEM_AUDIO_OFF
	} from '$lib/stores.svelte';
	import { commands } from '$lib/tauri';
	import { loadApiConfig } from '$lib/api.svelte';
	import Icon from '$lib/Icon.svelte';
	import TagChips from '$lib/TagChips.svelte';
	import SignalWarnings from '$lib/SignalWarnings.svelte';
	import TypeProfileField from '$lib/TypeProfileField.svelte';
	import { mergeDraftTags } from '$lib/tags';
	import { ensureProfiles, profilesCache } from '$lib/profiles.svelte';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';
	import { isAndroidTauri } from '$lib/mobile-recorder';
	import {
		buildRecordingTitle,
		deletePreset,
		getActivePreset,
		loadPresets,
		presetState,
		savePreset,
		selectPreset
	} from '$lib/presets.svelte';
	import type { CapturePreset } from '$lib/presets.svelte';

	// Mirrors CAPTURE_RATE in src-tauri/src/capture.rs; recorder.frames is the
	// session's written-frame count, so elapsed time survives window collapse.
	const CAPTURE_RATE = 48_000;
	let starting = $state(false);
	let android = $state(false);
	const elapsed = $derived(Math.floor(recorder.frames / CAPTURE_RATE));
	function fmt(sec: number): string {
		const h = Math.floor(sec / 3600);
		const m = Math.floor((sec % 3600) / 60);
		const s = sec % 60;
		return h ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	// ── Named presets ──────────────────────────────────────────────────────────
	const activePreset = $derived(getActivePreset());
	const failedUploads = $derived(mobilePendingUploads.filter((u) => u.status === 'failed'));
	const tagSuggestions = $derived(tagSuggestionsCache.items);
	let editorOpen = $state(false);
	/** Preset being edited; null = the editor is creating a new one. */
	let editingId = $state<string | null>(null);
	let presetName = $state('');
	let presetType = $state('');
	let presetTags = $state<string[]>([]);
	let presetTagDraft = $state('');
	/** Collapsible "Custom title & extra tags" section: forced shut while a
	 * recording is live (the fields are disabled for the session anyway). */
	let overridesOpen = $state(false);
	$effect(() => {
		if (recorder.recording) overridesOpen = false;
	});

	function openPresetEditor(mode: 'new' | 'edit'): void {
		if (mode === 'edit') {
			const preset = getActivePreset();
			editingId = preset.id;
			presetName = preset.name;
			presetType = preset.recType;
			presetTags = [...preset.tags];
		} else {
			editingId = null;
			presetName = '';
			presetType = '';
			presetTags = [];
		}
		presetTagDraft = '';
		editorOpen = true;
	}

	function closePresetEditor(): void {
		editorOpen = false;
	}

	function savePresetEditor(): void {
		savePreset({ id: editingId ?? undefined, name: presetName, recType: presetType, tags: presetTags });
		closePresetEditor();
	}

	function deleteActivePreset(): void {
		if (!editingId) return;
		deletePreset(editingId);
		closePresetEditor();
	}

	function presetTypeLabel(preset: CapturePreset): string {
		if (!preset.recType) return '';
		return profilesCache.items.find((profile) => profile.type === preset.recType)?.display_name ?? preset.recType;
	}

	onMount(() => {
		loadPresets();
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
		// NO mobile cleanup on unmount: the capture handle lives in the shared
		// store now, so leaving the page mid-recording keeps it running (the
		// REC pill in the layout bar stays reachable from any page).
	});

	async function beginRecording(): Promise<void> {
		if (recorder.recording || recorder.stopping || recorder.busy) return;
		starting = true;
		clearWarnings();
		// Flush the draft before the recording starts so any in-progress
		// chip the user did not press Enter for is not silently dropped.
		captureDraft.tags = mergeDraftTags(captureDraft.tags, captureDraft.tagDraft);
		captureDraft.tagDraft = '';
		// Title: the custom name wins, otherwise `<preset> · <date>`; the
		// one-off type override falls back to the preset's type.
		const recType = captureDraft.recType || activePreset.recType || null;
		try {
			await startRecording(
				buildRecordingTitle(captureDraft.title, activePreset),
				captureDraft.tags,
				android ? null : audioDevices.selectedMicrophone,
				android ? null : audioDevices.selectedSystemOutput === SYSTEM_AUDIO_OFF ? null : audioDevices.selectedSystemOutput,
				!android && audioDevices.selectedSystemOutput !== SYSTEM_AUDIO_OFF,
				recType
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
	<SignalWarnings />

	{#if failedUploads.length}
		<div class="notice warning upload-pending" role="status">
			<strong>Upload failed</strong>
			<span>
				{failedUploads.length === 1 ? 'One recording' : `${failedUploads.length} recordings`} waiting for the server —
				<a class="upload-link" href="/recordings">open Library</a> to retry later
			</span>
			<div class="upload-actions">
				<button
					type="button"
					class="retry-upload"
					onclick={() => failedUploads.forEach((u) => void retryMobileUpload(u.id))}
				>
					Retry
				</button>
				<button
					type="button"
					class="upload-dismiss"
					aria-label="Discard failed recordings"
					title="Discard"
					onclick={() => failedUploads.forEach((u) => discardMobileUpload(u.id))}
				>
					<Icon name="close" size={11} />
				</button>
			</div>
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
		<div class="presets-block">
			<div class="presets-header">
				<span class="field-label">Presets</span>
				<span class="presets-actions">
					<button type="button" class="ghost-btn" disabled={recorder.recording} onclick={() => openPresetEditor('new')}>+ Preset</button>
					<button type="button" class="ghost-btn" disabled={recorder.recording} onclick={() => openPresetEditor('edit')}>Edit</button>
				</span>
			</div>
			<div class="preset-grid" role="listbox" aria-label="Capture presets">
				{#each presetState.presets as preset (preset.id)}
					<button
						type="button"
						role="option"
						aria-selected={preset.id === activePreset.id}
						class="preset-chip"
						class:selected={preset.id === activePreset.id}
						disabled={recorder.recording}
						onclick={() => selectPreset(preset.id)}
					>
						<span class="preset-name">{preset.name}</span>
						{#if preset.recType}
							<span class="preset-type">{presetTypeLabel(preset)}</span>
						{/if}
						{#if preset.tags.length}
							<span class="preset-tags">
								{#each preset.tags as tag (tag)}<span>#{tag}</span>{/each}
							</span>
						{/if}
					</button>
				{/each}
			</div>
			{#if editorOpen}
				<div class="preset-editor">
					<label class="editor-field">
						<span class="field-label">Preset name</span>
						<input type="text" placeholder="e.g. Standup" bind:value={presetName} />
					</label>
					<label class="editor-field">
						<span class="field-label">Type</span>
						<select bind:value={presetType}>
							<option value="">Default pipeline</option>
							{#each profilesCache.items as profile (profile.id)}
								<option value={profile.type}>{profile.display_name}</option>
							{/each}
						</select>
					</label>
					<label class="editor-field">
						<span class="field-label">Tags <small>Press Enter or comma to add</small></span>
						<TagChips
							tags={presetTags}
							bind:draft={presetTagDraft}
							suggestions={tagSuggestions}
							placeholder="e.g. dnd, standup"
							onChange={(next) => (presetTags = next)}
						/>
					</label>
					<div class="editor-actions">
						<button type="button" class="editor-save" disabled={!presetName.trim()} onclick={savePresetEditor}>Save</button>
						{#if editingId}
							<button type="button" class="editor-delete" onclick={deleteActivePreset}>Delete</button>
						{/if}
						<button type="button" class="editor-cancel" onclick={closePresetEditor}>Cancel</button>
					</div>
				</div>
			{/if}
		</div>

		<details class="custom-overrides" class:locked={recorder.recording} bind:open={overridesOpen}>
			<summary>Custom title &amp; extra tags</summary>
			<div class="overrides-body">
				<label class="title-field">
					<span class="field-label">Recording name</span>
					<input
						type="text"
						placeholder={`Auto: ${buildRecordingTitle('', activePreset)}`}
						bind:value={captureDraft.title}
						disabled={recorder.recording}
					/>
				</label>
				<label class="type-field">
					<span class="field-label">Type <small>one-off override</small></span>
					<TypeProfileField bind:value={captureDraft.recType} disabled={recorder.recording} />
				</label>
				<label class="tags-field">
					<span class="field-label">Extra tags <small>Press Enter or comma to add</small></span>
					<TagChips
						tags={captureDraft.tags}
						bind:draft={captureDraft.tagDraft}
						disabled={recorder.recording}
						suggestions={tagSuggestions}
						placeholder="e.g. doctronic, dnd dark castle"
						onChange={(next) => (captureDraft.tags = next)}
					/>
				</label>
			</div>
		</details>
	</div>

	{#if recorder.recording}
		<button class="record-control stop" type="button" disabled={recorder.stopping} onclick={() => void stopRecording()}>
			<span class="control-symbol" aria-hidden="true"><Icon name="stop" size={12} /></span>
			<span><strong>{recorder.stopping ? 'Sealing archive…' : 'Stop recording'}</strong><small>Finish and send for processing</small></span>
		</button>
	{:else}
		<button class="record-control start" type="button" disabled={starting || recorder.busy} onclick={beginRecording}>
			<span class="control-symbol" aria-hidden="true"><Icon name="dot" size={12} /></span>
			<span><strong>{starting ? 'Running pre-flight…' : 'Start recording'}</strong><small>Starts with “{activePreset.name}”</small></span>
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
.tags-field .field-label small { margin-left: 6px; color: #6f685f; font-weight: 500; }
.record-control { width: 100%; margin-top: auto; min-height: 48px; display: grid; grid-template-columns: 36px 1fr; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid var(--red); border-radius: 3px; background: linear-gradient(105deg, #7f1715, #c72b23 72%, #e34737); color: white; text-align: left; cursor: pointer; box-shadow: 0 8px 24px rgba(111, 23, 21, 0.25), inset 0 1px rgba(255,255,255,0.17); transition: transform 120ms ease, filter 120ms ease; }
.record-control:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
.record-control.stop { background: rgba(213, 45, 36, 0.08); color: #ff8b7c; box-shadow: inset 0 0 18px rgba(213, 45, 36, 0.06); }
.control-symbol { width: 30px; height: 30px; display: grid; place-items: center; border: 1px solid rgba(255,255,255,0.42); border-radius: 50%; line-height: 0; }
.record-control strong, .record-control small { display: block; }
.record-control strong { font-size: 14px; letter-spacing: 0.01em; }
.record-control small { margin-top: 4px; font-size: 10px; opacity: 0.74; }
.upload-pending { grid-template-columns: 1fr auto; align-items: center; }
.upload-link { color: var(--brass); }
.upload-actions { display: flex; align-items: center; gap: 6px; }
.upload-dismiss { display: grid; place-items: center; width: 24px; height: 24px; padding: 0; border: none; background: transparent; color: #8d847a; cursor: pointer; transition: color 120ms ease; }
.upload-dismiss:hover { color: var(--bone); }
.retry-upload { align-self: center; min-height: 34px; padding: 0 14px; border: 1px solid var(--brass); border-radius: 3px; background: transparent; color: var(--brass); font-size: 11px; font-weight: 700; cursor: pointer; transition: background 120ms ease, color 120ms ease; }
.retry-upload:hover:not(:disabled) { background: var(--brass); color: #17110b; }
/* ── Presets bar ── */
.presets-block { display: grid; gap: 8px; }
.presets-header { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.presets-header .field-label { margin-bottom: 0; }
.presets-actions { display: inline-flex; gap: 6px; }
.ghost-btn { min-height: 28px; padding: 0 10px; border: 1px solid var(--line); border-radius: 3px; background: transparent; color: var(--brass); font-size: 11px; font-weight: 650; cursor: pointer; transition: border-color 120ms ease; }
.ghost-btn:hover:not(:disabled) { border-color: var(--brass); }
.preset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(126px, 1fr)); gap: 6px; }
.preset-chip { display: grid; gap: 3px; justify-items: start; padding: 8px 10px; border: 1px solid var(--line); border-radius: 3px; background: rgba(0, 0, 0, 0.22); color: var(--bone); text-align: left; cursor: pointer; transition: border-color 120ms ease, background 120ms ease; }
.preset-chip:hover:not(:disabled):not(.selected) { border-color: #6f685f; }
.preset-chip.selected { border-color: var(--brass); background: rgba(215, 167, 71, 0.09); }
.preset-name { font-size: 12px; font-weight: 650; }
.preset-type { font-size: 10px; color: var(--brass); }
.preset-tags { display: flex; flex-wrap: wrap; gap: 4px; font-size: 10px; color: #8d847a; }
.preset-editor { display: grid; gap: 10px; padding: 10px; border-left: 2px solid var(--brass); background: rgba(215, 167, 71, 0.05); }
.editor-field { display: grid; gap: 4px; }
.editor-field .field-label { margin-bottom: 0; }
.editor-actions { display: flex; gap: 6px; }
.editor-actions button { min-height: 32px; padding: 0 12px; border-radius: 3px; font-size: 11px; font-weight: 700; cursor: pointer; transition: filter 120ms ease, border-color 120ms ease; }
.editor-save { border: 1px solid var(--brass); background: var(--brass); color: #17110b; }
.editor-save:hover:not(:disabled) { filter: brightness(1.1); }
.editor-delete { border: 1px solid var(--red); background: transparent; color: var(--red); }
.editor-delete:hover:not(:disabled) { background: rgba(213, 45, 36, 0.12); }
.editor-cancel { border: 1px solid var(--line); background: transparent; color: var(--ash, #9e9183); }
.editor-cancel:hover:not(:disabled) { border-color: #6f685f; }
/* ── Collapsible overrides ── */
.custom-overrides { border-top: 1px solid var(--line); }
.custom-overrides summary { padding: 9px 2px; font-size: 11px; font-weight: 650; color: #b8ac9d; cursor: pointer; list-style: none; }
.custom-overrides summary::-webkit-details-marker { display: none; }
.custom-overrides summary::before { content: '▸'; display: inline-block; margin-right: 6px; color: #6f685f; transition: transform 120ms ease; }
.custom-overrides[open] summary::before { transform: rotate(90deg); }
.custom-overrides.locked { opacity: 0.55; }
.custom-overrides.locked summary { cursor: not-allowed; }
.overrides-body { display: grid; gap: 12px; padding: 2px 2px 10px; }
.type-field { display: block; }
.type-field .field-label small { margin-left: 6px; color: #6f685f; font-weight: 500; }
@keyframes signal { to { height: var(--bar-height); } }
</style>
