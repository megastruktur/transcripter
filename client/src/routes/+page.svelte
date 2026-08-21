<script lang="ts">
	// T8 wires capture commands; stub UI only.
	let recording = $state(false);
</script>

<section>
	<h1>Recorder</h1>
	<button disabled>Record</button>
	<p>{recording ? 'Recording…' : 'Idle (stub — capture lands in T8)'}</p>
</section>
