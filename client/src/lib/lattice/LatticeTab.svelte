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
		createGraphRelation,
		deleteGraphEntity,
		deleteGraphRelation,
		fetchGraph,
		loadApiConfig,
		mergeGraphEntities,
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

	// --- Drawer edit actions (phase A surface): entity delete/merge,
	// relation create/delete. Deletes are two-step (arm → confirm) —
	// never a native dialog; one recess open at a time.
	let actSaving = $state(false);
	let actError = $state('');
	let mergeOpen = $state(false);
	let linkOpen = $state(false);
	let delArmed = $state(false);
	let mergeTarget = $state('');
	let linkTarget = $state('');
	let linkType = $state('');
	let actionFeedback = $state('');
	let edgeArmed = $state('');

	const drawerTargets = $derived(entities.filter((e) => e.slug !== pickedSlug));

	function resetActions(): void {
		mergeOpen = false;
		linkOpen = false;
		delArmed = false;
		edgeArmed = '';
		actError = '';
		actionFeedback = '';
	}

	function openMerge(): void {
		resetActions();
		mergeOpen = true;
		mergeTarget = '';
	}

	function openLink(): void {
		resetActions();
		linkOpen = true;
		linkTarget = '';
		linkType = '';
	}

	async function runAction(work: () => Promise<unknown>): Promise<boolean> {
		actSaving = true;
		actError = '';
		try {
			await work();
			await refresh();
			return true;
		} catch {
			actError = 'The server refused the edit — nothing changed.';
			return false;
		} finally {
			actSaving = false;
		}
	}

	async function doDeleteEntity(): Promise<void> {
		if (!picked) return;
		const slug = picked.slug;
		if (await runAction(() => deleteGraphEntity(loadApiConfig(), tag, slug))) {
			pickedSlug = null;
			resetActions();
		} else {
			delArmed = false;
		}
	}

	async function doMerge(): Promise<void> {
		if (!picked || !mergeTarget) return;
		const source = picked.slug;
		if (await runAction(() =>
			mergeGraphEntities(loadApiConfig(), tag, source, mergeTarget, actionFeedback.trim())
		)) {
			pickedSlug = mergeTarget; // the survivor keeps the drawer open
			resetActions();
		}
	}

	async function doLink(): Promise<void> {
		if (!picked || !linkTarget || !linkType.trim()) return;
		if (await runAction(() =>
			createGraphRelation(
				loadApiConfig(),
				tag,
				picked!.slug,
				linkTarget,
				linkType.trim(),
				actionFeedback.trim()
			)
		)) {
			resetActions();
		}
	}

	async function doDeleteEdge(rel: GraphRelation): Promise<void> {
		await runAction(() => deleteGraphRelation(loadApiConfig(), tag, rel.from, rel.to, rel.type));
		edgeArmed = '';
	}

	function edgeKey(rel: GraphRelation): string {
		return `${rel.from}|${rel.to}|${rel.type}`;
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
			<button class="lattice-drawer-close" type="button" onclick={() => { pickedSlug = null; resetActions(); }} aria-label="Close entity panel">
				<Icon name="close" size={11} strokeWidth={1.6} />
			</button>
		</header>
		{#if mergeOpen}
			<form
				class="lattice-act-form"
				onsubmit={(e) => {
					e.preventDefault();
					void doMerge();
				}}
			>
				<label class="lattice-act-label" for="lattice-merge-target">Fold “{picked.label}” into</label>
				<select id="lattice-merge-target" class="lattice-act-select" bind:value={mergeTarget} disabled={actSaving}>
					<option value="" disabled>pick the surviving entity…</option>
					{#each drawerTargets as ent (ent.slug)}
						<option value={ent.slug}>{ent.label}</option>
					{/each}
				</select>
				<input class="lattice-act-input" type="text" aria-label="Correction rule (optional)" bind:value={actionFeedback} disabled={actSaving} maxlength="500" placeholder="correction rule — teaches future extractions" />
				<div class="lattice-act-row">
					<button class="lattice-act-save" type="submit" disabled={actSaving || !mergeTarget}>
						<Icon name="refresh" size={11} strokeWidth={1.6} />
						{actSaving ? 'Merging…' : 'Merge'}
					</button>
					<button class="lattice-act-cancel" type="button" disabled={actSaving} onclick={resetActions}>Cancel</button>
				</div>
			</form>
		{:else if linkOpen}
			<form
				class="lattice-act-form"
				onsubmit={(e) => {
					e.preventDefault();
					void doLink();
				}}
			>
				<label class="lattice-act-label" for="lattice-link-target">Link “{picked.label}” to</label>
				<select id="lattice-link-target" class="lattice-act-select" bind:value={linkTarget} disabled={actSaving}>
					<option value="" disabled>pick an entity…</option>
					{#each drawerTargets as ent (ent.slug)}
						<option value={ent.slug}>{ent.label}</option>
					{/each}
				</select>
				<input class="lattice-act-input" type="text" aria-label="Relation type" bind:value={linkType} disabled={actSaving} maxlength="100" placeholder="relation type (e.g. member_of)" />
				<input class="lattice-act-input" type="text" aria-label="Correction rule (optional)" bind:value={actionFeedback} disabled={actSaving} maxlength="500" placeholder="correction rule — teaches future extractions" />
				<div class="lattice-act-row">
					<button class="lattice-act-save" type="submit" disabled={actSaving || !linkTarget || !linkType.trim()}>
						<Icon name="refresh" size={11} strokeWidth={1.6} />
						{actSaving ? 'Linking…' : 'Create'}
					</button>
					<button class="lattice-act-cancel" type="button" disabled={actSaving} onclick={resetActions}>Cancel</button>
				</div>
			</form>
		{/if}
		{#if actError}
			<p class="lattice-act-error" role="alert">{actError}</p>
		{/if}
		{#if pickedEdges.length === 0}
			<p class="lattice-empty">No relations touch this entity.</p>
		{:else}
			{#each pickedEdges as rel (rel.from + rel.to + rel.type)}
				<div class="lattice-drawer-edge">
					<span>{rel.from === picked.slug ? '→' : '←'}</span>
					<strong>{labelOf(rel.from === picked.slug ? rel.to : rel.from)}</strong>
					<em>{rel.type}</em>
					<small>{rel.sessions}×</small>
					{#if edgeArmed === edgeKey(rel)}
						<button class="lattice-edge-act lattice-edge-danger" type="button" disabled={actSaving} onclick={() => void doDeleteEdge(rel)} title="Confirm delete">Del?</button>
						<button class="lattice-edge-act" type="button" disabled={actSaving} onclick={() => (edgeArmed = '')} title="Cancel delete">No</button>
					{:else}
						<button
							class="lattice-edge-act lattice-edge-danger"
							type="button"
							disabled={actSaving}
							onclick={() => { resetActions(); edgeArmed = edgeKey(rel); }}
							title="Delete relation"
							aria-label={`Delete relation ${rel.type}`}
						>
							<Icon name="trash" size={10} strokeWidth={1.6} />
						</button>
					{/if}
				</div>
			{/each}
		{/if}
		<footer class="lattice-drawer-actions">
			<button class="lattice-edge-act" type="button" disabled={actSaving || mergeOpen} onclick={openLink} title="Create a relation from this entity">
				<Icon name="link" size={11} strokeWidth={1.6} />
				Link…
			</button>
			<button class="lattice-edge-act" type="button" disabled={actSaving || linkOpen || drawerTargets.length === 0} onclick={openMerge} title="Fold this entity into another">
				<Icon name="plus" size={11} strokeWidth={1.6} />
				Merge…
			</button>
			{#if delArmed}
				<button class="lattice-edge-act lattice-edge-danger" type="button" disabled={actSaving} onclick={() => void doDeleteEntity()} title="Confirm delete">Delete?</button>
				<button class="lattice-edge-act" type="button" disabled={actSaving} onclick={() => (delArmed = false)} title="Cancel delete">No</button>
			{:else}
				<button class="lattice-edge-act lattice-edge-danger" type="button" disabled={actSaving} onclick={() => { resetActions(); delArmed = true; }} title="Delete entity" aria-label={`Delete entity ${picked.label}`}>
					<Icon name="trash" size={11} strokeWidth={1.6} />
				</button>
			{/if}
		</footer>
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
	.lattice-edge-act { display: inline-flex; align-items: center; gap: 4px; min-height: 20px; padding: 0 5px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.lattice-edge-act:hover:not(:disabled) { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.lattice-edge-act:disabled { opacity: 0.5; cursor: default; }
	.lattice-edge-danger { color: #f36b60; }
	.lattice-edge-danger:hover:not(:disabled) { color: #f36b60; border-color: var(--red); background: rgba(213,45,36,.1); }
	.lattice-drawer-actions { display: flex; gap: 6px; padding-top: 8px; border-top: 1px solid var(--line); margin-top: 4px; }
	.lattice-act-form { display: grid; gap: 6px; padding: 7px 0; border-bottom: 1px solid var(--line); }
	.lattice-act-label { font-size: 10px; color: var(--ash); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.lattice-act-select, .lattice-act-input { min-width: 0; padding: 4px 8px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 11px; font-family: inherit; }
	.lattice-act-select:focus, .lattice-act-input:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }
	.lattice-act-input::placeholder { color: var(--ash); }
	.lattice-act-select:disabled, .lattice-act-input:disabled { opacity: 0.6; }
	.lattice-act-row { display: flex; gap: 6px; }
	.lattice-act-save { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; min-height: 24px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.lattice-act-save:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.lattice-act-save:disabled { opacity: 0.6; cursor: default; }
	.lattice-act-cancel { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; min-height: 24px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); font-size: 10px; font-weight: 700; cursor: pointer; }
	.lattice-act-cancel:hover:not(:disabled) { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.lattice-act-cancel:disabled { opacity: 0.6; cursor: default; }
	.lattice-act-error { margin: 0; padding: 4px 2px 0; color: #f36b60; font-size: 10px; }
</style>
