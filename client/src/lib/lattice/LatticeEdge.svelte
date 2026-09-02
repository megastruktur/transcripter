<script lang="ts">
	import { BaseEdge, EdgeLabel, getEdgeCenter, type EdgeProps } from '@xyflow/svelte';

	/** PCB trace: orthogonal step with right-angle corners only, ash
	 * stroke, the relation type as a void-backed label at the trace's
	 * elbow (non-interactive — edge actions live in the node drawer). */
	let { id, sourceX, sourceY, targetX, targetY, label }: EdgeProps = $props();

	const midY = $derived((targetY - sourceY) / 2 + sourceY);
	const edgePath = $derived(
		`M ${sourceX} ${sourceY} L ${sourceX} ${midY} L ${targetX} ${midY} L ${targetX} ${targetY}`
	);
	const center = $derived(getEdgeCenter({ sourceX, sourceY, targetX, targetY }));
</script>

<BaseEdge {id} path={edgePath} style="stroke: rgba(158,145,131,.45); stroke-width: 1.5;" />
{#if label}
	<EdgeLabel x={center[0]} y={center[1]}>
		<span class="lattice-edge-label">{label}</span>
	</EdgeLabel>
{/if}
