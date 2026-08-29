<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Icon, { type IconName } from '$lib/Icon.svelte';
	import Markdown from '$lib/Markdown.svelte';
	import TagChips from '$lib/TagChips.svelte';
	import {
		getRecording,
		updateRecording,
		deleteRecording,
		audioUrl,
		fetchArtifact,
		regenerate,
		loadApiConfig,
		type Recording,
		type Stage
	} from '$lib/api.svelte';
	import { dateLabel, durationLabel } from '$lib/format';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';
	import { ensureProfiles, profilesCache } from '$lib/profiles.svelte';
	import { artifactTab, stageNames, stageRetry, type ArtifactTabKey, type StageKind } from '$lib/stores.svelte';

	const id = page.params.id ?? '';
	const stageIcons: Record<StageKind, IconName> = {
		chunk: 'chunk',
		transcribe: 'transcript',
		diarize: 'diarize',
		merge_speakers: 'speakers',
		summarize: 'summary',
		enrich: 'enrich'
	};

	let recording = $state<Recording | null>(null);
	let loading = $state(true);
	let notFound = $state(false);
	let error = $state('');
	let renaming = $state(false);
	let draft = $state('');
	let pendingTitle = $state<string | null>(null);
	let renameError = $state('');
	let rerunError = $state('');
	let deleteArmed = $state(false);
	let deleting = $state(false);
	let deleteError = $state('');
	let pollTimer: ReturnType<typeof globalThis.setInterval> | null = null;
	let audioEl = $state<HTMLAudioElement>();
	let tagError = $state('');
	let tagSaving = $state(false);
	let tagSuggestions = $derived(tagSuggestionsCache.items);
	/** Pipeline-type editor state: null = editing closed; string = draft value. */
	let typeDraft = $state<string | null>(null);
	let typeSaving = $state(false);
	let typeError = $state('');
	/** recorded_at editor state: null = editing closed; string = datetime-local draft. */
	let whenDraft = $state<string | null>(null);
	let whenSaving = $state(false);
	let whenError = $state('');
	const typeProfile = $derived(
		recording?.type ? profilesCache.items.find((profile) => profile.type === recording?.type) ?? null : null
	);

	type TabData = { kind: 'ready'; text: string } | { kind: 'missing' } | { kind: 'error'; message: string };
	const TAB_SPECS: Record<ArtifactTabKey, { label: string; stage: string; file?: string; markdown?: boolean }> = {
		transcript: { label: 'Transcript', stage: 'transcribe', markdown: true },
		speakers: { label: 'Speakers', stage: 'merge_speakers', markdown: true },
		summary: { label: 'Summary', stage: 'summarize', markdown: true },
		json: { label: 'JSON', stage: 'transcribe', file: 'segments.json' }
	};
	let tabData = $state<Partial<Record<ArtifactTabKey, TabData>>>({});
	let tabLoading = $state(false);
	let tabGeneration = 0;
	const tabInflight = new Set<ArtifactTabKey>();
	const activeTab = $derived(artifactTab.active);
	const currentTab = $derived(tabData[activeTab]);

	function defaultTab(rec: Recording): ArtifactTabKey {
		const done = (kind: Stage['kind']) => rec.stages.some((s) => s.kind === kind && s.status === 'done');
		if (done('summarize')) return 'summary';
		if (done('merge_speakers')) return 'speakers';
		if (done('transcribe')) return 'transcript';
		return 'json';
	}

	function stopPoll(): void {
		if (pollTimer) {
			globalThis.clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	function startPoll(): void {
		stopPoll();
		pollTimer = globalThis.setInterval(() => void load(), 3000);
	}

	function invalidateArtifacts(): void {
		tabData = {};
		tabGeneration += 1;
	}

	function applyRecording(next: Recording): void {
		const previous = recording;
		recording = pendingTitle !== null ? { ...next, title: pendingTitle } : next;
		stageRetry.stages = next.stages;
		stageRetry.enabled = next.state === 'done' || next.state === 'failed';
		if (previous?.state === 'processing' && next.state !== 'processing') {
			invalidateArtifacts();
			void loadTab(activeTab, true);
		}
		if (next.state === 'done' || next.state === 'failed') stopPoll();
	}

	async function load(): Promise<void> {
		try {
			const next = await getRecording(loadApiConfig(), id);
			notFound = false;
			error = '';
			applyRecording(next);
		} catch (caught) {
			const status = (caught as { status?: number }).status;
			if (status === 404 || status === 400) {
				notFound = true;
				error = '';
				stopPoll();
				stageRetry.stages = [];
				stageRetry.enabled = false;
			} else {
				error = String(caught);
				if (status === 401) stopPoll();
			}
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		stageRetry.rerun = rerun;
		void (async () => {
			await load();
			if (!recording) return;
			if (recording.state === 'uploading' || recording.state === 'processing') startPoll();
			artifactTab.active = defaultTab(recording);
		})();
		return () => {
			stopPoll();
			stageRetry.stages = [];
			stageRetry.enabled = false;
			stageRetry.rerun = null;
		};
	});

	$effect(() => {
		const tab = artifactTab.active;
		if (!recording) return;
		void loadTab(tab);
	});

	$effect(() => {
		// Type hint + freehand-tag suggestions; failures degrade silently.
		void ensureProfiles(loadApiConfig());
		void ensureTagSuggestions(loadApiConfig());
	});

	function autofocus(node: HTMLElement): void {
		node.focus();
	}

	function startRename(): void {
		if (!recording) return;
		renaming = true;
		draft = recording.title;
		renameError = '';
	}

	function cancelRename(): void {
		renaming = false;
		draft = '';
	}

	async function commitRename(): Promise<void> {
		if (!recording) return;
		const previous = recording.title;
		const title = draft.trim();
		cancelRename();
		if (title === previous) return;
		recording = { ...recording, title };
		pendingTitle = title;
		try {
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { title }));
		} catch (caught) {
			recording = { ...recording, title: previous };
			renameError = `Rename failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			pendingTitle = null;
		}
	}

	function sameTags(a: string[], b: string[]): boolean {
		if (a.length !== b.length) return false;
		for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
		return true;
	}

	async function saveTags(next: string[]): Promise<void> {
		if (!recording) return;
		const previous = recording.tags;
		if (sameTags(previous, next)) return;
		recording = { ...recording, tags: next };
		tagSaving = true;
		tagError = '';
		try {
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { tags: next }));
		} catch (caught) {
			recording = { ...recording, tags: previous };
			tagError = `Tag update failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			tagSaving = false;
		}
	}

	async function commitType(): Promise<void> {
		if (!recording || typeDraft === null || typeSaving) return;
		const next = typeDraft;
		const previous = recording.type;
		typeDraft = null;
		if (next === previous) return;
		recording = { ...recording, type: next };
		typeSaving = true;
		typeError = '';
		try {
			// A type change re-runs the pipeline (summarize+enrich) on the
			// server when the recording is done — the response carries the
			// fresh state; the poller picks up the re-processing stages.
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { type: next }));
		} catch (caught) {
			recording = { ...recording, type: previous };
			typeError = `Type update failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			typeSaving = false;
		}
	}

	function toLocalDatetimeValue(iso: string | null): string {
		if (!iso) return '';
		const date = new Date(iso);
		if (Number.isNaN(date.getTime())) return '';
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
	}

	async function commitWhen(): Promise<void> {
		if (!recording || whenDraft === null || whenSaving) return;
		const draft = whenDraft;
		whenDraft = null;
		// Empty draft clears the backdate (falls back to created_at display).
		const iso = draft ? new Date(draft).toISOString() : null;
		const previous = recording.recorded_at;
		if (iso === previous) return;
		recording = { ...recording, recorded_at: iso };
		whenSaving = true;
		whenError = '';
		try {
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { recorded_at: iso }));
		} catch (caught) {
			recording = { ...recording, recorded_at: previous };
			whenError = `Date update failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			whenSaving = false;
		}
	}

	function sizeLabel(bytes: number | null): string {
		if (bytes === null) return '—';
		return `${(bytes / 1_000_000).toFixed(1)} MB`;
	}

	async function rerun(stage: StageKind): Promise<void> {
		if (!recording) return;
		rerunError = '';
		try {
			await regenerate(loadApiConfig(), recording.id, stage);
			recording = { ...recording, state: 'processing' };
			stageRetry.enabled = false;
			invalidateArtifacts();
			startPoll();
		} catch (caught) {
			rerunError = `Re-run failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		}
	}

	async function loadTab(tab: ArtifactTabKey, force = false): Promise<void> {
		if (!recording) return;
		if (!force && (tabData[tab] !== undefined || tabInflight.has(tab))) return;
		const spec = TAB_SPECS[tab];
		const generation = ++tabGeneration;
		tabInflight.add(tab);
		tabLoading = true;
		try {
			const text = await fetchArtifact(loadApiConfig(), recording.id, spec.stage, spec.file);
			if (generation !== tabGeneration) return;
			tabData = { ...tabData, [tab]: { kind: 'ready', text } };
		} catch (caught) {
			if (generation !== tabGeneration) return;
			const status = (caught as { status?: number }).status;
			if (status === 404) {
				tabData = { ...tabData, [tab]: { kind: 'missing' } };
			} else {
				tabData = { ...tabData, [tab]: { kind: 'error', message: `${spec.label} failed to load: ${caught instanceof Error ? caught.message : String(caught)}` } };
			}
		} finally {
			tabInflight.delete(tab);
			if (generation === tabGeneration) tabLoading = false;
		}
	}

	function armDelete(): void {
		deleteArmed = true;
		deleteError = '';
	}

	function handleDeleteKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.stopPropagation();
		deleteArmed = false;
	}

	async function confirmDelete(): Promise<void> {
		if (!recording || deleting) return;
		deleting = true;
		deleteError = '';
		try {
			if (audioEl) {
				audioEl.pause();
				audioEl.removeAttribute('src');
				audioEl.load();
			}
			await deleteRecording(loadApiConfig(), recording.id);
			await goto('/recordings');
		} catch (caught) {
			if (audioEl) audioEl.src = audioUrl(loadApiConfig(), recording.id);
			deleteError = `Delete failed: ${caught instanceof Error ? caught.message : String(caught)}`;
			deleteArmed = false;
			deleting = false;
		}
	}
</script>

<svelte:head><title>{recording?.title || 'Recording'} · Transcriptor Maximus</title></svelte:head>

<section class="page detail-page">
	<header class="detail-header">
		<button class="back-button" type="button" onclick={() => goto('/recordings')} aria-label="Back to recordings" title="Back to recordings">
			<Icon name="back" size={16} />
		</button>
		{#if loading}
			<span class="skeleton-bar skeleton-heading" aria-hidden="true"></span>
		{:else if recording && renaming}
			<div class="rename-row">
				<input
					type="text"
					{@attach autofocus}
					bind:value={draft}
					placeholder="Untitled capture"
					aria-label="Recording title"
					onkeydown={(event) => {
						if (event.key === 'Enter') commitRename();
						else if (event.key === 'Escape') {
							event.stopPropagation();
							event.preventDefault();
							cancelRename();
						}
					}}
				/>
				<button type="button" onclick={commitRename}>Save</button>
				<button type="button" onclick={cancelRename}>Cancel</button>
			</div>
		{:else if recording}
			<h1 class="page-title detail-title">{recording.title || 'Untitled capture'}</h1>
			<button class="rename-edit" type="button" title="Rename recording" aria-label="Rename recording" onclick={startRename}><Icon name="pencil" size={14} /></button>
		{:else}
			<h1 class="page-title detail-title">Recording</h1>
		{/if}
	</header>

	{#if renameError}
		<p class="inline-error" role="alert">{renameError}</p>
	{/if}

	{#if loading}
		<div class="skeleton-panel" aria-hidden="true">
			<span class="skeleton-bar skeleton-meta"></span>
			<span class="skeleton-bar skeleton-strip"></span>
			<span class="skeleton-bar skeleton-player"></span>
			<span class="skeleton-bar skeleton-body"></span>
		</div>
	{:else if notFound}
		<div class="notice-panel">
			<strong>Recording not found</strong>
			<small>It may have been deleted, or the address is wrong.</small>
			<a class="back-link" href="/recordings"><Icon name="back" size={13} /> Back to recordings</a>
		</div>
	{:else if !recording}
		<div class="archive-error" role="alert"><strong>Recording unavailable</strong><span>{error}</span></div>
	{:else}
		{#if error}
			<div class="archive-error" role="alert"><strong>Refresh failed</strong><span>{error}</span></div>
		{/if}

		<div class="detail-meta">
			<div class="meta-row">
				<span class={`state-mark ${recording.state}`} aria-hidden="true"></span>
				<span class="meta-text">{dateLabel(recording.recorded_at ?? recording.created_at)} · {durationLabel(recording.duration_sec)} · {sizeLabel(recording.total_bytes)}</span>
				<span class={`state-label ${recording.state}`}>{recording.state}</span>
			</div>
			<div class="meta-row type-row">
				{#if typeDraft !== null}
					<select class="type-edit" bind:value={typeDraft} aria-label="Pipeline type" disabled={typeSaving}>
						<option value={null}>None — default pipeline</option>
						{#each profilesCache.items as profile (profile.id)}
							<option value={profile.type}>{profile.display_name}</option>
						{/each}
					</select>
					<button class="meta-edit-save" type="button" onclick={() => void commitType()} disabled={typeSaving}>Save</button>
					<button class="meta-edit-cancel" type="button" onclick={() => (typeDraft = null)} disabled={typeSaving}>Cancel</button>
				{:else}
					<button class="type-badge" type="button" title="Change pipeline type" onclick={() => (typeDraft = recording?.type ?? null)}>
						{typeProfile ? typeProfile.display_name : recording.type}
					</button>
					{#if typeSaving}<span class="meta-saving">saving…</span>{/if}
				{/if}
				{#if whenDraft !== null}
					<input class="when-edit" type="datetime-local" bind:value={whenDraft} aria-label="Recorded at" disabled={whenSaving} />
					<button class="meta-edit-save" type="button" onclick={() => void commitWhen()} disabled={whenSaving}>Save</button>
					<button class="meta-edit-cancel" type="button" onclick={() => (whenDraft = null)} disabled={whenSaving}>Cancel</button>
				{:else}
					<button class="when-badge" type="button" title="Edit recorded date" onclick={() => (whenDraft = toLocalDatetimeValue(recording?.recorded_at ?? recording?.created_at ?? null))}>
						<Icon name="pencil" size={11} strokeWidth={1.5} />
						{recording.recorded_at ? dateLabel(recording.recorded_at) : 'date = upload time'}
					</button>
					{#if whenSaving}<span class="meta-saving">saving…</span>{/if}
				{/if}
				{#if typeError}<span class="inline-error" role="alert">{typeError}</span>{/if}
				{#if whenError}<span class="inline-error" role="alert">{whenError}</span>{/if}
			</div>
			<span class="stage-icons" role="group" aria-label="Pipeline stages">
				{#each recording.stages as stage (stage.kind)}
					<span
						class={`stage-icon ${stage.status}`}
						role="img"
						title="{stageNames[stage.kind]} · {stage.status}"
						aria-label="{stageNames[stage.kind]}: {stage.status}"
					>
						<Icon name={stageIcons[stage.kind]} size={16} strokeWidth={1.4} />
					</span>
				{/each}
			</span>
		</div>
		<div class="tags-row" class:saving={tagSaving}>
			<span class="tags-label">Tags</span>
			<TagChips
				tags={recording.tags}
				dense
				placeholder="Add a tag and press Enter"
				suggestions={tagSuggestions}
				onChange={(next) => void saveTags(next)}
			/>
		</div>
	{#if tagError}
		<p class="inline-error" role="alert">{tagError}</p>
	{:else if recording.tags.length > 0}
		<p class="tags-hint">Tag changes re-run memory extraction automatically.</p>
	{/if}
	{#each recording.stages.filter((stage) => stage.status === 'failed' && stage.last_error) as stage (stage.kind)}
		<p class="stage-error" role="alert">{stageNames[stage.kind]} failed: {stage.last_error}</p>
	{/each}
	{#if rerunError}
		<p class="inline-error" role="alert">{rerunError}</p>
	{/if}


		{#if recording.state === 'uploading'}
			<p class="audio-note">Audio available after upload.</p>
		{:else}
			<audio bind:this={audioEl} class="audio-player" controls preload="none" src={audioUrl(loadApiConfig(), recording.id)}></audio>
		{/if}

		<div class="artifact-panel" role="tabpanel" aria-label={TAB_SPECS[activeTab].label}>
			{#if tabLoading}
				<p class="tab-placeholder">Retrieving archive…</p>
			{:else if currentTab?.kind === 'ready'}
				{#if TAB_SPECS[activeTab].markdown}
					<Markdown text={currentTab.text} />
				{:else}
					<pre>{currentTab.text}</pre>
				{/if}
			{:else if currentTab?.kind === 'missing'}
				<p class="tab-placeholder">This artifact has not been generated yet.</p>
			{:else if currentTab?.kind === 'error'}
				<p class="tab-error" role="alert">{currentTab.message}</p>
			{/if}
		</div>

		{#if recording.state === 'done' || recording.state === 'failed'}
			{#if deleteArmed}
				<div class="delete-confirm">
					<span>Permanently delete?</span>
					<button class="delete-yes" type="button" disabled={deleting} onkeydown={handleDeleteKeydown} onclick={confirmDelete}>{deleting ? 'Deleting…' : 'Confirm'}</button>
					<button class="delete-no" type="button" disabled={deleting} {@attach autofocus} onkeydown={handleDeleteKeydown} onclick={() => (deleteArmed = false)}>Cancel</button>
				</div>
			{:else}
				<button class="delete-button" type="button" onclick={armDelete}><Icon name="trash" size={14} /> Delete</button>
			{/if}
			{#if deleteError}
				<p class="inline-error" role="alert">{deleteError}</p>
			{/if}
		{/if}
	{/if}
</section>

<style>
	.detail-page { display: flex; flex-direction: column; gap: 12px; min-height: 100%; }
	.detail-header { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; }
	.detail-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 24px; }
	.back-button { width: 32px; height: 32px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #8e857b; cursor: pointer; line-height: 0; }
	.back-button:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
.detail-meta { display: flex; flex-direction: column; gap: 8px; }
.type-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.type-badge { padding: 2px 8px; border: 1px solid rgba(215,167,71,.35); border-radius: 2px; background: transparent; color: var(--brass); font-size: 10px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; cursor: pointer; }
.type-badge:hover { border-color: var(--brass); background: rgba(215,167,71,0.08); }
.when-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #8e857b; font-size: 10px; cursor: pointer; }
.when-badge:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
.type-edit, .when-edit { padding: 4px 6px; border: 1px solid rgba(215,167,71,.4); border-radius: 2px; background: rgba(0,0,0,0.25); color: var(--bone); font-size: 11px; color-scheme: dark; }
.meta-edit-save { padding: 4px 8px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,0.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
.meta-edit-cancel { padding: 4px 8px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #8e857b; font-size: 10px; cursor: pointer; }
.meta-edit-save:disabled, .meta-edit-cancel:disabled { opacity: 0.6; cursor: default; }
.meta-saving { font-size: 10px; color: #8d847a; }
	.meta-row { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; }
	.meta-text { font-size: 11px; color: #8b8278; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.state-mark { width: 7px; height: 7px; border-radius: 50%; background: #706960; box-shadow: 0 0 0 3px rgba(112,105,96,.12); }
	.state-mark.done { background: var(--cyan); box-shadow: 0 0 9px rgba(112,215,208,.55); }
	.state-mark.processing, .state-mark.uploading { background: var(--brass); box-shadow: 0 0 9px rgba(215,167,71,.5); }
	.state-mark.failed { background: var(--red); box-shadow: 0 0 9px rgba(213,45,36,.6); }
	.state-label { color: #968d83; font-size: 9px; font-weight: 700; text-transform: capitalize; }
	.state-label.done { color: var(--cyan); }
	.state-label.processing, .state-label.uploading { color: var(--brass); }
	.state-label.failed { color: #f36b60; }
	.stage-icons { display: flex; gap: 4px; padding-top: 8px; border-top: 1px solid var(--line); }
	.stage-icon { width: 22px; height: 22px; display: grid; place-items: center; border: 1px solid transparent; background: rgba(255,255,255,.03); border-radius: 2px; color: #6f685f; line-height: 0; }
	.stage-icon.done { color: var(--cyan); border-color: rgba(112,215,208,.25); }
	.stage-icon.running { color: var(--brass); border-color: rgba(215,167,71,.25); }
	.stage-icon.failed { color: #f36b60; border-color: rgba(213,45,36,.35); }
	.stage-error { margin: 0; color: #f36b60; font-size: 11px; }
	.tags-row { display: grid; grid-template-columns: auto 1fr; align-items: start; gap: 9px; padding-top: 8px; border-top: 1px solid var(--line); transition: opacity 120ms ease; }
	.tags-row.saving { opacity: 0.65; }
	.tags-label { padding-top: 5px; font-size: 10px; font-weight: 650; color: #8b8278; letter-spacing: 0.02em; }
	.tags-hint { margin: 0; color: var(--ash); font-size: 10px; }
	.audio-player { width: 100%; height: 36px; border-radius: 2px; background: rgba(0,0,0,.14); color-scheme: dark; accent-color: var(--brass); }
	.audio-note { margin: 0; font-size: 11px; color: var(--ash); }
	.artifact-panel { flex: 1 1 auto; min-height: 220px; display: flex; flex-direction: column; overflow: hidden; background: rgba(0,0,0,.22); border-radius: 3px; box-shadow: inset 0 1px 3px rgba(0,0,0,.4); }
	.artifact-panel pre { flex: 1; min-height: 0; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; color: #c7bbad; font: 11px/1.6 "SFMono-Regular", Consolas, monospace; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.tab-placeholder { margin: auto; padding: 18px; color: var(--ash); font-size: 11px; }
	.tab-error { margin: auto; padding: 18px; color: #f36b60; font-size: 11px; }
	.delete-button { min-height: 34px; display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 0 12px; border: 1px solid rgba(213,45,36,.4); border-radius: 2px; background: rgba(213,45,36,.08); color: #f36b60; font-size: 10px; font-weight: 700; cursor: pointer; line-height: 0; }
	.delete-button:hover { border-color: var(--red); background: rgba(213,45,36,.14); }
	.delete-confirm { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 5px; }
	.delete-confirm > span { font-size: 10px; font-weight: 650; color: #f36b60; }
	.delete-confirm button { min-height: 32px; padding: 0 10px; border-radius: 2px; font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.delete-yes { border: 1px solid var(--red); background: var(--red); color: var(--bone); }
	.delete-yes:hover:not(:disabled) { background: #b3251d; }
	.delete-no { border: 1px solid rgba(215,167,71,.26); background: rgba(215,167,71,.06); color: var(--brass); }
	.delete-no:hover:not(:disabled) { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.delete-confirm button:disabled { opacity: 0.55; cursor: default; }
	.rename-row { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 5px; }
	.rename-row input { min-height: 32px; height: 32px; }
	.rename-row button { min-height: 32px; padding: 0 10px; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.rename-row button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.rename-edit { width: 28px; height: 28px; display: grid; place-items: center; padding: 0; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); cursor: pointer; line-height: 0; }
	.rename-edit:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.inline-error { margin: 0; color: #f36b60; font-size: 11px; }
	.archive-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.archive-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.archive-error span { color: #c6baaa; }
	.notice-panel { display: grid; justify-items: start; gap: 6px; padding: 18px 14px; }
	.notice-panel strong { color: #b5aa9c; font-size: 13px; }
	.notice-panel small { color: #746d64; font-size: 11px; }
	.back-link { display: inline-flex; align-items: center; gap: 6px; margin-top: 4px; color: var(--brass); font-size: 11px; font-weight: 650; text-decoration: none; }
	.back-link:hover { color: var(--bone); }
	.skeleton-heading { width: 55%; height: 18px; }
	.skeleton-panel { display: grid; gap: 10px; padding: 0; }
	.skeleton-bar { display: block; border-radius: 2px; background: var(--iron-raised); animation: skeleton-pulse 150ms ease-in-out infinite alternate; }
	.skeleton-meta { width: 46%; height: 11px; }
	.skeleton-strip { width: 82%; height: 18px; }
	.skeleton-player { width: 100%; height: 36px; }
	.skeleton-body { width: 100%; height: 220px; }
	@keyframes skeleton-pulse { from { opacity: 0.55; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .skeleton-bar { animation: none; opacity: 0.75; } }
</style>
