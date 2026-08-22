<script lang="ts">
	import { onMount } from 'svelte';
	import Icon from '$lib/Icon.svelte';
	import { retryPendingUploads } from '$lib/stores.svelte';
	import { listRecordings, regenerate, fetchArtifact, loadApiConfig, type Recording, type StageStatus } from '$lib/api.svelte';

	let recordings = $state<Recording[]>([]);
	let error = $state('');
	let selected = $state<Recording | null>(null);
	let artifact = $state('');
	let artifactLabel = $state('');
	let query = $state('');
	let filter = $state<'all' | Recording['state']>('all');
	let pollTimer: ReturnType<typeof globalThis.setInterval> | null = null;

	const STAGES = ['transcribe', 'diarize', 'merge_speakers', 'summarize'] as const;
	const stageNames: Record<(typeof STAGES)[number], string> = {
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
			error = '';
			if (selected) selected = recordings.find((recording) => recording.id === selected?.id) ?? null;
		} catch (caught) {
			error = String(caught);
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

	function dateLabel(value: string): string {
		return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
	}

	function durationLabel(seconds: number | null): string {
		if (seconds === null) return '—';
		const minutes = Math.floor(seconds / 60);
		return `${minutes}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;
	}

	function stageStatus(recording: Recording, stage: (typeof STAGES)[number]): StageStatus {
		return recording.stages.find((item) => item.kind === stage)?.status ?? 'pending';
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

<svelte:head><title>Archive · Transcripter</title></svelte:head>

<section class="page archive-page">
	<header>
		<h1 class="page-title">Recordings</h1>
		<p class="page-intro">Find recordings, follow processing progress, and open transcripts or summaries.</p>
	</header>

	<div class="archive-tools">
		<label class="search-wrap">
			<span class="sr-only">Search recordings</span>
			<span class="search-icon" aria-hidden="true"><Icon name="search" size={15} /></span>
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
		{#each filteredRecordings as recording (recording.id)}
			<article class:selected={selected?.id === recording.id} class="record-card panel">
				<button class="record-heading" type="button" onclick={() => (selected = selected?.id === recording.id ? null : recording)}>
					<span class={`state-mark ${recording.state}`} aria-hidden="true"></span>
					<span class="record-name"><strong>{recording.title || 'Untitled capture'}</strong><small>{dateLabel(recording.created_at)} · {durationLabel(recording.duration_sec)}</small></span>
					<span class={`state-label ${recording.state}`}>{recording.state}</span>
				</button>

				<div class="stage-line" aria-label="Processing stages">
					{#each STAGES as stage (stage)}
						<span class={`stage ${stageStatus(recording, stage)}`} title={`${stageNames[stage]}: ${stageStatus(recording, stage)}`}>
							<i></i>{stageNames[stage]}
						</span>
					{/each}
				</div>

				{#if selected?.id === recording.id}
					<div class="record-actions">
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
	.archive-tools { display: grid; grid-template-columns: 1fr 116px; gap: 7px; }
	.search-wrap { position: relative; }
	.search-icon { position: absolute; left: 11px; top: 12px; display: grid; place-items: center; color: var(--brass); z-index: 1; line-height: 0; }
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
	.stage-line { display: grid; grid-template-columns: repeat(4, 1fr); border-top: 1px solid var(--line); }
	.stage { display: flex; align-items: center; gap: 5px; min-width: 0; padding: 8px 5px; border-right: 1px solid var(--line); color: #766f67; font-size: 9px; text-transform: none; }
	.stage:last-child { border-right: 0; }
	.stage i { width: 4px; height: 4px; flex: 0 0 auto; border-radius: 50%; background: #55504a; }
	.stage.done { color: #9bcbc8; }
	.stage.done i { background: var(--cyan); }
	.stage.running { color: var(--brass); }
	.stage.running i { background: var(--brass); box-shadow: 0 0 6px var(--brass); }
	.stage.failed { color: #d7675f; }
	.stage.failed i { background: var(--red); }
	.record-actions { padding: 9px 10px 10px; border-top: 1px solid var(--line); background: rgba(0,0,0,.14); }
	.artifact-actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; }
	.artifact-actions button, .rerun-row button { min-height: 32px; display: grid; place-items: center; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.artifact-actions button:hover, .rerun-row button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.rerun-row { display: grid; grid-template-columns: 1fr repeat(4, 28px); align-items: center; gap: 5px; margin-top: 7px; }
	.rerun-row > span { font-size: 9px; font-weight: 650; color: #867d73; }
	.empty { display: grid; justify-items: center; gap: 5px; padding: 28px 16px; color: #746d64; text-align: center; }
	.empty-icon { display: grid; place-items: center; color: var(--red); line-height: 0; }
	.empty strong { color: #b5aa9c; font-size: 13px; }
	.empty small { font-size: 11px; }
	.artifact { overflow: hidden; }
	.artifact header { display: flex; justify-content: space-between; align-items: center; padding: 9px 11px; border-bottom: 1px solid var(--line); background: rgba(0,0,0,.18); }
	.artifact header span, .artifact header strong { display: block; }
	.artifact header span { font-size: 9px; font-weight: 700; color: var(--red); }
	.artifact header strong { margin-top: 3px; font-size: 12px; color: #cfc3b3; }
	.artifact header button { width: 28px; height: 28px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); background: transparent; color: #8e857b; cursor: pointer; line-height: 0; }
	.artifact pre { max-height: 230px; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; color: #c7bbad; font: 11px/1.6 "SFMono-Regular", Consolas, monospace; }
</style>
