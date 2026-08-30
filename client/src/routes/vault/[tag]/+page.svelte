<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Icon from '$lib/Icon.svelte';
	import Markdown from '$lib/Markdown.svelte';
	import {
		fetchTimeline,
		fetchDigest,
		regenerateDigest,
		searchTag,
		patchEntity,
		loadApiConfig,
		type TimelineResponse,
		type TimelineSession,
		type TimelineEvent,
		type SearchResponse
	} from '$lib/api.svelte';
	import { dateLabel, durationLabel } from '$lib/format';

	const tag = decodeURIComponent(page.params.tag ?? '');

	type TabKey = 'timeline' | 'entities' | 'digest';
	const TABS: { key: TabKey; label: string; icon: 'timeline' | 'speakers' | 'summary' }[] = [
		{ key: 'timeline', label: 'Timeline', icon: 'timeline' },
		{ key: 'entities', label: 'Entities', icon: 'speakers' },
		{ key: 'digest', label: 'Digest', icon: 'summary' }
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
let searchOpen = $state(false);

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
		searchOpen = true;
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
		searchOpen = false;
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
	}

	function toggleSession(id: string): void {
		openSession = openSession === id ? null : id;
	}

	function sessionDate(session: TimelineSession): string {
		return dateLabel(session.date);
	}

	/** Seconds → "mm:ss" / "h:mm:ss" (hit rows + titles; the seek target
	 * stays in raw seconds). */
	function tsLabel(seconds: number): string {
		const total = Math.max(0, Math.floor(seconds));
		const h = Math.floor(total / 3600);
		const m = Math.floor((total % 3600) / 60);
		const s = total % 60;
		const mm = String(m).padStart(h > 0 ? 2 : 1, '0');
		return `${h > 0 ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`;
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
		<button class="back-button" type="button" onclick={() => goto('/vault')} aria-label="Back to vault" title="Back to vault">
			<Icon name="back" size={16} />
		</button>
		{#if loading}
			<span class="skeleton-bar skeleton-heading" aria-hidden="true"></span>
		{:else}
			<h1 class="page-title tag-title">{tag}</h1>
		{/if}
	</header>

	{#if error}
		<div class="tag-error" role="alert"><strong>Timeline unavailable</strong><span>{error}</span></div>
	{/if}

	{#if loading}
		<div class="skeleton-panel" aria-hidden="true">
			<span class="skeleton-bar skeleton-strip"></span>
			<span class="skeleton-bar skeleton-body"></span>
		</div>
	{:else if notFound}
		<div class="notice-panel">
			<strong>No sessions carry this tag</strong>
			<small>The tag may have been removed from every recording, or the address is wrong.</small>
			<a class="back-link" href="/vault"><Icon name="back" size={13} /> Back to vault</a>
		</div>
	{:else if data}
		<div class="tag-tabs" role="tablist" aria-label="Tag views">
			{#each TABS as entry (entry.key)}
				<button
					class="tag-tab"
					type="button"
					role="tab"
					aria-selected={tab === entry.key}
					class:active={tab === entry.key}
					onclick={() => switchTab(entry.key)}
				>
					<Icon name={entry.icon} size={12} />
					{entry.label}
				</button>
			{/each}
			</div>

		<section class="search-recess" aria-label={`Semantic search · ${tag}`}>
			<form
				class="search-form"
				onsubmit={(event) => {
					event.preventDefault();
					void runSearch();
				}}
			>
				<input
					class="search-input"
					type="search"
					placeholder="Search this tag's sessions…"
					aria-label="Search query"
					bind:value={searchQuery}
					disabled={searchLoading}
				/>
				<button class="search-go" type="submit" disabled={searchLoading || !searchQuery.trim()}>
					<Icon name="search" size={12} strokeWidth={1.6} />
					{searchLoading ? 'Searching…' : 'Search'}
				</button>
				{#if searchResults !== null || searchNote || searchError}
					<button class="search-clear" type="button" onclick={clearSearch} aria-label="Clear search results">
						<Icon name="close" size={12} strokeWidth={1.6} />
					</button>
				{/if}
			</form>
			{#if searchError}
				<p class="search-error" role="alert">{searchError}</p>
			{:else if searchNote}
				<p class="search-note">{searchNote}</p>
			{:else if searchResults !== null}
				{#if searchResults.hits.length === 0}
					<p class="search-note">No matching segments.</p>
				{:else}
					<div class="hit-list">
						{#each searchResults.hits as hit (hit.recording_id + hit.ts_start)}
							<button class="hit-row" type="button" title={`Open ${hit.session_title || 'session'} at ${tsLabel(hit.ts_start)}`} onclick={() => openHit(hit.recording_id, hit.ts_start)}>
								<span class="hit-ts">{tsLabel(hit.ts_start)}</span>
								<span class="hit-main">
									<strong>{hit.session_title || 'Untitled capture'}</strong>
									<small>
										{#if hit.speaker}{hit.speaker} · {/if}{hit.snippet}
									</small>
								</span>
							</button>
						{/each}
					</div>
				{/if}
			{/if}
		</section>

		{#if tab === 'timeline'}
			<div class="session-list">
				{#each data.sessions as session (session.recording_id)}
					<div class="session-card">
						<button class="session-heading" type="button" onclick={() => toggleSession(session.recording_id)} aria-expanded={openSession === session.recording_id}>
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
									<div class="event-card" style="--event-accent: {kindAccent(event.kind)}">
										<div class="event-head">
											<span class="event-ts">{event.ts}</span>
											<span class="event-kind" title={event.kind}>{event.kind}</span>
										</div>
										<p class="event-summary">{event.summary}</p>
										{#if event.mentions.length > 0}
											<div class="event-mentions">
												{#each event.mentions as mention (mention)}
													<span class="event-mention">{mention}</span>
												{/each}
											</div>
										{/if}
									</div>
								{:else}
									<p class="event-empty">No events extracted for this session.</p>
								{/each}
							</div>
						{/if}
					</div>
				{:else}
					<div class="empty">
						<span class="empty-icon" aria-hidden="true"><Icon name="timeline" size={30} /></span>
						<strong>No sessions carry this tag</strong>
						<small>Tag recordings in the Library to build the timeline.</small>
					</div>
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
						<button class="entity-row" type="button" onclick={() => startEdit(entity.slug, entity.label, entity.type)} title={`Rename ${entity.label}`}>
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
					<div class="empty">
						<span class="empty-icon" aria-hidden="true"><Icon name="speakers" size={30} /></span>
						<strong>No entities extracted yet</strong>
						<small>Run the pipeline on tagged recordings to populate the roster.</small>
					</div>
				{/each}
			</div>
		{:else}
			<section class="digest-recess" aria-label={`Digest · ${tag}`}>
				<header class="digest-header">
					<span class="digest-title">
						<Icon name="summary" size={11} />
						Digest
						<span class="digest-tag-name">{tag}</span>
					</span>
					<button type="button" class="digest-regen" disabled={digestGenerating} onclick={() => void regenerateDigestNow()}>
						<Icon name="refresh" size={11} strokeWidth={1.5} />
						Regenerate
					</button>
				</header>
				{#if digestLoading}
					<p class="tab-placeholder">Retrieving digest…</p>
				{:else if digestGenerating}
					<p class="tab-placeholder">Generating…</p>
				{:else if digestError}
					<p class="tab-error" role="alert">{digestError}</p>
				{:else if digestNote}
					<p class="tab-placeholder">{digestNote}</p>
				{:else if digestMissing}
					<p class="tab-placeholder">No digest yet — generate first.</p>
				{:else if digestText !== null}
					<div class="digest-body"><Markdown text={digestText} /></div>
				{/if}
			</section>
		{/if}
	{/if}
</section>
<style>
	.tag-page { display: flex; flex-direction: column; gap: 12px; min-height: 100%; }
	.tag-header { display: grid; grid-template-columns: auto 1fr; align-items: center; gap: 10px; }
	.tag-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 24px; }
	.back-button { width: 32px; height: 32px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #8e857b; cursor: pointer; line-height: 0; }
	.back-button:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.tag-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.tag-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.tag-error span { color: #c6baaa; }
	/* Brass underline tabs: the same control idiom as the digest-button
	   family (brass border/underline for the selected control). */
	.tag-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); }
	.tag-tab { display: inline-flex; align-items: center; gap: 6px; padding: 8px 10px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: #8e857b; font-size: 11px; font-weight: 650; cursor: pointer; transition: color 120ms ease, border-color 120ms ease; }
	.tag-tab:hover { color: var(--bone); }
	.tag-tab.active { color: var(--brass); border-bottom-color: var(--brass); }
	.session-list { display: grid; }
	.session-card { transition: background 120ms ease; }
	.session-card:not(:last-child) { border-bottom: 1px solid var(--line); }
	.session-heading { width: 100%; display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; padding: 11px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
	.session-heading:hover { background: rgba(255,255,255,.02); }
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
	.event-card { display: grid; gap: 5px; padding: 8px 10px 8px 9px; border-left: 2px solid var(--event-accent, var(--brass)); background: rgba(0,0,0,.18); border-radius: 0 3px 3px 0; box-shadow: inset 0 1px 3px rgba(0,0,0,.32); }
	.event-card + .event-card { margin-top: 6px; }
	.event-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
	.event-ts { font: 10px/1.4 "SFMono-Regular", Consolas, monospace; color: var(--brass); font-variant-numeric: tabular-nums; }
	.event-kind { color: var(--ash); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.event-summary { margin: 0; color: #c7bbad; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
	.event-mentions { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 1px; }
	.event-mention { padding: 1px 6px; border-radius: 2px; background: rgba(215,167,71,.08); color: var(--brass); font-size: 9px; font-weight: 650; line-height: 1.4; }
	.event-empty { margin: 0; padding: 8px 0; border-top: 1px solid var(--line); color: var(--ash); font-size: 11px; }
	.entity-row:not(:last-child) { border-bottom: 1px solid var(--line); }
	.entity-name { min-width: 0; }
	.entity-name strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: #ded3c4; }
	.entity-name small { display: block; margin-top: 3px; font-size: 9px; color: #8b8278; text-transform: capitalize; }
	.entity-meta { display: grid; justify-items: end; gap: 2px; }
	.entity-meta small { font-size: 10px; color: #8b8278; font-variant-numeric: tabular-nums; white-space: nowrap; }
	/* Digest recess: same material cut as the detail page's digest panel. */
	.digest-recess { flex: 1 1 auto; min-height: 160px; display: flex; flex-direction: column; overflow: hidden; background: rgba(0,0,0,.22); border-radius: 3px; box-shadow: inset 0 1px 3px rgba(0,0,0,.4); }
	.digest-header { display: flex; align-items: center; gap: 6px; padding: 5px 6px 5px 10px; border-bottom: 1px solid var(--line); }
	.digest-title { flex: 1; min-width: 0; display: inline-flex; align-items: center; gap: 6px; overflow: hidden; color: var(--ash); font-size: 9px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
	.digest-tag-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-transform: none; color: var(--bone); font-size: 10px; letter-spacing: 0.02em; }
	.digest-regen { display: inline-flex; align-items: center; gap: 5px; padding: 3px 8px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.digest-regen:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.digest-regen:disabled { opacity: 0.6; cursor: default; }
	.digest-body { flex: 1; min-height: 0; overflow: auto; padding: 4px 2px 8px; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.tab-placeholder { margin: auto; padding: 18px; color: var(--ash); font-size: 11px; }
	.tab-error { margin: auto; padding: 18px; color: #f36b60; font-size: 11px; }
	.notice-panel { display: grid; justify-items: start; gap: 6px; padding: 18px 14px; }
	.notice-panel strong { color: #b5aa9c; font-size: 13px; }
	.notice-panel small { color: #746d64; font-size: 11px; }
	.back-link { display: inline-flex; align-items: center; gap: 6px; margin-top: 4px; color: var(--brass); font-size: 11px; font-weight: 650; text-decoration: none; }
	.back-link:hover { color: var(--bone); }
	.empty { display: grid; justify-items: center; gap: 5px; padding: 28px 16px; color: #746d64; text-align: center; }
	.empty-icon { display: grid; place-items: center; color: var(--brass); line-height: 0; }
	.empty strong { color: #b5aa9c; font-size: 13px; }
	.empty small { font-size: 11px; }
	.skeleton-panel { display: grid; gap: 10px; padding: 0; }
	.skeleton-bar { display: block; border-radius: 2px; background: var(--iron-raised); animation: skeleton-pulse 150ms ease-in-out infinite alternate; }
	.skeleton-heading { width: 55%; height: 18px; }
	.skeleton-strip { width: 82%; height: 18px; }
	.skeleton-body { width: 100%; height: 90px; }
	/* Semantic search (3.5): recess panel + brass controls — same material
	   cut as the digest recess, same control idiom as the digest-regen
	   family. Hit rows are a ruled manifest on the recess surface. */
	.search-recess { display: flex; flex-direction: column; background: rgba(0,0,0,.22); border-radius: 3px; box-shadow: inset 0 1px 3px rgba(0,0,0,.4); }
	.search-form { display: grid; grid-template-columns: 1fr auto auto; align-items: stretch; gap: 6px; padding: 6px; }
	.search-input { min-width: 0; height: 30px; padding: 0 9px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 12px; }
	.search-input::placeholder { color: var(--ash); }
	.search-input:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }
	.search-input:disabled { opacity: 0.6; }
	.search-go { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 11px; font-weight: 700; cursor: pointer; }
	.search-go:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.search-go:disabled { opacity: 0.6; cursor: default; }
	.search-clear { display: inline-flex; align-items: center; justify-content: center; width: 30px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); cursor: pointer; }
	.search-clear:hover { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.search-note { margin: 0; padding: 0 10px 8px; color: var(--ash); font-size: 11px; line-height: 1.45; overflow-wrap: anywhere; }
	.search-error { margin: 0; padding: 0 10px 8px; color: #f36b60; font-size: 11px; }
	.hit-list { display: grid; max-height: 240px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.hit-row { display: grid; grid-template-columns: auto 1fr; align-items: baseline; gap: 4px 9px; padding: 8px 10px; border: 0; border-top: 1px solid var(--line); background: transparent; text-align: left; cursor: pointer; }
	.hit-row:first-child { border-top: 0; }
	.hit-row:hover { background: rgba(255,255,255,.02); }
	.hit-ts { font: 10px/1.4 "SFMono-Regular", Consolas, monospace; color: var(--brass); font-variant-numeric: tabular-nums; }
	.hit-main { min-width: 0; display: grid; gap: 3px; }
	.hit-main strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: #ded3c4; }
	.hit-main small { overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; color: #8b8278; font-size: 10px; line-height: 1.4; }
	@keyframes skeleton-pulse { from { opacity: 0.55; } to { opacity: 1; } }
	.entity-row:hover { background: rgba(255,255,255,.02); }
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
	@media (prefers-reduced-motion: reduce) { .skeleton-bar { animation: none; opacity: 0.75; } .session-chevron { transition: none; } }
</style>
