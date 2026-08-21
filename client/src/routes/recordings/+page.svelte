<script lang="ts">
	import { onMount } from 'svelte';
	import {
		listRecordings,
		regenerate,
		fetchArtifact,
		loadApiConfig,
		type Recording
	} from '$lib/api.svelte';

	let recordings = $state<Recording[]>([]);
	let error = $state('');
	let selected = $state<Recording | null>(null);
	let artifact = $state('');
	let pollTimer: ReturnType<typeof globalThis.setInterval> | null = null;

	const STAGES = ['transcribe', 'diarize', 'merge_speakers', 'summarize'] as const;

	async function refresh() {
		try {
			recordings = await listRecordings(loadApiConfig());
			error = '';
			if (selected) {
				selected = recordings.find((r) => r.id === selected?.id) ?? null;
			}
		} catch (e) {
			error = String(e);
		}
	}

	onMount(() => {
		refresh();
		pollTimer = setInterval(refresh, 3000);
		return () => {
			if (pollTimer) clearInterval(pollTimer);
		};
	});

	function statusColor(s: string): string {
		return { done: 'green', running: 'orange', failed: 'red', skipped: 'gray' }[s] ?? 'black';
	}

	async function showArtifact(rec: Recording, stage: string, file?: string) {
		try {
			artifact = await fetchArtifact(loadApiConfig(), rec.id, stage, file);
		} catch {
			artifact = '(not generated yet)';
		}
	}
</script>

<section>
	<h1>Recordings</h1>
	{#if error}
		<div class="error">{error}</div>
	{/if}

	<table>
		<thead>
			<tr><th>Title</th><th>State</th>{#each STAGES as s}<th>{s}</th>{/each}<th></th></tr>
		</thead>
		<tbody>
			{#each recordings as rec (rec.id)}
				<tr class:active={selected?.id === rec.id}>
					<td
						role="link"
						tabindex="0"
						onclick={() => (selected = rec)}
						onkeydown={(e) => e.key === 'Enter' && (selected = rec)}>{rec.title || rec.id.slice(0, 8)}</td
					>
					<td>{rec.state}</td>
					{#each rec.stages as s (s.kind)}
						<td style="color: {statusColor(s.status)}">{s.status}</td>
					{/each}
					<td>
						{#if rec.state === 'done' || rec.state === 'failed'}
							{#each STAGES as stage}
								<button onclick={() => regenerate(loadApiConfig(), rec.id, stage).then(refresh)}>
									↻ {stage.slice(0, 4)}
								</button>
							{/each}
						{/if}
					</td>
				</tr>
			{:else}
				<tr><td colspan="6">No recordings yet</td></tr>
			{/each}
		</tbody>
	</table>

	{#if selected}
		<hr />
		<h2>{selected.title || selected.id.slice(0, 8)}</h2>
		<nav>
			<button onclick={() => showArtifact(selected!, 'transcribe')}>Transcript</button>
			<button onclick={() => showArtifact(selected!, 'merge_speakers')}>Diarized</button>
			<button onclick={() => showArtifact(selected!, 'summarize')}>Summary</button>
			<button onclick={() => showArtifact(selected!, 'transcribe', 'segments.json')}>Segments JSON</button>
		</nav>
		<pre>{artifact}</pre>
	{/if}
</section>

<style>
	table {
		border-collapse: collapse;
		width: 100%;
	}
	th,
	td {
		border: 1px solid #ccc;
		padding: 0.3rem 0.6rem;
		text-align: left;
	}
	tr.active {
		background: #eef;
	}
	[role='link'] {
		cursor: pointer;
		text-decoration: underline;
	}
	pre {
		background: #f6f6f6;
		padding: 1rem;
		max-height: 50vh;
		overflow: auto;
	}
	.error {
		color: #b00;
	}
</style>
