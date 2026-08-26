<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Icon from '$lib/Icon.svelte';
	import {
		getRecording,
		renameRecording,
		deleteRecording,
		audioUrl,
		fetchArtifact,
		regenerate,
		loadApiConfig,
		type Recording
	} from '$lib/api.svelte';
	import { dateLabel, durationLabel } from '$lib/format';

	const id = page.params.id ?? ''; // undefined → 400 → not-found panel

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

	type TabKey = 'transcript' | 'speakers' | 'summary' | 'json';
	type TabData = { kind: 'ready'; text: string } | { kind: 'missing' } | { kind: 'error'; message: string };
	const TAB_SPECS: Record<TabKey, { label: string; stage: string; file?: string }> = {
		transcript: { label: 'Transcript', stage: 'transcribe' },
		speakers: { label: 'Speakers', stage: 'merge_speakers' },
		summary: { label: 'Summary', stage: 'summarize' },
		json: { label: 'JSON', stage: 'transcribe', file: 'segments.json' }
	};
	const TAB_ORDER: TabKey[] = ['transcript', 'speakers', 'summary', 'json'];
	let activeTab = $state<TabKey>('transcript');
	let tabData = $state<Partial<Record<TabKey, TabData>>>({});
	let tabLoading = $state(false);
	let tabGeneration = 0;
	const currentTab = $derived(tabData[activeTab]);

	const STAGES = ['chunk', 'transcribe', 'diarize', 'merge_speakers', 'summarize'] as const;
	const stageNames: Record<(typeof STAGES)[number], string> = {
		chunk: 'Chunks',
		transcribe: 'Transcript',
		diarize: 'Diarize',
		merge_speakers: 'Speakers',
		summarize: 'Summary'
	};

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
		if (previous?.state === 'processing' && next.state !== 'processing') {
			// A re-run finished: artifacts are rewritten only at stage completion,
			// so a tab opened mid-rerun would otherwise cache the pre-rerun file.
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
				stopPoll(); // recording gone / bad id — stop issuing doomed requests
			} else {
				error = String(caught);
				if (status === 401) stopPoll(); // token changed in Settings
			}
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void (async () => {
			await load();
			if (!recording) return;
			if (recording.state === 'uploading' || recording.state === 'processing') startPoll();
			void loadTab('transcript');
		})();
		return () => stopPoll();
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
			applyRecording(await renameRecording(loadApiConfig(), recording.id, title));
		} catch (caught) {
			recording = { ...recording, title: previous };
			renameError = `Rename failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			pendingTitle = null;
		}
	}

	function sizeLabel(bytes: number | null): string {
		if (bytes === null) return '—';
		return `${(bytes / 1_000_000).toFixed(1)} MB`;
	}

	async function rerun(stage: (typeof STAGES)[number]): Promise<void> {
		if (!recording) return;
		rerunError = '';
		try {
			await regenerate(loadApiConfig(), recording.id, stage);
			recording = { ...recording, state: 'processing' };
			invalidateArtifacts();
			startPoll();
		} catch (caught) {
			rerunError = `Re-run failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		}
	}

	function selectTab(tab: TabKey): void {
		if (tab === activeTab) return;
		activeTab = tab;
		void loadTab(tab);
	}

	async function loadTab(tab: TabKey, force = false): Promise<void> {
		if (!recording) return;
		if (!force && tabData[tab] !== undefined) return;
		const spec = TAB_SPECS[tab];
		const generation = ++tabGeneration;
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
			if (generation === tabGeneration) tabLoading = false;
		}
	}

	function armDelete(): void {
		deleteArmed = true;
		deleteError = '';
	}

	function handleDeleteKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		// The window-level layout Escape handler collapses the app; keep it local.
		event.stopPropagation();
		deleteArmed = false;
	}

	async function confirmDelete(): Promise<void> {
		if (!recording || deleting) return;
		deleting = true;
		deleteError = '';
		try {
			if (audioEl) {
				// Release the audio file before the server removes recordings_root/{id}.
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
		<div class="panel skeleton-panel" aria-hidden="true">
			<span class="skeleton-bar skeleton-meta"></span>
			<span class="skeleton-bar skeleton-strip"></span>
			<span class="skeleton-bar skeleton-player"></span>
			<span class="skeleton-bar skeleton-body"></span>
		</div>
	{:else if notFound}
		<div class="panel notice-panel">
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

		<div class="detail-meta panel">
			<span class={`state-mark ${recording.state}`} aria-hidden="true"></span>
			<span class="meta-text">{dateLabel(recording.created_at)} · {durationLabel(recording.duration_sec)} · {sizeLabel(recording.total_bytes)}</span>
			<span class={`state-label ${recording.state}`}>{recording.state}</span>
		</div>

		<div class="stage-strip" aria-label="Pipeline stages">
			{#each recording.stages as stage (stage.kind)}
				<span class={`stage-chip ${stage.status}`}>{stageNames[stage.kind]} · {stage.status}</span>
			{/each}
		</div>
		{#each recording.stages.filter((stage) => stage.status === 'failed' && stage.last_error) as stage (stage.kind)}
			<p class="stage-error" role="alert">{stageNames[stage.kind]} failed: {stage.last_error}</p>
		{/each}

		{#if recording.state === 'uploading'}
			<p class="audio-note">Audio available after upload.</p>
		{:else}
			<audio bind:this={audioEl} class="audio-player" controls preload="none" src={audioUrl(loadApiConfig(), recording.id)}></audio>
		{/if}

		<div class="tab-bar" role="tablist" aria-label="Artifacts">
			{#each TAB_ORDER as tab (tab)}
				<button
					type="button"
					role="tab"
					aria-selected={activeTab === tab}
					class:active={activeTab === tab}
					onclick={() => selectTab(tab)}
				>{TAB_SPECS[tab].label}</button>
			{/each}
		</div>
		<div class="artifact-panel panel">
			{#if tabLoading}
				<p class="tab-placeholder">Retrieving archive…</p>
			{:else if currentTab?.kind === 'ready'}
				<pre>{currentTab.text}</pre>
			{:else if currentTab?.kind === 'missing'}
				<p class="tab-placeholder">This artifact has not been generated yet.</p>
			{:else if currentTab?.kind === 'error'}
				<p class="tab-error" role="alert">{currentTab.message}</p>
			{/if}
		</div>

		{#if recording.state === 'done' || recording.state === 'failed'}
			<div class="rerun-row">
				<span>Re-run stage</span>
				{#each STAGES as stage (stage)}
					<button type="button" title={`Re-run ${stageNames[stage]}`} aria-label={`Re-run ${stageNames[stage]}`} onclick={() => rerun(stage)}><Icon name="refresh" size={14} /></button>
				{/each}
			</div>
			{#if rerunError}
				<p class="inline-error" role="alert">{rerunError}</p>
			{/if}

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
	.detail-meta { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; padding: 10px 11px; }
	.meta-text { font-size: 11px; color: #8b8278; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.state-mark { width: 7px; height: 7px; border-radius: 50%; background: #706960; box-shadow: 0 0 0 3px rgba(112,105,96,.12); }
	.state-mark.done { background: var(--cyan); box-shadow: 0 0 9px rgba(112,215,208,.55); }
	.state-mark.processing, .state-mark.uploading { background: var(--brass); box-shadow: 0 0 9px rgba(215,167,71,.5); }
	.state-mark.failed { background: var(--red); box-shadow: 0 0 9px rgba(213,45,36,.6); }
	.state-label { padding: 5px 7px; border: 1px solid var(--line); border-radius: 2px; color: #968d83; font-size: 9px; font-weight: 700; text-transform: capitalize; }
	.state-label.done { color: var(--cyan); border-color: rgba(112,215,208,.25); }
	.state-label.processing, .state-label.uploading { color: var(--brass); border-color: rgba(215,167,71,.25); }
	.state-label.failed { color: #f36b60; border-color: rgba(213,45,36,.35); }
	.stage-strip { display: flex; flex-wrap: wrap; gap: 5px; }
	.stage-chip { padding: 5px 7px; border: 1px solid var(--line); border-radius: 2px; color: #968d83; font-size: 9px; font-weight: 700; }
	.stage-chip.done { color: var(--cyan); border-color: rgba(112,215,208,.25); }
	.stage-chip.running { color: var(--brass); border-color: rgba(215,167,71,.25); }
	.stage-chip.failed { color: #f36b60; border-color: rgba(213,45,36,.35); }
	.stage-chip.skipped, .stage-chip.pending { color: #6f685f; }
	.stage-error { margin: 0; color: #f36b60; font-size: 11px; }
	.audio-player { width: 100%; height: 36px; border-radius: 2px; background: rgba(0,0,0,.14); color-scheme: dark; accent-color: var(--brass); }
	.audio-note { margin: 0; font-size: 11px; color: var(--ash); }
	.tab-bar { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; }
	.tab-bar button { min-height: 32px; display: grid; place-items: center; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); font-size: 9px; font-weight: 700; cursor: pointer; }
	.tab-bar button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.tab-bar button.active { background: rgba(215,167,71,.16); border-color: var(--brass); color: var(--bone); }
	.artifact-panel { flex: 1 1 auto; min-height: 220px; display: flex; flex-direction: column; overflow: hidden; }
	.artifact-panel pre { flex: 1; min-height: 0; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; color: #c7bbad; font: 11px/1.6 "SFMono-Regular", Consolas, monospace; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.tab-placeholder { margin: auto; padding: 18px; color: var(--ash); font-size: 11px; }
	.tab-error { margin: auto; padding: 18px; color: #f36b60; font-size: 11px; }
	.rerun-row { display: grid; grid-template-columns: 1fr repeat(5, 28px); align-items: center; gap: 5px; }
	.rerun-row > span { font-size: 9px; font-weight: 650; color: #867d73; }
	.rerun-row button { min-height: 32px; display: grid; place-items: center; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); cursor: pointer; line-height: 0; }
	.rerun-row button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
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
	.skeleton-panel { display: grid; gap: 10px; padding: 12px; }
	.skeleton-bar { display: block; border-radius: 2px; background: var(--iron-raised); animation: skeleton-pulse 150ms ease-in-out infinite alternate; }
	.skeleton-meta { width: 46%; height: 11px; }
	.skeleton-strip { width: 82%; height: 18px; }
	.skeleton-player { width: 100%; height: 36px; }
	.skeleton-body { width: 100%; height: 220px; }
	@keyframes skeleton-pulse { from { opacity: 0.55; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .skeleton-bar { animation: none; opacity: 0.75; } }
</style>
