<script lang="ts">
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';

	/** Lattice entity node: a PCB pad — iron-raised surface, type accent
	 * as the left solder-mask stripe, bone label, ash type micro-label.
	 * The tab builds node.data as { slug, label, type, sessions }; the
	 * generic-free NodeProps keeps this compatible with NodeTypes. */
	let { data }: NodeProps = $props();

	const entity = $derived(data ?? {});
	const label = $derived(typeof entity.label === 'string' ? entity.label : '');
	const type = $derived(typeof entity.type === 'string' ? entity.type : '');
	const accent = $derived(typeAccent(type));

	function typeAccent(type: string): string {
		const key = (type ?? '').trim().toLowerCase();
		const explicit: Record<string, string> = {
			person: 'var(--brass)',
			org: 'var(--red)',
			project: '#e9dfcf',
			place: '#9e9183',
			thing: 'var(--cyan)'
		};
		if (key && explicit[key] !== undefined) return explicit[key];
		if (!key) return 'var(--brass)';
		// Unknown profile types: deterministic hash onto the ramp.
		let hash = 0;
		for (const ch of key) hash = (hash * 31 + ch.codePointAt(0)!) >>> 0;
		const ramp = ['var(--brass)', 'var(--red)', '#e9dfcf', '#9e9183'];
		return ramp[hash % ramp.length] ?? 'var(--brass)';
	}
</script>

<Handle type="target" position={Position.Top} class="lattice-handle" />
<div class="lattice-node" style="--node-accent: {accent}" title={`${label} · ${type || 'entity'}`}>
	<span class="lattice-node-label">{label}</span>
	{#if type}
		<span class="lattice-node-type">{type}</span>
	{/if}
</div>
<Handle type="source" position={Position.Bottom} class="lattice-handle" />

<style>
	.lattice-node {
		display: grid;
		gap: 2px;
		min-width: 84px;
		max-width: 148px;
		padding: 6px 9px 6px 8px;
		border-left: 2px solid var(--node-accent, var(--brass));
		background: var(--iron-raised);
		border-radius: 0 3px 3px 0;
		box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.35);
	}
	.lattice-node-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: #ded3c4;
		font-size: 11px;
		font-weight: 650;
		line-height: 1.35;
	}
	.lattice-node-type {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: #8b8278;
		font-size: 8px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
</style>
