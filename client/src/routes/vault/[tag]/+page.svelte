<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Icon from '$lib/Icon.svelte';
	import BackButton from '$lib/BackButton.svelte';
	import DigestPanel from '$lib/DigestPanel.svelte';
	import EmptyState from '$lib/EmptyState.svelte';
	import NoticePanel from '$lib/NoticePanel.svelte';
	import SearchRecess from '$lib/SearchRecess.svelte';
	import ViewTabs from '$lib/ViewTabs.svelte';
	import Skeleton from '$lib/Skeleton.svelte';
	import LatticeTab from '$lib/lattice/LatticeTab.svelte';
	import CorrectionsTab from '$lib/vault/CorrectionsTab.svelte';
	import TagDefinitionTab from '$lib/vault/TagDefinitionTab.svelte';
	import EventCard from '$lib/vault/EventCard.svelte';
	import {
		fetchTimeline,
		fetchDigest,
		fetchDigestStatus,
		fetchMemoryStatus,
		purgeTagMemory,
		regenerateDigest,
		searchTag,
		patchEntity,
		patchGraphEvent,
		deleteGraphEvent,
		setEntityDescription,
		refreshEntityDescription,
		loadApiConfig,
		type TimelineResponse,
		type TimelineSession,
		type TimelineEvent,
		type SearchResponse,
		type DigestNote
	} from '$lib/api.svelte';
	import { dateLabel, durationLabel } from '$lib/format';

	const tag = decodeURIComponent(page.params.tag ?? '');

	type TabKey = 'definition' | 'timeline' | 'entities' | 'lattice' | 'digest' | 'corrections';

	const TABS: { key: TabKey; label: string; icon: 'tags' | 'timeline' | 'speakers' | 'enrich' | 'summary' | 'shield' }[] = [
		{ key: 'definition', label: 'Definition', icon: 'tags' },
		{ key: 'timeline', label: 'Timeline', icon: 'timeline' },
		{ key: 'entities', label: 'Entities', icon: 'speakers' },
		{ key: 'lattice', label: 'Lattice', icon: 'enrich' },
		{ key: 'digest', label: 'Digest', icon: 'summary' },
		{ key: 'corrections', label: 'Corrections', icon: 'shield' }
	];
	let data = $state<TimelineResponse | null>(null);
	let loading = $state(true);
	let tab = $state<TabKey>('digest');
	let error = $state('');
	let notFound = $state(false);
	let fetchSeq = 0;

	// Digest viewer: shares the detail page's poll shape (10s tick, 2min
	// budget after a 202) but reads only this tag; duplicated rather than
	// extracted — the detail page's copy is entangled with recording state.
	const DIGEST_POLL_MS = 10_000;
	const DIGEST_POLL_BUDGET_MS = 120_000;
	let digestText = $state<DigestNote | null>(null);
	let digestLoading = $state(false);
	let digestMissing = $state(false);
	let digestError = $state('');
	let digestGenerating = $state(false);
	let digestNote = $state('');
	let digestPoll: ReturnType<typeof globalThis.setTimeout> | null = null;
	let digestLoaded = false;
	let digestStatus = $state<{ state: 'fresh' | 'queued'; last_edit_at: string | null; debounce_sec: number } | null>(null);
	let digestStatusTimer: ReturnType<typeof globalThis.setTimeout> | null = null;

	// Phase 3.5 semantic search: query embedded server-side (same backend
	// that indexed the tag's segments), hits listed under the input; a hit
	// navigates to the recording with ?t=seconds (the detail page seeks).
	let searchQuery = $state('');
	let searchResults = $state<SearchResponse | null>(null);
	let searchLoading = $state(false);
	let searchError = $state('');
	let searchNote = $state('');
	let searchSeq = 0;

	// Phase 4 entity rename: click a row → inline recess input + brass
	// controls; PATCH is applied optimistically and rolled back with an
	// ash note on error. One editor open at a time (editingSlug).
	let editingSlug = $state<string | null>(null);
	let editLabel = $state('');
	let editType = $state('');
	let editSaving = $state(false);
	let editError = $state('');
	// Priors captured at startEdit — the optimistic pass overwrites
	// entity.label, so the rollback values must live outside the row.
	let editPriorLabel = '';
	let editPriorType = '';

	function startEdit(slug: string, label: string, type: string): void {
		if (editSaving) return;
		editingSlug = slug;
		editLabel = label;
		editType = type;
		editPriorLabel = label;
		editPriorType = type;
		editError = '';
	}

	function cancelEdit(): void {
		if (editSaving) return;
		editingSlug = null;
		editError = '';
	}

	async function saveEdit(slug: string): Promise<void> {
		const label = editLabel.trim();
		if (!label || editSaving) return;
		editSaving = true;
		editError = '';
		// Optimistic: swap the row immediately; rollback on any failure.
		if (data) {
			const row = data.entities.find((e) => e.slug === slug);
			if (row) row.label = label;
		}
		try {
			const type = editType.trim();
			await patchEntity(loadApiConfig(), tag, slug, label, type !== editPriorType ? type : undefined);
			editingSlug = null;
		} catch (caught) {
			// Rollback to the pre-edit row values (captured in startEdit).
			if (data) {
				const row = data.entities.find((e) => e.slug === slug);
				if (row) row.label = editPriorLabel;
			}
			editError = `Rename failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			editSaving = false;
		}
	}

	// Entity dossiers (2026-09-07): one expanded row at a time
	// (expandedSlug); the card shows the dossier text, mentioning
	// events (client-side filter over the already-loaded timeline) and
	// edit/refresh actions. Grouped by type (group order = group size).
	let expandedSlug = $state<string | null>(null);
	let dossierEditing = $state(false);
	let dossierText = $state('');
	let dossierSaving = $state(false);
	let dossierRefreshing = $state(false);
	let dossierError = $state('');
	let dossierNote = $state('');

	const entityGroups = $derived.by(() => {
		if (!data) return [] as { type: string; rows: TimelineResponse['entities'] }[];
		const groups = new Map<string, TimelineResponse['entities']>();
		for (const entity of data.entities) {
			const key = entity.type || 'other';
			const bucket = groups.get(key);
			if (bucket) bucket.push(entity);
			else groups.set(key, [entity]);
		}
		return [...groups.entries()]
			.sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
			.map(([type, rows]) => ({ type, rows }));
	});

	function mentioningSessions(
		slug: string
	): { recording_id: string; title: string; date: string; event: TimelineEvent }[] {
		if (!data) return [];
		const out: { recording_id: string; title: string; date: string; event: TimelineEvent }[] = [];
		for (const session of data.sessions) {
			for (const event of session.events) {
				if (event.mentions?.includes(slug)) {
					out.push({
						recording_id: session.recording_id,
						title: session.title,
						date: session.date,
						event
					});
				}
			}
		}
		return out;
	}

	function toggleDossier(slug: string): void {
		if (editingSlug) return; // rename editor takes precedence
		expandedSlug = expandedSlug === slug ? null : slug;
		dossierEditing = false;
		dossierError = '';
		dossierNote = '';
	}

	function startDossierEdit(current: string): void {
		dossierEditing = true;
		dossierText = current;
		dossierError = '';
		dossierNote = '';
	}

	async function saveDossier(slug: string): Promise<void> {
		const text = dossierText.trim();
		if (!text || dossierSaving) return;
		dossierSaving = true;
		dossierError = '';
		try {
			await setEntityDescription(loadApiConfig(), tag, slug, text);
			if (data) {
				const row = data.entities.find((e) => e.slug === slug);
				if (row) row.description = text;
			}
			dossierEditing = false;
			dossierNote = 'Dossier saved.';
		} catch (caught) {
			dossierError = `Save failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			dossierSaving = false;
		}
	}

	async function refreshDossier(slug: string): Promise<void> {
		if (dossierRefreshing) return;
		dossierRefreshing = true;
		dossierError = '';
		dossierNote = 'Refreshing…';
		try {
			await refreshEntityDescription(loadApiConfig(), tag, slug);
			dossierNote = 'Refresh queued — reload the page in a few seconds.';
	} catch (caught) {
		const status = (caught as { status?: number }).status;
		const message = caught instanceof Error ? caught.message : String(caught);
		dossierNote = '';
		// The endpoint's 409 fires for BOTH "graph not configured" and
		// (never, today) an edited refusal — the edited case is a
		// workflow failure AFTER the 202. Distinguish by the server's
		// detail text, not the bare status (roborev 2107).
		dossierError =
			status === 409 && /user-edited|edited/i.test(message)
				? 'Dossier is user-edited — save a new text or leave it as is.'
				: `Refresh failed: ${message}`;
	} finally {
			dossierRefreshing = false;
		}
	}

	/** Event ts ("hh:mm:ss" | "mm:ss" | "ss") → seconds for the
	 * detail-page deep link (?t= consumes a NUMBER). */
	function tsToSeconds(ts: string): number {
		const parts = ts.split(':').map((p) => Number(p));
		if (parts.some((p) => !Number.isFinite(p))) return 0;
		return parts.reduce((acc, p) => acc * 60 + p, 0);
	}

async function runSearch(): Promise<void> {
	const q = searchQuery.trim();
	if (!q || searchLoading) return;
	const seq = ++searchSeq;
	searchLoading = true;
	searchError = '';
	searchNote = '';
	try {
		const result = await searchTag(loadApiConfig(), tag, q);
		if (seq !== searchSeq) return;
		searchResults = result;
	} catch (caught) {
		if (seq !== searchSeq) return;
		searchResults = null;
		const err = caught as { status?: number; reason?: string; message?: string };
		if (err.status === 503) {
			searchNote = err.reason || 'Semantic search unavailable.';
		} else if (err.status === 404) {
			searchNote = 'No sessions carry this tag.';
		} else {
			searchError = `Search failed: ${err.message ?? String(caught)}`;
		}
	} finally {
		if (seq === searchSeq) searchLoading = false;
	}
}

	function clearSearch(): void {
		searchQuery = '';
		searchResults = null;
		searchError = '';
		searchNote = '';
	}

	function openHit(recordingId: string, tsStart: number): void {
		void goto(`/recordings/${recordingId}?t=${Math.max(0, Math.floor(tsStart))}`);
	}

	// Timeline sessions expand/collapse one at a time (open = recording_id).
	let openSession = $state<string | null>(null);

	async function refresh(): Promise<void> {
		const seq = ++fetchSeq;
		try {
			const result = await fetchTimeline(loadApiConfig(), tag);
			if (seq !== fetchSeq) return;
			data = result;
			error = '';
		} catch (caught) {
			if (seq !== fetchSeq) return;
			if ((caught as { status?: number }).status === 404) {
				notFound = true;
				error = '';
			} else {
				error = String(caught);
			}
		} finally {
			if (seq === fetchSeq) loading = false;
		}
	}

	function stopDigestPoll(): void {
		if (digestPoll) {
			globalThis.clearTimeout(digestPoll);
			digestPoll = null;
		}
	}

	function scheduleDigestPoll(startedAt: number): void {
		digestPoll = globalThis.setTimeout(() => void pollDigestOnce(startedAt), DIGEST_POLL_MS);
	}

async function pollDigestOnce(startedAt: number): Promise<void> {
	digestPoll = null;
	if (tab !== 'digest') return;
	try {
		const note = await fetchDigest(loadApiConfig(), tag);
		// A 200 with an OLD note is not done: the worker overwrites the
		// note in place, so during the LLM run the endpoint happily serves
		// the previous digest (observed live 2026-09-08: the first poll
		// tick 23 s before the write froze the panel on the stale body).
		// Only a generated_at NEWER than the Regenerate click settles it.
		if (Date.parse(note.generated_at) <= startedAt) {
			if (Date.now() - startedAt < DIGEST_POLL_BUDGET_MS) {
				scheduleDigestPoll(startedAt);
			} else {
				digestGenerating = false;
				digestNote = 'Still generating — check back in a minute.';
			}
			return;
		}
		digestText = note;
		digestGenerating = false;
		digestMissing = false;
		digestNote = '';
	} catch (caught) {
		const status = (caught as { status?: number }).status;
		// 404 (note not written yet) and status-less transport blips
		// (webview "Failed to fetch") are transient: keep polling while
		// the budget lasts. A real HTTP status (401/500…) is terminal —
		// retrying cannot fix it (2026-09-04: one webview blip aborted
		// the whole poll loop and stranded the panel on an error).
		if (
			(status === 404 || status === undefined) &&
			Date.now() - startedAt < DIGEST_POLL_BUDGET_MS
		) {
			scheduleDigestPoll(startedAt);
			return;
		}
		digestGenerating = false;
		if (status === 404) {
			digestNote = 'Still generating — check again in a minute.';
		} else {
			digestError = `Digest failed to load: ${caught instanceof Error ? caught.message : String(caught)}`;
		}
	}
}

async function loadDigest(): Promise<void> {
	stopDigestPoll();
	digestText = null;
	digestMissing = false;
	digestError = '';
	digestNote = '';
	digestGenerating = false;
	digestLoading = true;
	try {
		digestText = await fetchDigest(loadApiConfig(), tag);
	} catch (caught) {
		const status = (caught as { status?: number }).status;
		if (status === 404) {
			digestMissing = true;
		} else {
			digestError = `Digest failed to load: ${caught instanceof Error ? caught.message : String(caught)}`;
		}
	} finally {
		digestLoading = false;
	}
}

async function regenerateDigestNow(): Promise<void> {
	if (digestGenerating) return;
	stopDigestPoll();
	digestText = null;
	digestMissing = false;
	digestNote = '';
	digestError = '';
	digestGenerating = true;
	try {
		await regenerateDigest(loadApiConfig(), tag);
		scheduleDigestPoll(Date.now());
	} catch (caught) {
		digestGenerating = false;
		digestError = `Digest request failed: ${caught instanceof Error ? caught.message : String(caught)}`;
	}
}

function switchTab(next: TabKey): void {
	tab = next;
	if (next === 'digest' && !digestLoaded) {
		digestLoaded = true;
		void loadDigest();
	}
	if (next === 'digest') {
		void refreshDigestStatus();
	} else {
		clearDigestStatusPoll();
	}
}

	/** Definition tab's "Open sessions →" link: jump to the timeline. */
	function gotoTimeline(): void {
		switchTab('timeline');
	}


	function toggleSession(id: string): void {
		openSession = openSession === id ? null : id;
	}

	// Digest renewal status poll: 10s tick while on the digest tab — the lamp shows the maintenance workflow's state.
	async function refreshDigestStatus(): Promise<void> {
		try {
			digestStatus = await fetchDigestStatus(loadApiConfig(), tag);
		} catch {
			digestStatus = null;
		}
		if (tab === 'digest') {
			if (digestStatusTimer) globalThis.clearTimeout(digestStatusTimer);
			digestStatusTimer = globalThis.setTimeout(() => void refreshDigestStatus(), 10_000);
		}
	}

	function clearDigestStatusPoll(): void {
		if (digestStatusTimer) {
			globalThis.clearTimeout(digestStatusTimer);
			digestStatusTimer = null;
		}
	}

	// Phase A event edit handlers: a single 202 then a settle refetch; the Digest lamp flips on its own poll.
	let eventSaving = $state(false);

	async function applyEventEdit(
		eventKey: string,
		fields: { ts?: string; kind?: string; summary?: string; mentions?: string[] },
		feedback: string
	): Promise<boolean> {
		if (eventSaving) return false;
		eventSaving = true;
		try {
			await patchGraphEvent(loadApiConfig(), tag, eventKey, { ...fields, feedback_text: feedback || undefined });
			globalThis.setTimeout(() => void refresh(), 2_000);
			return true;
		} catch {
			return false;
		} finally {
			eventSaving = false;
		}
	}

	async function removeEvent(eventKey: string): Promise<boolean> {
		if (eventSaving) return false;
		eventSaving = true;
		try {
			await deleteGraphEvent(loadApiConfig(), tag, eventKey);
			globalThis.setTimeout(() => void refresh(), 2_000);
			return true;
		} catch {
			return false;
		} finally {
			eventSaving = false;
		}
	}

// Tag-memory admin (2026-09-04): ellipsis menu on the header — Purge
// memory… / Rebuild memory. Destructive, so each goes through an armed
// confirm (the same two-step shape the recording Delete uses). The
// 202 hands a workflow id; a 5s poll drives the brass status line.
let memoryMenuOpen = $state(false);
let memoryConfirm = $state<'purge' | 'rebuild' | null>(null);
let memoryBusy = $state(false);
let memoryError = $state('');
let memoryStatusLine = $state('');
let memoryPoll: ReturnType<typeof globalThis.setTimeout> | null = null;
let memoryTag = '';

function closeMemoryMenu(): void {
	memoryMenuOpen = false;
	memoryConfirm = null;
}

async function startMemoryAction(rebuild: boolean): Promise<void> {
	if (memoryBusy) return;
	memoryBusy = true;
	memoryError = '';
	memoryConfirm = null;
	closeMemoryMenu();
	try {
		const { workflow_id } = await purgeTagMemory(loadApiConfig(), tag, rebuild);
		memoryTag = tag;
		memoryStatusLine = rebuild ? 'Rebuilding memory…' : 'Purging memory…';
		scheduleMemoryPoll(workflow_id, rebuild);
	} catch (caught) {
		memoryError = String(caught instanceof Error ? caught.message : caught);
	} finally {
		memoryBusy = false;
	}
}

function scheduleMemoryPoll(workflowId: string, rebuild: boolean): void {
	if (memoryPoll) globalThis.clearTimeout(memoryPoll);
	memoryPoll = globalThis.setTimeout(async () => {
		try {
			const status = await fetchMemoryStatus(loadApiConfig(), tag, workflowId);
			if (status.state === 'running') {
				const p = status.progress;
				memoryStatusLine = rebuild && p
					? `Rebuilding memory — ${Math.min(p.done, p.total)} of ${p.total} sessions`
					: rebuild ? 'Rebuilding memory…' : 'Purging memory…';
				scheduleMemoryPoll(workflowId, rebuild);
				return;
			}
			if (status.state === 'failed') {
				memoryError = status.detail ?? 'memory workflow failed';
				memoryStatusLine = '';
			} else if (status.state === 'unknown') {
				memoryStatusLine = '';
			} else {
				memoryStatusLine = '';
				void refresh();
				void refreshDigestStatus();
			}
		} catch {
			memoryError = 'memory status poll failed';
			memoryStatusLine = '';
		}
	}, 5_000);
}


	function sessionDate(session: TimelineSession): string {
		return dateLabel(session.date);
	}

	/** Event kind → left-rail accent. Explicit entries cover the default
	 * enrich vocabulary (milestone/change/decision/meeting); kinds invented
	 * by custom profiles get a deterministic color from the same ramp.
	 * Cyan stays out — it is reserved for verified state. */
	const KIND_ACCENTS: Record<string, string> = {
		milestone: 'var(--brass)',
		decision: 'var(--red)',
		change: '#e9dfcf',
		meeting: '#9e9183'
	};
	const KIND_RAMP = ['var(--brass)', 'var(--red)', '#e9dfcf', '#9e9183'];

	function kindAccent(kind: string): string {
		const key = kind.trim().toLowerCase();
		const explicit = KIND_ACCENTS[key];
		if (explicit !== undefined) return explicit;
		let hash = 0;
		for (const ch of key) hash = (hash * 31 + ch.codePointAt(0)!) >>> 0;
		return KIND_RAMP[hash % KIND_RAMP.length] ?? 'var(--brass)';
	}

	onMount(() => {
		refresh();
		// Digest is the DEFAULT entry (2026-09-07): switchTab runs the
		// lazy-load, but nothing calls it on mount — fire the same init
		// here so the panel arrives without a tab round-trip.
		digestLoaded = true;
		void loadDigest();
		void refreshDigestStatus();
		return stopDigestPoll;
	});
</script>

<svelte:head><title>{tag} · Vault · Transcriptor Maximus</title></svelte:head>

<svelte:window
	onkeydown={(event) => { if (event.key === 'Escape' && memoryMenuOpen) { event.stopPropagation(); closeMemoryMenu(); } }}
	onpointerdown={(event) => { if (memoryMenuOpen && !(event.target instanceof Element && event.target.closest('.memory-menu-wrap'))) closeMemoryMenu(); }}
/>

<section class="page tag-page" class:tag-page--lattice={tab === 'lattice' && !loading && !!data}>
	<header class="tag-header">
		<BackButton href="/vault" label="Back to vault" />
		{#if loading}
			<Skeleton variant="heading" />
		{:else}
			<h1 class="page-title tag-title">{tag}</h1>
		{/if}
		<div class="memory-menu-wrap">
			<button class="memory-toggle" type="button" aria-label="Tag memory actions" aria-haspopup="menu" aria-expanded={memoryMenuOpen} onclick={() => (memoryMenuOpen ? closeMemoryMenu() : (memoryMenuOpen = true))}><Icon name="dots" size={15} /></button>
			{#if memoryMenuOpen}
				<div class="memory-menu" role="menu" aria-label="Tag memory actions">
					{#if memoryConfirm === 'purge'}
						<div class="memory-confirm">
							<span>Wipe graph, edits, digest, index and timelines?</span>
							<small>Recordings stay. Rebuild restores the memory. No undo.</small>
							<div class="memory-confirm-actions">
								<button class="memory-confirm-yes" type="button" disabled={memoryBusy} onclick={() => void startMemoryAction(false)}>Confirm wipe</button>
								<button type="button" onclick={() => (memoryConfirm = null)}>Cancel</button>
							</div>
						</div>
					{:else if memoryConfirm === 'rebuild'}
						<div class="memory-confirm">
							<span>Wipe and rebuild everything — summaries and graph?</span>
							<small>Re-summarizes and re-extracts every session, oldest first. Takes a while.</small>
							<div class="memory-confirm-actions">
								<button class="memory-confirm-yes" type="button" disabled={memoryBusy} onclick={() => void startMemoryAction(true)}>Confirm rebuild</button>
								<button type="button" onclick={() => (memoryConfirm = null)}>Cancel</button>
							</div>
						</div>
					{:else}
						<button type="button" role="menuitem" class="memory-item memory-danger" onclick={() => (memoryConfirm = 'purge')}>Purge memory…</button>
						<button type="button" role="menuitem" class="memory-item" onclick={() => (memoryConfirm = 'rebuild')}>Rebuild memory fully</button>
					{/if}
				</div>
			{/if}
		</div>
	</header>

	{#if memoryStatusLine}
		<p class="memory-status" role="status">{memoryStatusLine}</p>
	{/if}
	{#if memoryError}
		<p class="memory-error" role="alert">{memoryError}</p>
	{/if}

	{#if error}
		<div class="tag-error" role="alert"><strong>Timeline unavailable</strong><span>{error}</span></div>
	{/if}

	{#if loading}
		<Skeleton variant="panel-tag" />
	{:else if notFound}
		<!-- Empty tag (registry-only): the timeline 404s, but the tag page
		     must stay reachable — Definition is the only meaningful view,
		     and the tab row keeps it discoverable for non-empty tags. -->
		<ViewTabs tabs={TABS} active={tab} ariaLabel="Tag views" onchange={(key) => switchTab(key as TabKey)} />
		{#if tab === 'definition'}
			<TagDefinitionTab {tag} ontimeline={gotoTimeline} ondeleted={() => void goto('/vault')} />
		{:else}
			<NoticePanel title="No sessions carry this tag" hint="The tag may have been removed from every recording, or the address is wrong." backHref="/vault" backLabel="Back to vault" />
		{/if}
	{:else if data}
		<ViewTabs tabs={TABS} active={tab} ariaLabel="Tag views" onchange={(key) => switchTab(key as TabKey)} />

		{#if tab !== 'lattice' && tab !== 'definition'}
			<SearchRecess
				ariaLabel={`Semantic search · ${tag}`}
				placeholder="Search this tag's sessions…"
				bind:query={searchQuery}
				loading={searchLoading}
				error={searchError}
				note={searchNote}
				results={searchResults}
				onsearch={runSearch}
				onclear={clearSearch}
				onopenhit={openHit}
			/>
		{/if}

		{#if tab === 'definition'}
			<TagDefinitionTab {tag} ontimeline={gotoTimeline} ondeleted={() => void goto('/vault')} />
		{:else if tab === 'timeline'}
			<div class="session-list">
				{#each data.sessions as session (session.recording_id)}
					<div class="session-card">
						<button class="list-row session-heading" type="button" onclick={() => toggleSession(session.recording_id)} aria-expanded={openSession === session.recording_id}>
							<span class="session-mark" class:open={openSession === session.recording_id} aria-hidden="true"></span>
							<span class="session-name">
								<strong>{session.title || 'Untitled capture'}</strong>
								<small>{sessionDate(session)}{session.type ? ` · ${session.type}` : ''}{session.duration_sec !== null ? ` · ${durationLabel(session.duration_sec)}` : ''} · {session.events.length} event{session.events.length === 1 ? '' : 's'}</small>
							</span>
							<span class="session-chevron"><Icon name="collapse" size={14} /></span>
						</button>
						{#if openSession === session.recording_id}
							<div class="event-list">
								{#each session.events as event, index (index + event.ts + event.summary)}
									<EventCard
										{event}
										eventKey={event.event_key}
										accent={kindAccent(event.kind)}
										saving={eventSaving}
										onedit={(fields, feedback) => applyEventEdit(event.event_key, fields, feedback)}
										ondelete={() => removeEvent(event.event_key)}
									/>
								{:else}
									<p class="event-empty">No events extracted for this session.</p>
								{/each}
							</div>
						{/if}
					</div>
				{:else}
					<EmptyState icon="timeline" title="No sessions carry this tag" hint="Tag recordings in the Library to build the timeline." />
				{/each}
			</div>
		{:else if tab === 'entities'}
			<div class="entity-list">
				{#each entityGroups as group (group.type)}
					<div class="entity-group">
						<h4 class="entity-group-title">
							{group.type}
							<small>{group.rows.length}</small>
						</h4>
						{#each group.rows as entity (entity.slug)}
							{#if editingSlug === entity.slug}
								<div class="entity-edit">
									<form
										class="entity-edit-form"
										onsubmit={(event) => {
											event.preventDefault();
											void saveEdit(entity.slug);
										}}
									>
										<input
											class="entity-edit-input"
											type="text"
											aria-label={`Rename ${entity.label}`}
											bind:value={editLabel}
											disabled={editSaving}
											maxlength="200"
										/>
										<input
											class="entity-edit-type"
											type="text"
											aria-label="Entity type"
											bind:value={editType}
											disabled={editSaving}
											maxlength="100"
											placeholder="type"
										/>
										<button class="entity-edit-save" type="submit" disabled={editSaving || !editLabel.trim()}>
											<Icon name="refresh" size={11} strokeWidth={1.6} />
											{editSaving ? 'Saving…' : 'Save'}
										</button>
										<button class="entity-edit-cancel" type="button" disabled={editSaving} onclick={cancelEdit}>
											<Icon name="close" size={11} strokeWidth={1.6} />
											Cancel
										</button>
									</form>
									{#if editError}
										<p class="entity-edit-error" role="alert">{editError}</p>
									{/if}
								</div>
							{:else}
								<div class="entity-block">
									<button
										class="list-row entity-row"
										type="button"
										onclick={() => toggleDossier(entity.slug)}
										aria-expanded={expandedSlug === entity.slug}
										title={expandedSlug === entity.slug ? `Collapse ${entity.label}` : `Expand dossier for ${entity.label}`}
									>
										<span class="entity-name">
											<strong>{entity.label}</strong>
											<small>{entity.type}</small>
										</span>
										<span class="entity-meta">
											<small>{entity.sessions} session{entity.sessions === 1 ? '' : 's'}</small>
											<small>{dateLabel(entity.last_seen)}</small>
											<Icon name={expandedSlug === entity.slug ? 'close' : 'enrich'} size={11} strokeWidth={1.6} />
										</span>
									</button>
									<button
										class="entity-rename-btn"
										type="button"
										onclick={() => startEdit(entity.slug, entity.label, entity.type)}
										title={`Rename ${entity.label}`}
									>
										<Icon name="pencil" size={11} strokeWidth={1.6} />
									</button>
									{#if expandedSlug === entity.slug}
										<div class="entity-dossier">
											{#if dossierEditing}
												<form
													class="dossier-form"
													onsubmit={(event) => {
														event.preventDefault();
														void saveDossier(entity.slug);
													}}
												>
													<textarea
														class="dossier-textarea"
														aria-label={`Dossier for ${entity.label}`}
														bind:value={dossierText}
														disabled={dossierSaving}
														maxlength="2000"
														rows="4"
													></textarea>
													<div class="dossier-actions">
														<button class="dossier-btn" type="submit" disabled={dossierSaving || !dossierText.trim()}>
															{dossierSaving ? 'Saving…' : 'Save dossier'}
														</button>
														<button
															class="dossier-btn"
															type="button"
															disabled={dossierSaving}
															onclick={() => (dossierEditing = false)}
														>
															Cancel
														</button>
													</div>
												</form>
											{:else}
												{#if entity.description}
													<p class="dossier-text">{entity.description}</p>
												{:else}
													<p class="dossier-empty">No dossier yet — run enrich on a session mentioning this entity, or refresh.</p>
												{/if}
												<div class="dossier-actions">
													<button
														class="dossier-btn"
														type="button"
														disabled={dossierRefreshing}
														onclick={() => void refreshDossier(entity.slug)}
													>
														<Icon name="refresh" size={11} strokeWidth={1.6} />
														{dossierRefreshing ? 'Refreshing…' : 'Refresh'}
													</button>
													<button
														class="dossier-btn"
														type="button"
														onclick={() => startDossierEdit(entity.description ?? '')}
													>
														<Icon name="pencil" size={11} strokeWidth={1.6} />
														Edit
													</button>
												</div>
											{/if}
											{#if dossierError}
												<p class="entity-edit-error" role="alert">{dossierError}</p>
											{/if}
											{#if dossierNote}
												<p class="dossier-note">{dossierNote}</p>
											{/if}
										{#if mentioningSessions(entity.slug).length > 0}
											<div class="dossier-mentions">
												<h5>Mentioned in</h5>
												{#each mentioningSessions(entity.slug) as m, i (i)}
													<button
														class="dossier-mention"
														type="button"
														onclick={() => goto(`/recordings/${encodeURIComponent(m.recording_id)}?t=${tsToSeconds(m.event.ts)}`)}
													>
														<small>{m.title} · {dateLabel(m.date)}</small>
														<span>{m.event.summary}</span>
													</button>
												{/each}
											</div>
										{/if}
										</div>
									{/if}
								</div>
							{/if}
						{/each}
					</div>
				{:else}
					<EmptyState icon="speakers" title="No entities extracted yet" hint="Run the pipeline on tagged recordings to populate the roster." />
				{/each}
			</div>
		{:else if tab === 'lattice'}
			<LatticeTab
				{tag}
				entitiesSeed={data.entities.map((e) => ({ slug: e.slug, label: e.label, type: e.type, sessions: e.sessions, description: e.description ?? '' }))}
				relationsSeed={[]}
			/>
		{:else if tab === 'corrections'}
			<CorrectionsTab
				{tag}
				entities={data.entities.map((e) => ({ slug: e.slug, label: e.label, type: e.type }))}
				events={data.sessions.flatMap((s) => s.events)}
				onchanged={() => { void refresh(); void refreshDigestStatus(); }}
			/>
		{:else}
			<DigestPanel tag={tag} loading={digestLoading} generating={digestGenerating} error={digestError} note={digestNote} missing={digestMissing} text={digestText?.body ?? null} generatedAt={digestText?.generated_at} recordings={digestText?.recordings ?? []} queued={digestStatus?.state === 'queued'} onregen={() => void regenerateDigestNow()} />
		{/if}
	{/if}
</section>
<style>
	.tag-page { display: flex; flex-direction: column; gap: 12px; min-height: 100%; }
	/* Lattice tab: the page becomes the canvas frame — height chains
	   down from the scroll viewport so the graph fills the workspace
	   instead of a fixed 46vh strip. Other tabs keep normal flow.
	   overflow:hidden — the lattice view is a fixed frame by design:
	   nothing inside may grow the page (a WebView flex-quirk letting
	   the canvas out of its cell would otherwise scroll the tab strip
	   out of sight — reported on all engines 2026-09-05). */
	.tag-page--lattice { height: 100%; overflow: hidden; }
	.tag-header { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; }
	.memory-menu-wrap { position: relative; display: flex; min-width: 0; }
	.memory-toggle { width: 26px; height: 26px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #968d83; cursor: pointer; line-height: 0; }
	.memory-toggle:hover, .memory-toggle[aria-expanded='true'] { color: var(--bone); border-color: rgba(215, 167, 71, 0.4); background: rgba(215, 167, 71, 0.08); }
	.memory-menu { position: absolute; top: calc(100% + 4px); right: 0; z-index: 40; min-width: 220px; display: grid; padding: 4px; border: 1px solid var(--line); border-radius: 3px; background: var(--iron-raised); box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55); }
	.memory-item { width: 100%; min-height: 26px; display: flex; align-items: center; padding: 0 8px; border: 0; border-radius: 2px; background: transparent; color: #c6baaa; font-size: 11px; text-align: left; cursor: pointer; }
	.memory-item:hover { background: rgba(215, 167, 71, 0.1); color: var(--bone); }
	.memory-danger { color: #f36b60; }
	.memory-danger:hover { background: rgba(213, 45, 36, 0.12); color: #f36b60; }
	.memory-confirm { display: grid; gap: 4px; padding: 6px 8px; }
	.memory-confirm span { font-size: 11px; color: var(--bone); }
	.memory-confirm small { font-size: 9px; color: var(--ash); }
	.memory-confirm-actions { display: flex; gap: 6px; margin-top: 2px; }
	.memory-confirm-actions button { min-height: 22px; padding: 0 8px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); font-size: 10px; font-weight: 700; cursor: pointer; }
	.memory-confirm-actions button:hover { color: var(--bone); border-color: rgba(215, 167, 71, 0.4); }
	.memory-confirm-yes { border-color: rgba(213, 45, 36, 0.5) !important; color: #f36b60 !important; }
	.memory-confirm-yes:hover { background: rgba(213, 45, 36, 0.16) !important; }
	.memory-status { margin: 0; padding: 6px 10px; border-left: 2px solid var(--brass); background: rgba(215, 167, 71, 0.08); color: var(--brass); font-size: 11px; font-weight: 700; }
	.memory-error { margin: 0; padding: 6px 10px; border-left: 2px solid var(--red); background: rgba(213, 45, 36, 0.08); color: #f36b60; font-size: 11px; }
	.tag-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 24px; }
	.tag-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.tag-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.tag-error span { color: #c6baaa; }
	.session-list { display: grid; }
	.session-card { transition: background 120ms ease; }
	.session-card:not(:last-child) { border-bottom: 1px solid var(--line); }
	.session-heading { grid-template-columns: auto 1fr auto; }
	.session-mark { width: 7px; height: 7px; border-radius: 50%; background: var(--brass); box-shadow: 0 0 0 3px rgba(215,167,71,.12); }
	.session-name { min-width: 0; }
	.session-name strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; color: #ded3c4; }
	.session-name small { display: block; margin-top: 4px; font-size: 10px; color: #8b8278; }
	.session-chevron { display: grid; place-items: center; color: #6f685f; line-height: 0; transition: transform 120ms ease; }
	.session-heading[aria-expanded='true'] .session-chevron { transform: rotate(180deg); color: var(--brass); }
	/* Event cards: full-width summary text instead of the old three-column
	   row. The colored left rail carries the event kind; consecutive rails
	   form the timeline spine. Cyan is deliberately absent — it belongs to
	   verified state, not taxonomy. */
	.event-list { display: grid; padding: 0 11px 8px 27px; }
	.event-empty { margin: 0; padding: 8px 0; border-top: 1px solid var(--line); color: var(--ash); font-size: 11px; }
	.entity-row { grid-template-columns: 1fr auto; }
	.entity-row:not(:last-child) { border-bottom: 1px solid var(--line); }
	.entity-name { min-width: 0; }
	.entity-name strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: #ded3c4; }
	.entity-name small { display: block; margin-top: 3px; font-size: 9px; color: #8b8278; text-transform: capitalize; }
	.entity-meta { display: grid; justify-items: end; gap: 2px; }
	.entity-meta small { font-size: 10px; color: #8b8278; font-variant-numeric: tabular-nums; white-space: nowrap; }
	.entity-row:hover .entity-name strong { color: var(--bone); }
	.entity-group { margin-bottom: 10px; }
	.entity-group-title {
		display: flex; align-items: baseline; gap: 6px; margin: 10px 0 4px;
		font-size: 10px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
		color: var(--brass);
	}
	.entity-group-title small { color: var(--ash); font-size: 9px; letter-spacing: normal; }
	.entity-block { position: relative; border-bottom: 1px solid var(--line); }
	.entity-block .entity-row { width: 100%; border-bottom: none; }
	.entity-rename-btn {
		position: absolute; top: 50%; right: 8px; transform: translateY(-50%);
		display: grid; place-items: center; width: 22px; height: 22px;
		border: 1px solid transparent; border-radius: 3px; background: transparent;
		color: var(--ash); cursor: pointer; opacity: 0; transition: opacity .12s ease, color .12s ease;
	}
	.entity-block:hover .entity-rename-btn { opacity: 1; }
	.entity-rename-btn:hover { color: var(--brass); border-color: var(--brass); }
	.entity-row[aria-expanded='true'] { padding-right: 40px; }
	.entity-dossier { padding: 6px 4px 10px; background: rgba(0,0,0,.25); }
	.dossier-text { margin: 0 0 8px; font-size: 11.5px; line-height: 1.5; color: #cfc4b4; }
	.dossier-empty { margin: 0 0 8px; font-size: 11px; font-style: italic; color: var(--ash); }
	.dossier-actions { display: flex; gap: 6px; }
	.dossier-btn {
		display: inline-flex; align-items: center; gap: 4px; padding: 3px 9px;
		border: 1px solid var(--brass); border-radius: 3px;
		background: rgba(215,167,71,.12); color: var(--brass);
		font-size: 10px; cursor: pointer;
	}
	.dossier-btn:disabled { opacity: .5; cursor: default; }
	.dossier-form { display: grid; gap: 6px; }
	.dossier-textarea {
		width: 100%; padding: 7px 9px; border: 1px solid var(--line); border-radius: 2px;
		background: rgba(0,0,0,.3); color: var(--bone); font-size: 12px; line-height: 1.5;
		resize: vertical; min-height: 64px;
	}
	.dossier-note { margin: 6px 0 0; font-size: 10px; color: var(--ash); }
	.dossier-mentions { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 8px; }
	.dossier-mentions h5 {
		margin: 0 0 6px; font-size: 9px; font-weight: 600; letter-spacing: .12em;
		text-transform: uppercase; color: var(--ash);
	}
	.dossier-mention {
		display: grid; gap: 2px; width: 100%; padding: 5px 6px; margin-bottom: 4px;
		border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.2);
		text-align: left; cursor: pointer;
	}
	.dossier-mention:hover { border-color: var(--brass); }
	.dossier-mention small { font-size: 9px; color: var(--ash); }
	.dossier-mention span { font-size: 11px; color: #ded3c4; line-height: 1.4; }
	.entity-edit { display: grid; gap: 5px; padding: 8px 4px; border-bottom: 1px solid var(--line); }
	.entity-edit-form { display: grid; grid-template-columns: 1fr 92px auto auto; align-items: stretch; gap: 6px; }
	.entity-edit-input, .entity-edit-type { min-width: 0; height: 30px; padding: 0 9px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 12px; }
	.entity-edit-input::placeholder, .entity-edit-type::placeholder { color: var(--ash); }
	.entity-edit-input:focus, .entity-edit-type:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }
	.entity-edit-input:disabled, .entity-edit-type:disabled { opacity: 0.6; }
	.entity-edit-save { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 11px; font-weight: 700; cursor: pointer; }
	.entity-edit-save:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.entity-edit-save:disabled { opacity: 0.6; cursor: default; }
	.entity-edit-cancel { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); font-size: 11px; font-weight: 700; cursor: pointer; }
	.entity-edit-cancel:hover:not(:disabled) { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.entity-edit-cancel:disabled { opacity: 0.6; cursor: default; }
	.entity-edit-error { margin: 0; padding: 0 2px; color: #f36b60; font-size: 10px; }
	@media (prefers-reduced-motion: reduce) { .session-chevron { transition: none; } }
</style>
