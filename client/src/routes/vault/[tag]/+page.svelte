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
	import EventCard from '$lib/vault/EventCard.svelte';
	import {
		fetchTimeline,
		fetchDigest,
		fetchDigestStatus,
		regenerateDigest,
		searchTag,
		patchEntity,
		patchGraphEvent,
		deleteGraphEvent,
		loadApiConfig,
		type TimelineResponse,
		type TimelineSession,
		type TimelineEvent,
		type SearchResponse
	} from '$lib/api.svelte';
	import { dateLabel, durationLabel } from '$lib/format';

	const tag = decodeURIComponent(page.params.tag ?? '');

	type TabKey = 'timeline' | 'entities' | 'lattice' | 'digest' | 'corrections';
	const TABS: { key: TabKey; label: string; icon: 'timeline' | 'speakers' | 'summary' | 'shield' }[] = [
		{ key: 'timeline', label: 'Timeline', icon: 'timeline' },
		{ key: 'entities', label: 'Entities', icon: 'speakers' },
		{ key: 'lattice', label: 'Lattice', icon: 'speakers' },
		{ key: 'digest', label: 'Digest', icon: 'summary' },
		{ key: 'corrections', label: 'Corrections', icon: 'shield' }
	];
	let tab = $state<TabKey>('timeline');
	let data = $state<TimelineResponse | null>(null);
	let loading = $state(true);
	let error = $state('');
	let notFound = $state(false);
	let fetchSeq = 0;

	// Digest viewer: shares the detail page's poll shape (10s tick, 2min
	// budget after a 202) but reads only this tag; duplicated rather than
	// extracted — the detail page's copy is entangled with recording state.
	const DIGEST_POLL_MS = 10_000;
	const DIGEST_POLL_BUDGET_MS = 120_000;
	let digestText = $state<string | null>(null);
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
			digestText = await fetchDigest(loadApiConfig(), tag);
			digestGenerating = false;
			digestMissing = false;
			digestNote = '';
		} catch (caught) {
			const status = (caught as { status?: number }).status;
			if (status === 404) {
				if (Date.now() - startedAt >= DIGEST_POLL_BUDGET_MS) {
					digestGenerating = false;
					digestNote = 'Still generating — check again in a minute.';
					return;
				}
				scheduleDigestPoll(startedAt);
				return;
			}
			digestGenerating = false;
			digestError = `Digest failed to load: ${caught instanceof Error ? caught.message : String(caught)}`;
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
		return stopDigestPoll;
	});
</script>

<svelte:head><title>{tag} · Vault · Transcriptor Maximus</title></svelte:head>

<section class="page tag-page">
	<header class="tag-header">
		<BackButton href="/vault" label="Back to vault" />
		{#if loading}
			<Skeleton variant="heading" />
		{:else}
			<h1 class="page-title tag-title">{tag}</h1>
		{/if}
	</header>

	{#if error}
		<div class="tag-error" role="alert"><strong>Timeline unavailable</strong><span>{error}</span></div>
	{/if}

	{#if loading}
		<Skeleton variant="panel-tag" />
	{:else if notFound}
		<NoticePanel title="No sessions carry this tag" hint="The tag may have been removed from every recording, or the address is wrong." backHref="/vault" backLabel="Back to vault" />
	{:else if data}
		<ViewTabs tabs={TABS} active={tab} ariaLabel="Tag views" onchange={(key) => switchTab(key as TabKey)} />

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

		{#if tab === 'timeline'}
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
				{#each data.entities as entity (entity.slug)}
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
						<button class="list-row entity-row" type="button" onclick={() => startEdit(entity.slug, entity.label, entity.type)} title={`Rename ${entity.label}`}>
							<span class="entity-name">
								<strong>{entity.label}</strong>
								<small>{entity.type}</small>
							</span>
							<span class="entity-meta">
								<small>{entity.sessions} session{entity.sessions === 1 ? '' : 's'}</small>
								<small>{dateLabel(entity.last_seen)}</small>
							</span>
						</button>
					{/if}
				{:else}
					<EmptyState icon="speakers" title="No entities extracted yet" hint="Run the pipeline on tagged recordings to populate the roster." />
				{/each}
			</div>
		{:else if tab === 'lattice'}
			<LatticeTab
				{tag}
				entitiesSeed={data.entities.map((e) => ({ slug: e.slug, label: e.label, type: e.type, sessions: e.sessions }))}
				relationsSeed={[]}
			/>
		{:else if tab === 'corrections'}
			<CorrectionsTab {tag} onchanged={() => { void refresh(); void refreshDigestStatus(); }} />
		{:else}
			<DigestPanel tag={tag} loading={digestLoading} generating={digestGenerating} error={digestError} note={digestNote} missing={digestMissing} text={digestText} queued={digestStatus?.state === 'queued'} onregen={() => void regenerateDigestNow()} />
		{/if}
	{/if}
</section>
<style>
	.tag-page { display: flex; flex-direction: column; gap: 12px; min-height: 100%; }
	.tag-header { display: grid; grid-template-columns: auto 1fr; align-items: center; gap: 10px; }
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
