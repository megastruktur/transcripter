<script lang="ts">
	let { children } = $props();
</script>

<main>
	<nav>
		<a href="/">Recorder</a>
		<a href="/recordings">Recordings</a>
		<a href="/settings">Settings</a>
	</nav>
	{@render children()}
</main>

<style>
	nav {
		display: flex;
		gap: 1rem;
		padding: 0.5rem 1rem;
		border-bottom: 1px solid #ddd;
	}
	main {
		font-family: system-ui, sans-serif;
	}
</style>
