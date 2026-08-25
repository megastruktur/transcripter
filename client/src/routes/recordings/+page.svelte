<script lang="ts">
	import { onMount } from 'svelte';
	import Icon from '$lib/Icon.svelte';
	import { retryPendingUploads } from '$lib/stores.svelte';
	import { listRecordings, renameRecording, audioUrl, regenerate, fetchArtifact, loadApiConfig, type Recording } from '$lib/api.svelte';

	let recordings = $state<Recording[]>([]);
	let error = $state('');
	let loading = $state(true);
	let selected = $state<Recording | null>(null);
	let artifact = $state('');
	let artifactLabel = $state('');
	let query = $state('');
	let filter = $state<'all' | Recording['state']>('all');
	let renaming = $state<string | null>(null);
	let draft = $state('');
	let pendingTitles = $state<Record<string, string>>({});
	let renameError = $state<{ id: string; message: string } | null>(null);
	let pollTimer: ReturnType<typeof globalThis.setInterval> | null = null;

	const STAGES = ['chunk', 'transcribe', 'diarize', 'merge_speakers', 'summarize'] as const;
	const stageNames: Record<(typeof STAGES)[number], string> = {
		chunk: 'Chunks',
		transcribe: 'Transcript',
		diarize: 'Diarize',
		merge_speakers: 'Speakers',
		summarize: 'Summary'
	};
	const filteredRecordings = $derived(
		recordings.filter((recording) => {
			const matchesState = filter === 'all' || recording.state === filter;
			const needle = query.trim().toLowerCase();
			return matchesState && (!needle || recording.title.toLowerCase().includes(needle) || recording.id.toLowerCase().includes(needle));
		})
	);

	async function refresh(): Promise<void> {
		try {
			recordings = await listRecordings(loadApiConfig());
			recordings = recordings.map((recording) =>
				recording.id in pendingTitles ? { ...recording, title: pendingTitles[recording.id] } : recording
			);
			error = '';
			if (selected) selected = recordings.find((recording) => recording.id === selected?.id) ?? null;
		} catch (caught) {
			error = String(caught);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		refresh();
		retryPendingUploads().catch(() => {});
		pollTimer = globalThis.setInterval(refresh, 3000);
		return () => {
			if (pollTimer) globalThis.clearInterval(pollTimer);
		};
	});

	function autofocus(node: HTMLInputElement): void {
		node.focus();
	}

	function startRename(recording: Recording): void {
		renaming = recording.id;
		draft = recording.title;
		renameError = null;
	}

	function cancelRename(): void {
		renaming = null;
		draft = '';
	}

	async function commitRename(recording: Recording): Promise<void> {
		const id = recording.id;
		const previous = recording.title;
		const title = draft.trim();
		cancelRename();
		if (title === previous) return;
		recordings = recordings.map((entry) => (entry.id === id ? { ...entry, title } : entry));
		pendingTitles = { ...pendingTitles, [id]: title };
		try {
			await renameRecording(loadApiConfig(), id, title);
		} catch (caught) {
			recordings = recordings.map((entry) => (entry.id === id ? { ...entry, title: previous } : entry));
			renameError = { id, message: `Rename failed: ${caught instanceof Error ? caught.message : String(caught)}` };
		} finally {
			const next = { ...pendingTitles };
			delete next[id];
			pendingTitles = next;
		}
	}

	function dateLabel(value: string): string {
		return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
	}

	function durationLabel(seconds: number | null): string {
		if (seconds === null) return '—';
		const minutes = Math.floor(seconds / 60);
		return `${minutes}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;
	}

	async function rerun(recording: Recording, stage: (typeof STAGES)[number]): Promise<void> {
		try {
			await regenerate(loadApiConfig(), recording.id, stage);
			await refresh();
		} catch (caught) {
			error = String(caught);
		}
	}

	async function showArtifact(recording: Recording, stage: string, label: string, file?: string): Promise<void> {
		selected = recording;
		artifactLabel = label;
		artifact = 'Retrieving archive…';
		try {
			artifact = await fetchArtifact(loadApiConfig(), recording.id, stage, file);
		} catch {
			artifact = 'This artifact has not been generated yet.';
		}
	}
</script>

<svelte:head><title>Archive · Transcriptor Maximus</title></svelte:head>

<section class="page archive-page">
	<header>
		<h1 class="page-title">Recordings</h1>
	</header>

	<div class="archive-tools">
		<label>
			<span class="sr-only">Search recordings</span>
			<input type="search" placeholder="Search recordings" bind:value={query} />
		</label>
		<label>
			<span class="sr-only">Filter by state</span>
			<select bind:value={filter}>
				<option value="all">All states</option>
				<option value="uploading">Uploading</option>
				<option value="processing">Processing</option>
				<option value="done">Complete</option>
				<option value="failed">Failed</option>
			</select>
		</label>
	</div>

	{#if error}
		<div class="archive-error" role="alert"><strong>Archive unavailable</strong><span>{error}</span></div>
	{/if}

	<div class="record-list" aria-live="polite">
		{#if loading}
			{#each [0, 1, 2] as skeletonIndex (skeletonIndex)}
				<div class="record-card panel skeleton-card" aria-hidden="true">
					<span class="skeleton-bar skeleton-mark"></span>
					<span class="skeleton-lines">
						<span class="skeleton-bar skeleton-title"></span>
						<span class="skeleton-bar skeleton-meta"></span>
					</span>
					<span class="skeleton-bar skeleton-label"></span>
				</div>
			{/each}
		{:else}
			{#each filteredRecordings as recording (recording.id)}
				<article class:selected={selected?.id === recording.id} class="record-card panel">
					<div class="record-top">
						<button class="record-heading" type="button" onclick={() => (selected = selected?.id === recording.id ? null : recording)}>
							<span class={`state-mark ${recording.state}`} aria-hidden="true"></span>
							<span class="record-name"><strong>{recording.title || 'Untitled capture'}</strong><small>{dateLabel(recording.created_at)} · {durationLabel(recording.duration_sec)}</small></span>
							<span class={`state-label ${recording.state}`}>{recording.state}</span>
						</button>
						{#if selected?.id === recording.id && renaming !== recording.id}
							<button class="rename-edit" type="button" title="Rename recording" aria-label="Rename recording" onclick={() => startRename(recording)}><Icon name="pencil" size={14} /></button>
						{/if}
					</div>

					{#if selected?.id === recording.id}
						<div class="record-actions">
							{#if renaming === recording.id}
								<div class="rename-row">
									<input
										type="text"
										{@attach autofocus}
										bind:value={draft}
										placeholder="Untitled capture"
										aria-label="Recording title"
										onkeydown={(event) => {
											if (event.key === 'Enter') commitRename(recording);
											else if (event.key === 'Escape') cancelRename();
										}}
									/>
									<button type="button" onclick={() => commitRename(recording)}>Save</button>
									<button type="button" onclick={cancelRename}>Cancel</button>
								</div>
							{/if}
							{#if renameError?.id === recording.id}
								<div class="rename-error" role="alert">{renameError.message}</div>
							{/if}
							{#if recording.state === 'uploading'}
								<p class="audio-note">Audio available after upload.</p>
							{:else}
								<audio class="audio-player" controls preload="none" src={audioUrl(loadApiConfig(), recording.id)}></audio>
							{/if}
							<div class="artifact-actions" aria-label="Open artifact">
								<button type="button" onclick={() => showArtifact(recording, 'transcribe', 'Transcript')}>Transcript</button>
								<button type="button" onclick={() => showArtifact(recording, 'merge_speakers', 'Diarized transcript')}>Speakers</button>
								<button type="button" onclick={() => showArtifact(recording, 'summarize', 'Summary')}>Summary</button>
								<button type="button" onclick={() => showArtifact(recording, 'transcribe', 'Segments JSON', 'segments.json')}>JSON</button>
							</div>
							{#if recording.state === 'done' || recording.state === 'failed'}
								<div class="rerun-row">
									<span>Re-run stage</span>
									{#each STAGES as stage (stage)}
										<button type="button" title={`Re-run ${stageNames[stage]}`} aria-label={`Re-run ${stageNames[stage]}`} onclick={() => rerun(recording, stage)}><Icon name="refresh" size={14} /></button>
									{/each}
								</div>
							{/if}
						</div>
					{/if}
				</article>
			{:else}
				<div class="empty panel"><span class="empty-icon" aria-hidden="true"><Icon name="empty" size={30} /></span><strong>No matching captures</strong><small>New recordings appear here after upload begins.</small></div>
			{/each}
		{/if}
	</div>

	{#if selected && artifactLabel}
		<section class="artifact panel">
			<header>
				<div><span>OPEN ARTIFACT</span><strong>{artifactLabel}</strong></div>
				<button type="button" onclick={() => { artifactLabel = ''; artifact = ''; }} aria-label="Close artifact"><Icon name="close" size={15} /></button>
			</header>
			<pre>{artifact}</pre>
		</section>
	{/if}
</section>

<style>
	.archive-page { display: flex; flex-direction: column; gap: 14px; }
	.archive-tools { display: grid; grid-template-columns: 1fr 116px; gap: 7px; align-items: stretch; }
	.archive-tools label { display: grid; }
	.archive-tools input, .archive-tools select { height: 42px; }
	.archive-tools input[type='search'] { -webkit-appearance: none; appearance: none; }
	.archive-tools input[type='search']::-webkit-search-cancel-button { -webkit-appearance: none; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
	.archive-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.archive-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.archive-error span { color: #c6baaa; }
	.record-list { display: grid; gap: 8px; }
	.record-card { overflow: hidden; transition: border-color 120ms ease, background 120ms ease; }
	.record-card.selected { border-color: rgba(215,167,71,.44); background: linear-gradient(120deg, rgba(213,45,36,.06), rgba(215,167,71,.035)); }
	.record-heading { width: 100%; display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; padding: 11px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
	.record-heading:hover { background: rgba(255,255,255,.02); }
	.state-mark { width: 7px; height: 7px; border-radius: 50%; background: #706960; box-shadow: 0 0 0 3px rgba(112,105,96,.12); }
	.state-mark.done { background: var(--cyan); box-shadow: 0 0 9px rgba(112,215,208,.55); }
	.state-mark.processing, .state-mark.uploading { background: var(--brass); box-shadow: 0 0 9px rgba(215,167,71,.5); }
	.state-mark.failed { background: var(--red); box-shadow: 0 0 9px rgba(213,45,36,.6); }
	.record-name { min-width: 0; }
	.record-name strong, .record-name small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.record-name strong { font-size: 13px; color: #ded3c4; }
	.record-name small { margin-top: 4px; font-size: 10px; color: #8b8278; }
	.state-label { padding: 5px 7px; border: 1px solid var(--line); border-radius: 2px; color: #968d83; font-size: 9px; font-weight: 700; text-transform: capitalize; }
	.state-label.done { color: var(--cyan); border-color: rgba(112,215,208,.25); }
	.state-label.processing, .state-label.uploading { color: var(--brass); border-color: rgba(215,167,71,.25); }
	.state-label.failed { color: #f36b60; border-color: rgba(213,45,36,.35); }
	.record-actions { padding: 9px 10px 10px; border-top: 1px solid var(--line); background: rgba(0,0,0,.14); }
	.artifact-actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; }
	.artifact-actions button, .rerun-row button { min-height: 32px; display: grid; place-items: center; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.artifact-actions button:hover, .rerun-row button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.rerun-row { display: grid; grid-template-columns: 1fr repeat(4, 28px); align-items: center; gap: 5px; margin-top: 7px; }
	.rerun-row > span { font-size: 9px; font-weight: 650; color: #867d73; }
	.record-top { display: grid; grid-template-columns: 1fr auto; align-items: center; }
	.rename-edit { width: 28px; height: 28px; margin-right: 9px; display: grid; place-items: center; padding: 0; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); cursor: pointer; line-height: 0; }
	.rename-edit:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.rename-row { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 5px; margin-bottom: 7px; }
	.rename-row input { min-height: 32px; height: 32px; }
	.rename-row button { min-height: 32px; padding: 0 10px; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.rename-row button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.rename-error { margin: 0 0 7px; color: #f36b60; font-size: 11px; }
	.audio-player { width: 100%; height: 36px; margin: 0 0 7px; border-radius: 2px; background: rgba(0,0,0,.14); color-scheme: dark; accent-color: var(--brass); }
	.audio-note { margin: 0 0 7px; font-size: 11px; color: var(--ash); }
	.empty { display: grid; justify-items: center; gap: 5px; padding: 28px 16px; color: #746d64; text-align: center; }
	.empty-icon { display: grid; place-items: center; color: var(--red); line-height: 0; }
	.empty strong { color: #b5aa9c; font-size: 13px; }
	.empty small { font-size: 11px; }
	.skeleton-card { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; padding: 11px; }
	.skeleton-lines { display: grid; gap: 6px; }
	.skeleton-bar { display: block; border-radius: 2px; background: var(--iron-raised); animation: skeleton-pulse 150ms ease-in-out infinite alternate; }
	.skeleton-mark { width: 7px; height: 7px; border-radius: 50%; }
	.skeleton-title { width: 62%; height: 11px; }
	.skeleton-meta { width: 38%; height: 8px; }
	.skeleton-label { width: 46px; height: 18px; }
	@keyframes skeleton-pulse { from { opacity: 0.55; } to { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .skeleton-bar { animation: none; opacity: 0.75; } }
	.artifact { overflow: hidden; }
	.artifact header { display: flex; justify-content: space-between; align-items: center; padding: 9px 11px; border-bottom: 1px solid var(--line); background: rgba(0,0,0,.18); }
	.artifact header span, .artifact header strong { display: block; }
	.artifact header span { font-size: 9px; font-weight: 700; color: var(--red); }
	.artifact header strong { margin-top: 3px; font-size: 12px; color: #cfc3b3; }
	.artifact header button { width: 28px; height: 28px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); background: transparent; color: #8e857b; cursor: pointer; line-height: 0; }
	.artifact pre { max-height: 230px; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; color: #c7bbad; font: 11px/1.6 "SFMono-Regular", Consolas, monospace; }
</style>
