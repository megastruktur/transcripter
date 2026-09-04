<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import dagre from '@dagrejs/dagre';
	import {
		SvelteFlow,
		Background,
		useSvelteFlow,
		type Node,
		type Edge
	} from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';
	import LatticeNode from '$lib/lattice/LatticeNode.svelte';
	import LatticeEdge from '$lib/lattice/LatticeEdge.svelte';
	import type { GraphEntity, GraphRelation } from '$lib/api.svelte';

	/** Full-page PCB canvas: dagre layout, aspect-aware rank direction
	 * (portrait TB, landscape LR), and viewport ownership. The view
	 * refits on container resize until the user navigates solo — manual
	 * pan/zoom emits onmove with a real event, programmatic fitView with
	 * null, so the two are distinguishable. Must render inside
	 * <SvelteFlowProvider>: useSvelteFlow reads the provider context. */

	let {
		entities,
		relations,
		onpick
	}: {
		entities: GraphEntity[];
		relations: GraphRelation[];
		onpick: (slug: string) => void;
	} = $props();

	const nodeTypes = { lattice: LatticeNode };
	const edgeTypes = { trace: LatticeEdge };
	const { fitView } = useSvelteFlow();

	let host: HTMLDivElement | null = $state(null);
	// null until the ResizeObserver reports the first real box; the
	// build effect waits for it so the initial layout matches the
	// container's aspect instead of flashing the wrong direction.
	let landscape: boolean | null = $state(null);
	let userMoved = $state(false);

	let nodes = $state.raw<Node<Record<string, unknown>>[]>([]);
	let edges = $state.raw<Edge<Record<string, unknown>>[]>([]);

	const NODE_W = 148;
	const NODE_H = 44;

	function build(horizontal: boolean): void {
		const g = new dagre.graphlib.Graph();
		g.setDefaultEdgeLabel(() => ({}));
		g.setGraph({ rankdir: horizontal ? 'LR' : 'TB', nodesep: 28, ranksep: 52, marginx: 16, marginy: 16 });
		for (const ent of entities) g.setNode(ent.slug, { width: NODE_W, height: NODE_H });
		for (const rel of relations) g.setEdge(rel.from, rel.to);
		dagre.layout(g);
		nodes = entities.map((ent) => {
			const pos = g.node(ent.slug);
			return {
				id: ent.slug,
				type: 'lattice',
				position: { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 },
				data: { slug: ent.slug, label: ent.label, type: ent.type, sessions: ent.sessions, horizontal }
			};
		});
		edges = relations.map((rel, i) => ({
			id: `t${i}-${rel.from}-${rel.to}`,
			type: 'trace',
			source: rel.from,
			target: rel.to,
			label: rel.type,
			data: { horizontal }
		}));
	}

	/** The store only drains a queued fitView when the nodes signal
	 * re-runs — reassign the array (same content) so pure container
	 * resizes refit too, not just data changes. */
	function refit(): void {
		nodes = nodes.slice();
		void fitView();
	}

	$effect(() => {
		if (landscape === null || (entities.length === 0 && relations.length === 0)) return;
		build(landscape);
		userMoved = false;
		untrack(refit);
	});

	onMount(() => {
		if (!host) return;
		let lastW = 0;
		let lastH = 0;
		let settle: ReturnType<typeof setTimeout> | undefined;
		const ro = new ResizeObserver((entries) => {
			const box = entries[0]?.contentRect;
			if (!box || box.width < 2 || box.height < 2) return;
			if (Math.abs(box.width - lastW) < 1 && Math.abs(box.height - lastH) < 1) return;
			lastW = box.width;
			lastH = box.height;
			clearTimeout(settle);
			settle = setTimeout(() => {
				const next = box.width > box.height * 1.2;
				if (next !== landscape) {
					// Aspect crossed the threshold: re-layout (the effect
					// rebuilds and refits, resetting user navigation).
					landscape = next;
				} else if (!userMoved) {
					untrack(refit);
				}
			}, 160);
		});
		ro.observe(host);
		return () => {
			clearTimeout(settle);
			ro.disconnect();
		};
	});
</script>

<div class="lattice-canvas" bind:this={host}>
	<SvelteFlow
		{nodes}
		{edges}
		{nodeTypes}
		{edgeTypes}
		fitView
		minZoom={0.15}
		panOnDrag
		zoomOnScroll
		nodesConnectable={false}
		elementsSelectable
		proOptions={{ hideAttribution: true }}
		onmove={(event) => {
			if (event) userMoved = true;
		}}
		onnodeclick={({ node }) => onpick((node.data as { slug?: string }).slug ?? '')}
	>
		<Background gap={22} size={1.2} bgColor="transparent" patternColor="rgba(231,214,190,0.06)" />
	</SvelteFlow>
</div>

<style>
	.lattice-canvas {
		position: relative;
		overflow: hidden;
		flex: 1 1 auto;
		min-height: 240px;
		background: rgba(0, 0, 0, 0.18);
		box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.32);
		border-top: 1px solid var(--line);
		border-bottom: 1px solid var(--line);
	}
	/* Library default paints edge labels white-on-transparent; the tab
	   owns the PCB palette — void plate, ash text (WCAG on --void). */
	:global(.svelte-flow__edge-label) { background: var(--void); color: var(--ash); }
</style>
