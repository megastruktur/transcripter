<script lang="ts">
	import { onMount } from 'svelte';
	import dagre from '@dagrejs/dagre';
	import {
		SvelteFlow,
		Background,
		type Node,
		type Edge
	} from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';
	import LatticeNode from '$lib/lattice/LatticeNode.svelte';
	import LatticeEdge from '$lib/lattice/LatticeEdge.svelte';
	import EmptyState from '$lib/EmptyState.svelte';
	import Skeleton from '$lib/Skeleton.svelte';
	import Icon from '$lib/Icon.svelte';
	import {
		fetchGraph,
		loadApiConfig,
		type GraphEntity,
		type GraphRelation
	} from '$lib/api.svelte';

	/** Phase D Lattice tab: the tag's knowledge graph. Desktop = PCB
	 * node-link canvas (dagre TB, orthogonal step edges); narrow
	 * viewports = edge list (node-link is unreadable at 360px). Node
	 * tap opens the entity drawer (relations of the picked entity). */

	let {
		tag,
		entitiesSeed,
		relationsSeed
	}: {
		tag: string;
		entitiesSeed: GraphEntity[];
		relationsSeed: GraphRelation[];
	} = $props();

	const nodeTypes = { lattice: LatticeNode };
	const edgeTypes = { trace: LatticeEdge };

	let loading = $state(true);
	let error = $state('');
	let unavailable = $state(false);
	let entities = $state<GraphEntity[]>([]);
	let relations = $state<GraphRelation[]>([]);
	let compact = $state(false);

	// Drawer: the tapped entity + its edges.
	let pickedSlug = $state<string | null>(null);
	const picked = $derived(entities.find((e) => e.slug === pickedSlug) ?? null);
	const pickedEdges = $derived(
		relations.filter((r) => r.from === pickedSlug || r.to === pickedSlug)
	);

	// Edge-list filter (compact mode).
	let filterText = $state('');

	const nodes = $state.raw<Node<Record<string, unknown>>[]>([]);
	const edges = $state.raw<Edge<Record<string, unknown>>[]>([]);

	const _NODE_W = 148;
	const _NODE_H = 44;

	function buildCanvas(): void {
		const g = new dagre.graphlib.Graph();
		g.setDefaultEdgeLabel(() => ({}));
		g.setGraph({ rankdir: 'TB', nodesep: 28, ranksep: 52, marginx: 16, marginy: 16 });
		for (const ent of entities) g.setNode(ent.slug, { width: _NODE_W, height: _NODE_H });
		for (const rel of relations) g.setEdge(rel.from, rel.to);
		dagre.layout(g);
		const positioned = entities.map((ent) => {
			const pos = g.node(ent.slug);
			return {
				id: ent.slug,
				type: 'lattice',
				position: { x: pos.x - _NODE_W / 2, y: pos.y - _NODE_H / 2 },
				data: { slug: ent.slug, label: ent.label, type: ent.type, sessions: ent.sessions }
			};
		});
		const traced = relations.map((rel, i) => ({
			id: `t${i}-${rel.from}-${rel.to}`,
			type: 'trace',
			source: rel.from,
			target: rel.to,
			label: rel.type
		}));
		nodes.splice(0, nodes.length, ...positioned);
		edges.splice(0, edges.length, ...traced);
	}

	const filteredRelations = $derived(
		relations.filter((r) => {
			if (!filterText.trim()) return true;
			const q = filterText.trim().toLowerCase();
			const from = entities.find((e) => e.slug === r.from);
			const to = entities.find((e) => e.slug === r.to);
			return (
				r.from.includes(q) ||
				r.to.includes(q) ||
				(from?.label.toLowerCase().includes(q) ?? false) ||
				(to?.label.toLowerCase().includes(q) ?? false) ||
				r.type.toLowerCase().includes(q)
			);
		})
	);

	async function refresh(): Promise<void> {
		loading = true;
		error = '';
		unavailable = false;
		try {
			const data = await fetchGraph(loadApiConfig(), tag);
			entities = data.entities;
			relations = data.relations;
			if (!compact) buildCanvas();
		} catch (caught) {
			const status = (caught as { status?: number }).status;
			if (status === 409) unavailable = true;
			else error = caught instanceof Error ? caught.message : String(caught);
		} finally {
			loading = false;
		}
	}

	function onResize(): void {
		const wasCompact = compact;
		compact = window.innerWidth <= 420;
		// Crossing the breakpoint (re)builds the canvas: entering wide
		// needs positions, leaving wide makes them stale (harmless).
		if (!wasCompact && !compact && entities.length > 0) buildCanvas();
	}

	onMount(() => {
		// Seed from the already-fetched timeline when present (instant
		// render); the graph endpoint refines with session counts.
		if (entitiesSeed.length > 0) {
			entities = entitiesSeed;
			relations = relationsSeed;
			loading = false;
			if (!compact) buildCanvas();
		}
		onResize();
		window.addEventListener('resize', onResize);
		void refresh();
		return () => window.removeEventListener('resize', onResize);
	});

	function labelOf(slug: string): string {
		return entities.find((e) => e.slug === slug)?.label ?? slug;
	}
</script>

{#if loading}
	<Skeleton variant="panel-tag" />
{:else if unavailable}
	<EmptyState
		icon="speakers"
		title="Graph not configured"
		hint="This server has no graph backend enabled; the Lattice stays dark."
	/>
{:else if error}
	<div class="lattice-error" role="alert"><strong>Lattice unavailable</strong><span>{error}</span></div>
{:else if entities.length === 0}
	<EmptyState
		icon="speakers"
		title="No entities extracted yet"
		hint="Run the pipeline on tagged recordings to populate the lattice."
	/>
{:else if compact}
	<div class="lattice-list">
		<input
			class="lattice-filter"
			type="text"
			placeholder="Filter edges…"
			aria-label="Filter relations"
			bind:value={filterText}
		/>
		{#if filteredRelations.length === 0}
			<p class="lattice-empty">No edges carry “{filterText}”.</p>
		{:else}
			{#each filteredRelations as rel (rel.from + rel.to + rel.type)}
				<button
					class="list-row lattice-edge-row"
					type="button"
					onclick={() => (pickedSlug = rel.from === pickedSlug ? rel.to : rel.from)}
					title="Open {labelOf(rel.from)}"
				>
					<span class="lattice-edge-text">
						<strong>{labelOf(rel.from)}</strong>
						<em>{rel.type}</em>
						<strong>{labelOf(rel.to)}</strong>
					</span>
					<span class="lattice-edge-sessions">{rel.sessions}</span>
				</button>
			{/each}
		{/if}
	</div>
{:else}
	<div class="lattice-canvas">
		<SvelteFlow
			{nodes}
			{edges}
			{nodeTypes}
			{edgeTypes}
			fitView
			panOnDrag
			zoomOnScroll
			nodesConnectable={false}
			elementsSelectable
			proOptions={{ hideAttribution: true }}
			onnodeclick={({ node }) => (pickedSlug = (node.data as { slug?: string }).slug ?? null)}
		>
			<Background gap={22} size={1.2} bgColor="transparent" patternColor="rgba(231,214,190,0.06)" />
		</SvelteFlow>
	</div>
{/if}

{#if picked}
	<aside class="lattice-drawer" aria-label={`Entity ${picked.label}`}>
		<header class="lattice-drawer-head">
			<strong>{picked.label}</strong>
			<small>{picked.type || 'entity'} · {picked.sessions} session{picked.sessions === 1 ? '' : 's'}</small>
			<button class="lattice-drawer-close" type="button" onclick={() => (pickedSlug = null)} aria-label="Close entity panel">
				<Icon name="close" size={11} strokeWidth={1.6} />
			</button>
		</header>
		{#if pickedEdges.length === 0}
			<p class="lattice-empty">No relations touch this entity.</p>
		{:else}
			{#each pickedEdges as rel (rel.from + rel.to + rel.type)}
				<div class="lattice-drawer-edge">
					<span>{rel.from === picked.slug ? '→' : '←'}</span>
					<strong>{labelOf(rel.from === picked.slug ? rel.to : rel.from)}</strong>
					<em>{rel.type}</em>
					<small>{rel.sessions}×</small>
				</div>
			{/each}
		{/if}
	</aside>
{/if}

<style>
	.lattice-canvas {
		position: relative;
		overflow: hidden;
		height: min(46vh, 360px);
		min-height: 240px;
		background: rgba(0, 0, 0, 0.18);
		box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.32);
		border-top: 1px solid var(--line);
		border-bottom: 1px solid var(--line);
	}
	/* Library default paints edge labels white-on-transparent; the tab
	   owns the PCB palette — void plate, ash text (WCAG on --void). */
	:global(.svelte-flow__edge-label) { background: var(--void); color: var(--ash); }
	.lattice-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.lattice-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.lattice-error span { color: #c6baaa; }
	.lattice-list { display: grid; }
	.lattice-filter { min-width: 0; height: 34px; padding: 0 10px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 12px; margin-bottom: 6px; }
	.lattice-filter::placeholder { color: var(--ash); }
	.lattice-filter:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }
	.lattice-edge-row { grid-template-columns: 1fr auto; }
	.lattice-edge-row:not(:last-child) { border-bottom: 1px solid var(--line); }
	.lattice-edge-text { min-width: 0; display: flex; align-items: baseline; gap: 6px; overflow: hidden; }
	.lattice-edge-text strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: #ded3c4; }
	.lattice-edge-text em { flex: 0 0 auto; color: var(--ash); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; font-style: normal; }
	.lattice-edge-sessions { font-size: 10px; color: #8b8278; font-variant-numeric: tabular-nums; }
	.lattice-empty { margin: 0; padding: 10px 2px; color: var(--ash); font-size: 11px; }
	.lattice-drawer { margin-top: 8px; padding: 9px 11px; background: rgba(0,0,0,.22); border-top: 1px solid var(--line); box-shadow: inset 0 1px 3px rgba(0,0,0,.3); }
	.lattice-drawer-head { display: grid; grid-template-columns: 1fr auto; gap: 2px 8px; align-items: center; }
	.lattice-drawer-head strong { font-size: 12px; color: var(--bone); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.lattice-drawer-head small { grid-column: 1; font-size: 9px; color: #8b8278; }
	.lattice-drawer-close { grid-column: 2; grid-row: 1 / span 2; display: grid; place-items: center; width: 26px; height: 26px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); cursor: pointer; }
	.lattice-drawer-close:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.lattice-drawer-edge { display: flex; align-items: baseline; gap: 7px; padding: 6px 0; border-bottom: 1px solid var(--line); }
	.lattice-drawer-edge:last-child { border-bottom: 0; }
	.lattice-drawer-edge span { color: var(--brass); font-size: 10px; }
	.lattice-drawer-edge strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; color: #ded3c4; }
	.lattice-drawer-edge em { color: var(--ash); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; font-style: normal; }
	.lattice-drawer-edge small { margin-left: auto; font-size: 10px; color: #8b8278; font-variant-numeric: tabular-nums; }
</style>
