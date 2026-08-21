<script lang="ts">
	import { clearWarnings, recorder, preflight, startRecording, stopRecording } from '$lib/stores.svelte';

	let title = $state('');
	let elapsed = $state(0);
	let starting = $state(false);
	let timer: ReturnType<typeof globalThis.setInterval> | null = null;

	$effect(() => {
		if (recorder.recording && !timer) {
			timer = setInterval(() => (elapsed += 1), 1000);
		} else if (!recorder.recording && timer) {
			clearInterval(timer);
			timer = null;
			elapsed = 0;
		}
	});

	function fmt(sec: number): string {
		const m = Math.floor(sec / 60);
		const s = sec % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}
</script>

<section>
	<h1>Recorder</h1>

	{#if preflight.current && preflight.current.error}
		<div class="error">Pre-flight: {preflight.current.error}</div>
	{/if}
	{#each recorder.warnings as w}
		<div class="warn">{w}</div>
	{/each}

	<input type="text" placeholder="Title (optional)" bind:value={title} disabled={recorder.recording} />

	{#if recorder.recording}
		<button class="stop" disabled={recorder.stopping} onclick={() => stopRecording()}
			>Stop ({fmt(elapsed)})</button
		>
		<p>frames buffered: {recorder.frames}</p>
	{:else}
		<button
			class="start"
			disabled={starting}
			onclick={async () => {
				starting = true;
				clearWarnings();
				try {
					await startRecording(title);
				} catch (e) {
					recorder.warnings.push(String(e));
				} finally {
					starting = false;
				}
			}}>Record</button
		>
	{/if}
</section>

<style>
	.error {
		color: #b00;
	}
	.warn {
		color: #960;
	}
	input {
		display: block;
		margin: 0.5rem 0;
	}
	button {
		font-size: 1.1rem;
		padding: 0.5rem 2rem;
	}
	.stop {
		background: #b00;
		color: white;
	}
</style>
