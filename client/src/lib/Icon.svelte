	<script module lang="ts">
	export type IconName =
		| 'mark'
		| 'record'
		| 'library'
		| 'settings'
		| 'collapse'
		| 'minimize'
		| 'close'
		| 'microphone'
		| 'monitor'
		| 'link'
		| 'refresh'
		| 'search'
		| 'stop'
		| 'dot'
		| 'shield'
		| 'empty'
		| 'pencil'
		| 'trash'
		| 'back'
		| 'transcript'
		| 'speakers'
		| 'summary'
		| 'json'
		| 'chunk'
		| 'diarize'
		| 'enrich';
</script>

<script lang="ts">
	let { name, size = 20, strokeWidth = 1.8 }: { name: IconName; size?: number; strokeWidth?: number } = $props();
</script>

<svg
	class="icon"
	width={size}
	height={size}
	viewBox={name === 'mark' ? '0 0 64 64' : '0 0 24 24'}
	fill="none"
	stroke="currentColor"
	stroke-width={strokeWidth}
	stroke-linecap="round"
	stroke-linejoin="round"
	aria-hidden="true"
	focusable="false"
>
	{#if name === 'mark'}
		<path class="mark-cog" d="M28 3h8l2 7a24 24 0 0 1 5 2l6-4 6 6-4 6a24 24 0 0 1 2 5l8 2v9l-8 2a24 24 0 0 1-2 5l4 6-6 6-6-4a24 24 0 0 1-5 2l-2 8h-8l-2-8a24 24 0 0 1-5-2l-6 4-6-6 4-6a24 24 0 0 1-2-5l-8-2v-9l8-2a24 24 0 0 1 2-5l-4-6 6-6 6 4a24 24 0 0 1 5-2z" />
		<circle class="mark-ring" cx="32" cy="32" r="14" />
		<path class="mark-sigil" d="M32 18v28M25 24h14M25 40h14M22 32h20" />
	{:else if name === 'record'}
		<circle cx="12" cy="12" r="8" />
		<circle cx="12" cy="12" r="3" fill="currentColor" stroke="none" />
	{:else if name === 'library'}
		<path d="M4.5 7.5h15v12h-15zM3.5 4.5h17v3h-17zM9 12h6" />
	{:else if name === 'settings'}
		<path d="M4 6h4M12 6h8M4 12h9M17 12h3M4 18h3M11 18h9" />
		<circle cx="10" cy="6" r="2" />
		<circle cx="15" cy="12" r="2" />
		<circle cx="9" cy="18" r="2" />
	{:else if name === 'collapse'}
		<path d="m6.5 9 5.5 5.5L17.5 9" />
	{:else if name === 'minimize'}
		<path d="M6 12h12" />
	{:else if name === 'close'}
		<path d="m7 7 10 10M17 7 7 17" />
	{:else if name === 'microphone'}
		<rect x="9" y="3" width="6" height="11" rx="3" />
		<path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M9 21h6" />
	{:else if name === 'monitor'}
		<rect x="3" y="4" width="18" height="14" rx="2" />
		<path d="M8 21h8M12 18v3" />
	{:else if name === 'link'}
		<path d="m9.5 14.5-1 1a3.54 3.54 0 0 1-5-5l3-3a3.54 3.54 0 0 1 5 0M14.5 9.5l1-1a3.54 3.54 0 0 1 5 5l-3 3a3.54 3.54 0 0 1-5 0M8.5 15.5l7-7" />
	{:else if name === 'refresh'}
		<path d="M20 7v5h-5M4 17v-5h5M18.2 9A7 7 0 0 0 6.4 6.4L4 9M5.8 15A7 7 0 0 0 17.6 17.6L20 15" />
	{:else if name === 'search'}
		<circle cx="10.5" cy="10.5" r="5.5" />
		<path d="m15 15 4 4" />
	{:else if name === 'stop'}
		<rect x="7" y="7" width="10" height="10" rx="1" fill="currentColor" stroke="none" />
	{:else if name === 'dot'}
		<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none" />
	{:else if name === 'shield'}
		<path d="m12 3 7 3v5c0 4.6-2.8 8-7 10-4.2-2-7-5.4-7-10V6z" />
		<path d="M9 12h6" />
	{:else if name === 'empty'}
		<circle cx="12" cy="12" r="8" />
		<path d="m6.3 17.7 11.4-11.4" />
	{:else if name === 'pencil'}
		<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" />
	{:else if name === 'trash'}
		<path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5M14 11v5" />
	{:else if name === 'back'}
		<path d="M14.5 6 9 12l5.5 6" />
	{:else if name === 'transcript'}
		<path d="M4 10v4M8 6.5v11M12 3.5v17M16 8v8M20 10.5v3" />
	{:else if name === 'speakers'}
		<circle cx="9" cy="8" r="3.2" />
		<path d="M3.4 19.2c.7-3.4 2.9-5.2 5.6-5.2s4.9 1.8 5.6 5.2" />
		<circle cx="16.8" cy="8.8" r="2.5" />
		<path d="M15.9 14.2c2.5.3 4.2 2 4.7 4.6" />
	{:else if name === 'summary'}
		<path d="M6 3.5h8l4 4v13H6z" />
		<path d="M14 3.5v4h4M9 12h6M9 15.5h6" />
	{:else if name === 'json'}
		<path d="M9.5 4.5c-2.2 0-3 .9-3 2.6v2.3c0 1.5-.9 2.6-2.3 2.6 1.4 0 2.3 1.1 2.3 2.6v2.3c0 1.7.8 2.6 3 2.6" />
		<path d="M14.5 4.5c2.2 0 3 .9 3 2.6v2.3c0 1.5.9 2.6 2.3 2.6-1.4 0-2.3 1.1-2.3 2.6v2.3c0 1.7-.8 2.6-3 2.6" />
	{:else if name === 'chunk'}
		<path d="M4 8.5h16v7H4zM9.3 8.5v7M14.6 8.5v7" />
	{:else if name === 'diarize'}
		<path d="M4 10v4M7.5 7.5v9M11 9.5v5" />
		<circle cx="16.8" cy="8.4" r="2.3" />
		<path d="M14 18c.5-2.3 1.5-3.4 2.8-3.4s2.3 1.1 2.8 3.4" />
	{:else if name === 'enrich'}
		<circle cx="6" cy="6.5" r="2.3" />
		<circle cx="18" cy="6.5" r="2.3" />
		<circle cx="12" cy="17.5" r="2.3" />
		<path d="M8.2 7.3h7.6M7 8.6l3.9 6.7M17 8.6l-3.9 6.7" />
	{/if}
</svg>

<style>
	.icon {
		display: block;
		flex: 0 0 auto;
		overflow: visible;
	}

	.mark-cog {
		fill: #1b1613;
		stroke: var(--brass, #d7a747);
		stroke-width: 1.4;
	}

	.mark-ring {
		fill: #160f0d;
		stroke: var(--red, #d52d24);
		stroke-width: 2;
	}

	.mark-sigil {
		fill: none;
		stroke: var(--bone, #e9dfcf);
		stroke-width: 2;
		stroke-linecap: square;
	}
</style>
