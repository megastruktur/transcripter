<script lang="ts">
	let serverUrl = $state('');
	let token = $state('');
</script>

<section>
	<h1>Settings</h1>
	<label>
		Server URL
		<input type="url" placeholder="http://megaserver:8080" bind:value={serverUrl} />
	</label>
	<label>
		Token
		<input type="password" bind:value={token} />
	</label>
	<button disabled>Test connection</button>
	<p>Stub — wiring lands in T10.</p>
</section>

<style>
	label {
		display: block;
		margin-bottom: 0.75rem;
	}
	input {
		display: block;
		margin-top: 0.25rem;
	}
</style>
