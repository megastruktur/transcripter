<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Icon, { type IconName } from '$lib/Icon.svelte';
	import ViewTabs from '$lib/ViewTabs.svelte';
	import Markdown from '$lib/Markdown.svelte';
	import TagEditor from '$lib/TagEditor.svelte';
	import BackButton from '$lib/BackButton.svelte';
	import DigestPanel from '$lib/DigestPanel.svelte';
	import NoticePanel from '$lib/NoticePanel.svelte';
	import Skeleton from '$lib/Skeleton.svelte';
	import {
		getRecording,
		updateRecording,
		deleteRecording,
		audioUrl,
		fetchArtifact,
		fetchDigest,
		regenerate,
		regenerateDigest,
		loadApiConfig,
		type EventsArtifact,
		type Recording,
		type Stage,
	} from '$lib/api.svelte';
	import { dateLabel, durationLabel } from '$lib/format';
	import { ensureTagSuggestions, tagSuggestionsCache } from '$lib/tag-suggestions.svelte';
	import { ensureProfiles, profilesCache } from '$lib/profiles.svelte';
	import { recordActions, stageNames, stageRetry, type ArtifactTabKey, type StageKind } from '$lib/stores.svelte';

	const id = page.params.id ?? '';

	let recording = $state<Recording | null>(null);
	let loading = $state(true);
	let notFound = $state(false);
	let error = $state('');
	let renaming = $state(false);
	let draft = $state('');
	let pendingTitle = $state<string | null>(null);
	let renameError = $state('');
	let rerunError = $state('');
	let deleting = $state(false);
	let deleteError = $state('');
	let pollTimer: ReturnType<typeof globalThis.setInterval> | null = null;
	let audioEl = $state<HTMLAudioElement>();
	let tagError = $state('');
	let tagSaving = $state(false);
	let tagSuggestions = $derived(tagSuggestionsCache.items);

	/** Per-tag digest viewer: one viewer at a time; regeneration is async,
	 * so the GET is polled every 10s for up to 2min after a 202. */
	const DIGEST_POLL_MS = 10_000;
	const DIGEST_POLL_BUDGET_MS = 120_000;
	let digestTag = $state<string | null>(null);
	let digestText = $state<string | null>(null);
	let digestLoading = $state(false);
	let digestMissing = $state(false);
	let digestError = $state('');
	let digestGenerating = $state(false);
	let digestNote = $state('');
	let digestPoll: ReturnType<typeof globalThis.setTimeout> | null = null;
	/** Pipeline-type editor state: typeEditing opens the editor; typeDraft is the draft value
	 * (null = "None — default pipeline", a real selectable value). */
	let typeEditing = $state(false);
	let typeDraft = $state<string | null>(null);
	let typeSaving = $state(false);
	let typeError = $state('');
	/** recorded_at editor state: null = editing closed; string = datetime-local draft. */
	let whenDraft = $state<string | null>(null);
	let whenSaving = $state(false);
	let whenError = $state('');
	const typeProfile = $derived(
		recording?.type ? profilesCache.items.find((profile) => profile.type === recording?.type) ?? null : null
	);

	type TabData = { kind: 'ready'; text: string } | { kind: 'missing' } | { kind: 'error'; message: string };
	const TAB_SPECS: Record<ArtifactTabKey, { label: string; stage: string; file?: string; markdown?: boolean }> = {
		transcript: { label: 'Transcript', stage: 'transcribe', markdown: true },
		speakers: { label: 'Speakers', stage: 'merge_speakers', markdown: true },
		events: { label: 'Events', stage: 'enrich', markdown: false },
		summary: { label: 'Summary', stage: 'summarize', markdown: true },
		json: { label: 'JSON', stage: 'transcribe', file: 'segments.json' }
	};
	/** Tab strip vocabulary mirrors TAB_SPECS order. Text-only: five tabs
	 * share one 366px workspace column — labels carry the meaning, the icon
	 * would only cost width (Vault's 3-tab strips keep their icons). */
	const ARTIFACT_VIEWS: { key: ArtifactTabKey; label: string }[] = [
		{ key: 'transcript', label: 'Transcript' },
		{ key: 'speakers', label: 'Speakers' },
		{ key: 'events', label: 'Events' },
		{ key: 'summary', label: 'Summary' },
		{ key: 'json', label: 'JSON' }
	];
	let tabData = $state<Partial<Record<ArtifactTabKey, TabData>>>({});
	let tabLoading = $state(false);
	let tabGeneration = 0;
	const tabInflight = new Set<ArtifactTabKey>();
	/** Active artifact view. Local state (not a store): the tabs live on
	 * this page now, so nothing outside needs to write the selection. */
	let activeTab = $state<ArtifactTabKey>('transcript');
	const currentTab = $derived(tabData[activeTab]);

	/** Recap marker from the summarize stage details (worker sets
	 * details.recap = { used, sessions, chars } when the memory recap was
	 * applied). Drives the small brass chip above the artifact panel. */
	const recapUsed = $derived(
		recording?.stages.some((s) => s.kind === 'summarize' && s.status === 'done' && (s.details?.recap as { used?: unknown } | undefined)?.used === true) ?? false
	);
	function defaultTab(rec: Recording): ArtifactTabKey {
		const done = (kind: Stage['kind'] | 'enrich') => rec.stages.some((s) => s.kind === kind && s.status === 'done');
		if (done('summarize')) return 'summary';
		if (done('merge_speakers') && done('enrich')) return 'events';
		if (done('merge_speakers')) return 'speakers';
		if (done('transcribe')) return 'transcript';
		return 'json';
	}

	/** "mm:ss" / "hh:mm:ss" → seconds; NaN for anything else. */
	function parseTs(ts: string): number {
		const parts = ts.split(':').map(Number);
		if (parts.some((n) => Number.isNaN(n)) || parts.length < 2 || parts.length > 3) return NaN;
		return parts.reduce((acc, n) => acc * 60 + n, 0);
	}

	/** events.json (fetched as text through the enrich artifact route) →
	 * parsed payload; null when the body is missing, garbage, or carries no
	 * events. Rendering is JSON-aware, never raw text. */
	function parseEventsArtifact(text: string): EventsArtifact | null {
		try {
			const parsed = JSON.parse(text) as EventsArtifact;
			if (!parsed || !Array.isArray(parsed.events)) return null;
			return parsed;
		} catch {
			return null;
		}
	}

	/** Events tab: click an event row to jump the player to its offset. */
	function seekToEvent(ts: string): void {
		if (!audioEl) return;
		const seconds = parseTs(ts);
		if (Number.isNaN(seconds)) return;
		audioEl.currentTime = seconds;
		audioEl.play().catch(() => {});
	}

	/** Deep-link seek (?t=seconds, e.g. from the Vault tag search): the
	 * offset is captured on mount and applied once the <audio> element
	 * reports loadedmetadata (currentTime sticks only then), after which
	 * the param is stripped so a reload doesn't re-seek. The player uses
	 * preload="metadata" — the header-only fetch is tiny and it makes
	 * loadedmetadata (and so the seek) fire without user interaction. */
	let pendingSeek = $state<number | null>(null);

	function consumePendingSeek(): void {
		const raw = page.url.searchParams.get('t');
		if (raw === null) return;
		const seconds = Number(raw);
		if (Number.isFinite(seconds) && seconds >= 0) pendingSeek = seconds;
		goto('?', { replaceState: true, keepFocus: true, noScroll: true });
	}

	function handleAudioMetadata(): void {
		if (pendingSeek === null || !audioEl) return;
		audioEl.currentTime = pendingSeek;
		pendingSeek = null;
	}

	function stopPoll(): void {
		if (pollTimer) {
			globalThis.clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	function startPoll(): void {
		stopPoll();
		pollTimer = globalThis.setInterval(() => void load(), 3000);
	}

	function invalidateArtifacts(): void {
		tabData = {};
		tabGeneration += 1;
	}

	function publishRecordActions(): void {
		recordActions.rename = startRename;
		recordActions.editDate = openWhenEditor;
		recordActions.editType = openTypeEditor;
		recordActions.remove = confirmDelete;
	}

	function clearRecordActions(): void {
		recordActions.loaded = false;
		recordActions.deletable = false;
		recordActions.rename = null;
		recordActions.editDate = null;
		recordActions.editType = null;
		recordActions.remove = null;
	}

	function applyRecording(next: Recording): void {
		const previous = recording;
		recording = pendingTitle !== null ? { ...next, title: pendingTitle } : next;
		stageRetry.stages = next.stages;
		stageRetry.enabled = next.state === 'done' || next.state === 'failed';
		publishRecordActions();
		recordActions.loaded = true;
		recordActions.deletable = next.state === 'done' || next.state === 'failed';
		if (previous?.state === 'processing' && next.state !== 'processing') {
			invalidateArtifacts();
			void loadTab(activeTab, true);
		}
		if (next.state === 'done' || next.state === 'failed') stopPoll();
	}

	async function load(): Promise<void> {
		try {
			const next = await getRecording(loadApiConfig(), id);
			notFound = false;
			error = '';
			applyRecording(next);
		} catch (caught) {
			const status = (caught as { status?: number }).status;
			if (status === 404 || status === 400) {
				notFound = true;
				error = '';
				stopPoll();
				stageRetry.stages = [];
				stageRetry.enabled = false;
				clearRecordActions();
			} else {
				error = String(caught);
				if (status === 401) stopPoll();
			}
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		stageRetry.rerun = rerun;
		consumePendingSeek();
		void (async () => {
			await load();
			if (!recording) return;
			activeTab = defaultTab(recording);
			if (recording.state === 'uploading' || recording.state === 'processing') startPoll();
		})();
		return () => {
			stopPoll();
			stopDigestPoll();
			stageRetry.stages = [];
			stageRetry.enabled = false;
			stageRetry.rerun = null;
			clearRecordActions();
		};
	});

	$effect(() => {
		if (!recording) return;
		void loadTab(activeTab);
	});

	$effect(() => {
		// Type hint + freehand-tag suggestions; failures degrade silently.
		void ensureProfiles(loadApiConfig());
		void ensureTagSuggestions(loadApiConfig());
	});

	function autofocus(node: HTMLElement): void {
		node.focus();
	}

	function startRename(): void {
		if (!recording) return;
		renaming = true;
		draft = recording.title;
		renameError = '';
	}

	function cancelRename(): void {
		renaming = false;
		draft = '';
	}

	function openTypeEditor(): void {
		if (!recording || typeEditing) return;
		typeEditing = true;
		typeDraft = recording.type ?? null;
	}

	function openWhenEditor(): void {
		if (!recording || whenDraft !== null) return;
		whenDraft = toLocalDatetimeValue(recording.recorded_at ?? recording.created_at ?? null);
	}

	async function commitRename(): Promise<void> {
		if (!recording) return;
		const previous = recording.title;
		const title = draft.trim();
		cancelRename();
		if (title === previous) return;
		recording = { ...recording, title };
		pendingTitle = title;
		try {
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { title }));
		} catch (caught) {
			recording = { ...recording, title: previous };
			renameError = `Rename failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			pendingTitle = null;
		}
	}

	function sameTags(a: string[], b: string[]): boolean {
		if (a.length !== b.length) return false;
		for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
		return true;
	}

	async function saveTags(next: string[]): Promise<void> {
		if (!recording) return;
		const previous = recording.tags;
		if (sameTags(previous, next)) return;
		recording = { ...recording, tags: next };
		tagSaving = true;
		tagError = '';
		try {
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { tags: next }));
			if (digestTag && !next.includes(digestTag)) closeDigest();
		} catch (caught) {
			recording = { ...recording, tags: previous };
			tagError = `Tag update failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			tagSaving = false;
		}
	}

	async function commitType(): Promise<void> {
		if (!recording || !typeEditing || typeSaving) return;
		const next = typeDraft;
		const previous = recording.type;
		typeEditing = false;
		if (next === previous) return;
		recording = { ...recording, type: next };
		typeSaving = true;
		typeError = '';
		try {
			// A type change re-runs the pipeline (summarize+enrich) on the
			// server when the recording is done — the response carries the
			// fresh state; the poller picks up the re-processing stages.
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { type: next }));
		} catch (caught) {
			recording = { ...recording, type: previous };
			typeError = `Type update failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			typeSaving = false;
		}
	}

	function toLocalDatetimeValue(iso: string | null): string {
		if (!iso) return '';
		const date = new Date(iso);
		if (Number.isNaN(date.getTime())) return '';
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
	}

	async function commitWhen(): Promise<void> {
		if (!recording || whenDraft === null || whenSaving) return;
		const draft = whenDraft;
		whenDraft = null;
		// Empty draft clears the backdate (falls back to created_at display).
		const iso = draft ? new Date(draft).toISOString() : null;
		const previous = recording.recorded_at;
		if (iso === previous) return;
		recording = { ...recording, recorded_at: iso };
		whenSaving = true;
		whenError = '';
		try {
			applyRecording(await updateRecording(loadApiConfig(), recording.id, { recorded_at: iso }));
		} catch (caught) {
			recording = { ...recording, recorded_at: previous };
			whenError = `Date update failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			whenSaving = false;
		}
	}

	function sizeLabel(bytes: number | null): string {
		if (bytes === null) return '—';
		return `${(bytes / 1_000_000).toFixed(1)} MB`;
	}

	async function rerun(stage: StageKind): Promise<void> {
		if (!recording) return;
		rerunError = '';
		try {
			await regenerate(loadApiConfig(), recording.id, stage);
			recording = { ...recording, state: 'processing' };
			stageRetry.enabled = false;
			invalidateArtifacts();
			startPoll();
		} catch (caught) {
			rerunError = `Re-run failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		}
	}

	async function loadTab(tab: ArtifactTabKey, force = false): Promise<void> {
		if (!recording) return;
		if (!force && (tabData[tab] !== undefined || tabInflight.has(tab))) return;
		const spec = TAB_SPECS[tab];
		const generation = ++tabGeneration;
		tabInflight.add(tab);
		tabLoading = true;
		try {
			const text = await fetchArtifact(loadApiConfig(), recording.id, spec.stage, spec.file);
			if (generation !== tabGeneration) return;
			tabData = { ...tabData, [tab]: { kind: 'ready', text } };
		} catch (caught) {
			if (generation !== tabGeneration) return;
			const status = (caught as { status?: number }).status;
			if (status === 404) {
				tabData = { ...tabData, [tab]: { kind: 'missing' } };
			} else {
				tabData = { ...tabData, [tab]: { kind: 'error', message: `${spec.label} failed to load: ${caught instanceof Error ? caught.message : String(caught)}` } };
			}
		} finally {
			tabInflight.delete(tab);
			if (generation === tabGeneration) tabLoading = false;
		}
	}

	function stopDigestPoll(): void {
		if (digestPoll) {
			globalThis.clearTimeout(digestPoll);
			digestPoll = null;
		}
	}

	function scheduleDigestPoll(tag: string, startedAt: number): void {
		digestPoll = globalThis.setTimeout(() => void pollDigestOnce(tag, startedAt), DIGEST_POLL_MS);
	}

	async function pollDigestOnce(tag: string, startedAt: number): Promise<void> {
		digestPoll = null;
		if (digestTag !== tag) return;
		try {
			const text = await fetchDigest(loadApiConfig(), tag);
			if (digestTag !== tag) return;
			digestText = text;
			digestGenerating = false;
			digestMissing = false;
			digestNote = '';
		} catch (caught) {
			if (digestTag !== tag) return;
			const status = (caught as { status?: number }).status;
			if (status === 404) {
				if (Date.now() - startedAt >= DIGEST_POLL_BUDGET_MS) {
					digestGenerating = false;
					digestNote = 'Still generating — check again in a minute.';
					return;
				}
				scheduleDigestPoll(tag, startedAt);
				return;
			}
			digestGenerating = false;
			digestError = `Digest failed to load: ${caught instanceof Error ? caught.message : String(caught)}`;
		}
	}

	async function openDigest(tag: string): Promise<void> {
		stopDigestPoll();
		digestTag = tag;
		digestText = null;
		digestMissing = false;
		digestError = '';
		digestNote = '';
		digestGenerating = false;
		digestLoading = true;
		try {
			const text = await fetchDigest(loadApiConfig(), tag);
			if (digestTag !== tag) return;
			digestText = text;
		} catch (caught) {
			if (digestTag !== tag) return;
			const status = (caught as { status?: number }).status;
			if (status === 404) {
				digestMissing = true;
			} else {
				digestError = `Digest failed to load: ${caught instanceof Error ? caught.message : String(caught)}`;
			}
		} finally {
			if (digestTag === tag) digestLoading = false;
		}
	}

	function closeDigest(): void {
		stopDigestPoll();
		digestTag = null;
		digestText = null;
		digestLoading = false;
		digestMissing = false;
		digestError = '';
		digestGenerating = false;
		digestNote = '';
	}

	async function regenerateDigestNow(): Promise<void> {
		if (!digestTag || digestGenerating) return;
		const tag = digestTag;
		stopDigestPoll();
		digestText = null;
		digestMissing = false;
		digestNote = '';
		digestError = '';
		digestGenerating = true;
		try {
			await regenerateDigest(loadApiConfig(), tag);
			scheduleDigestPoll(tag, Date.now());
		} catch (caught) {
			if (digestTag !== tag) return;
			digestGenerating = false;
			digestError = `Digest request failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		}
	}

	async function confirmDelete(): Promise<void> {
		if (!recording || deleting) return;
		deleting = true;
		deleteError = '';
		try {
			if (audioEl) {
				audioEl.pause();
				audioEl.removeAttribute('src');
				audioEl.load();
			}
			await deleteRecording(loadApiConfig(), recording.id);
			await goto('/recordings');
		} catch (caught) {
			if (audioEl) audioEl.src = audioUrl(loadApiConfig(), recording.id);
			deleteError = `Delete failed: ${caught instanceof Error ? caught.message : String(caught)}`;
			deleting = false;
		}
	}
</script>

<svelte:head><title>{recording?.title || 'Recording'} · Transcriptor Maximus</title></svelte:head>

<section class="page detail-page">
	<header class="detail-header">
		<BackButton href="/recordings" label="Back to recordings" />
		{#if loading}
			<Skeleton variant="heading" />
		{:else if recording && renaming}
			<div class="rename-row">
				<input
					type="text"
					{@attach autofocus}
					bind:value={draft}
					placeholder="Untitled capture"
					aria-label="Recording title"
					onkeydown={(event) => {
						if (event.key === 'Enter') commitRename();
						else if (event.key === 'Escape') {
							event.stopPropagation();
							event.preventDefault();
							cancelRename();
						}
					}}
				/>
				<button type="button" onclick={commitRename}>Save</button>
				<button type="button" onclick={cancelRename}>Cancel</button>
			</div>
		{:else if recording}
			<h1 class="page-title detail-title">{recording.title || 'Untitled capture'}</h1>
		{:else}
			<h1 class="page-title detail-title">Recording</h1>
		{/if}
	</header>

	{#if renameError}
		<p class="inline-error" role="alert">{renameError}</p>
	{/if}
	{#if deleteError}
		<p class="inline-error" role="alert">{deleteError}</p>
	{/if}

	{#if loading}
		<Skeleton variant="panel-detail" />
	{:else if notFound}
		<NoticePanel title="Recording not found" hint="It may have been deleted, or the address is wrong." backHref="/recordings" backLabel="Back to recordings" />
	{:else if !recording}
		<div class="archive-error" role="alert"><strong>Recording unavailable</strong><span>{error}</span></div>
	{:else}
		{#if error}
			<div class="archive-error" role="alert"><strong>Refresh failed</strong><span>{error}</span></div>
		{/if}

		<div class="detail-meta">
			<div class="meta-row">
				<span class={`state-mark ${recording.state}`} aria-hidden="true"></span>
				<span class="meta-text">{dateLabel(recording.recorded_at ?? recording.created_at)} · {durationLabel(recording.duration_sec)} · {sizeLabel(recording.total_bytes)}</span>
				<span class={`state-label ${recording.state}`}>{recording.state}</span>
			</div>
			<div class="meta-row type-row">
				{#if typeEditing}
					<select class="type-edit" bind:value={typeDraft} aria-label="Pipeline type" disabled={typeSaving}>
						<option value={null}>None — default pipeline</option>
						{#each profilesCache.items as profile (profile.id)}
							<option value={profile.type}>{profile.display_name}</option>
						{/each}
					</select>
					<button class="meta-edit-save" type="button" onclick={() => void commitType()} disabled={typeSaving}>Save</button>
					<button class="meta-edit-cancel" type="button" onclick={() => (typeEditing = false)} disabled={typeSaving}>Cancel</button>
				{:else}
					{#if recording.type}
						<span class="type-badge">{typeProfile ? typeProfile.display_name : recording.type}</span>
					{/if}
					{#if typeSaving}<span class="meta-saving">saving…</span>{/if}
				{/if}
				{#if whenDraft !== null}
					<input class="when-edit" type="datetime-local" bind:value={whenDraft} aria-label="Recorded at" disabled={whenSaving} />
					<button class="meta-edit-save" type="button" onclick={() => void commitWhen()} disabled={whenSaving}>Save</button>
					<button class="meta-edit-cancel" type="button" onclick={() => (whenDraft = null)} disabled={whenSaving}>Cancel</button>
				{/if}
				{#if whenSaving}<span class="meta-saving">saving…</span>{/if}
				{#if typeError}<span class="inline-error" role="alert">{typeError}</span>{/if}
				{#if whenError}<span class="inline-error" role="alert">{whenError}</span>{/if}
			</div>
		</div>
		<div class="tags-row" class:saving={tagSaving}>
			<span class="tags-label">Tags</span>
			<TagEditor
				tags={recording.tags}
				suggestions={tagSuggestions}
				onChange={(next) => void saveTags(next)}
			/>
		</div>
	{#if tagError}
		<p class="inline-error" role="alert">{tagError}</p>
	{:else if recording.tags.length > 0}
		<p class="tags-hint">Tag changes re-run memory extraction automatically.</p>
	{/if}
	{#if recording.state === 'done' && recording.tags.length > 0}
		<div class="digest-row" role="group" aria-label="Tag digests">
			{#each recording.tags as tag (tag)}
				<button
					type="button"
					class="digest-button"
					class:active={digestTag === tag}
					aria-pressed={digestTag === tag}
					onclick={() => (digestTag === tag ? closeDigest() : void openDigest(tag))}
				>
					<Icon name="summary" size={11} />
					{tag}
				</button>
			{/each}
		</div>
		{#if digestTag}
			<DigestPanel tag={digestTag} loading={digestLoading} generating={digestGenerating} error={digestError} note={digestNote} missing={digestMissing} text={digestText} onregen={() => void regenerateDigestNow()} onclose={closeDigest} />
		{/if}
	{/if}
	{#each recording.stages.filter((stage) => stage.status === 'failed' && stage.last_error) as stage (stage.kind)}
		<p class="stage-error" role="alert">{stageNames[stage.kind]} failed: {stage.last_error}</p>
	{/each}
	{#if rerunError}
		<p class="inline-error" role="alert">{rerunError}</p>
	{/if}


		{#if recording.state === 'uploading'}
			<p class="audio-note">Audio available after upload.</p>
		{:else}
		<audio bind:this={audioEl} class="audio-player" controls preload="metadata" src={audioUrl(loadApiConfig(), recording.id)} onloadedmetadata={handleAudioMetadata}></audio>
		{/if}
		<ViewTabs tabs={ARTIFACT_VIEWS} active={activeTab} ariaLabel="Artifact views" onchange={(key) => (activeTab = key as ArtifactTabKey)} />
		<div class="artifact-panel">
			{#each ARTIFACT_VIEWS as view (view.key)}
				{#if view.key === activeTab || tabData[view.key] !== undefined}
					<div class="artifact-view" class:hidden={view.key !== activeTab} role="tabpanel" aria-label={TAB_SPECS[view.key].label} data-tab={view.key}>
						{#if view.key === activeTab && tabLoading}
							<p class="tab-placeholder">Retrieving archive…</p>
						{:else if tabData[view.key]?.kind === 'ready' && view.key === 'events'}
							{@const data = tabData[view.key] as { kind: 'ready'; text: string }}
							{@const events = parseEventsArtifact(data.text)}
							{#if events === null}
								<p class="tab-placeholder">No events extracted yet.</p>
							{:else}
								<div class="events-body">
									{#each events.events as event, index (index + event.ts + event.summary)}
										<button class="event-row" type="button" title="Jump to {event.ts}" onclick={() => seekToEvent(event.ts)}>
											<span class="event-ts">{event.ts}</span>
											<span class="event-kind">{event.kind}</span>
											<span class="event-summary">{event.summary}</span>
											{#if event.mentions.length > 0}
												<span class="event-mentions">
													{#each event.mentions as mention (mention)}
														<span class="event-mention">{mention}</span>
													{/each}
												</span>
											{/if}
										</button>
									{:else}
										<p class="tab-placeholder">No events extracted yet.</p>
									{/each}
								</div>
							{/if}
						{:else if tabData[view.key]?.kind === 'ready'}
							{@const data = tabData[view.key] as { kind: 'ready'; text: string }}
							{#if view.key === 'summary' && recapUsed}
								<p class="recap-chip">
									<Icon name="enrich" size={11} />
									Memory applied
								</p>
							{/if}
							{#if TAB_SPECS[view.key].markdown}
								<Markdown text={data.text} />
							{:else}
								<pre>{data.text}</pre>
							{/if}
						{:else if tabData[view.key]?.kind === 'missing'}
							<p class="tab-placeholder">{view.key === 'events' ? 'No events extracted yet.' : 'This artifact has not been generated yet.'}</p>
						{:else if tabData[view.key]?.kind === 'error'}
							{@const errData = tabData[view.key] as { kind: 'error'; message: string }}
							<p class="tab-error" role="alert">{errData.message}</p>
						{/if}
					</div>
				{/if}
			{/each}
		</div>

	{/if}
</section>

<style>
	.detail-page { display: flex; flex-direction: column; gap: 12px; min-height: 100%; }
	.detail-header { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 10px; }
	.detail-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 24px; }
.detail-meta { display: flex; flex-direction: column; gap: 8px; }
.type-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.type-badge { padding: 2px 8px; border: 1px solid rgba(215,167,71,.35); border-radius: 2px; background: transparent; color: var(--brass); font-size: 10px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }
.type-edit, .when-edit { padding: 4px 6px; border: 1px solid rgba(215,167,71,.4); border-radius: 2px; background: rgba(0,0,0,0.25); color: var(--bone); font-size: 11px; color-scheme: dark; }
.meta-edit-save { padding: 4px 8px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,0.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
.meta-edit-cancel { padding: 4px 8px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #8e857b; font-size: 10px; cursor: pointer; }
.meta-edit-save:disabled, .meta-edit-cancel:disabled { opacity: 0.6; cursor: default; }
.meta-saving { font-size: 10px; color: #8d847a; }
	.meta-row { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 9px; }
	.meta-text { font-size: 11px; color: #8b8278; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.state-mark { width: 7px; height: 7px; border-radius: 50%; background: #706960; box-shadow: 0 0 0 3px rgba(112,105,96,.12); }
	.state-mark.done { background: var(--cyan); box-shadow: 0 0 9px rgba(112,215,208,.55); }
	.state-mark.processing, .state-mark.uploading { background: var(--brass); box-shadow: 0 0 9px rgba(215,167,71,.5); }
	.state-mark.failed { background: var(--red); box-shadow: 0 0 9px rgba(213,45,36,.6); }
	.state-label { color: #968d83; font-size: 9px; font-weight: 700; text-transform: capitalize; }
	.state-label.done { color: var(--cyan); }
	.state-label.processing, .state-label.uploading { color: var(--brass); }
	.state-label.failed { color: #f36b60; }
	.stage-error { margin: 0; color: #f36b60; font-size: 11px; }
	.tags-row { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 9px; padding-top: 8px; border-top: 1px solid var(--line); transition: opacity 120ms ease; }
	.tags-row.saving { opacity: 0.65; }
	.tags-label { font-size: 10px; font-weight: 650; color: #8b8278; letter-spacing: 0.02em; }
	.tags-hint { margin: 0; color: var(--ash); font-size: 10px; }
	.digest-row { display: flex; flex-wrap: wrap; gap: 6px; }
	.digest-button { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); font-size: 10px; font-weight: 650; cursor: pointer; }
	.digest-button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	/* The chip belongs to the Summary artifact content, not to the page flow:
	 * when it sat above the tab strip, entering Summary shifted the tabs down.
	 * Inside the panel it scrolls with the summary; the margin keeps it clear
	 * of the markdown body below. inline-flex — display:flex would stretch
	 * the chip banner-wide across the view. */
	.recap-chip { display: inline-flex; align-self: flex-start; align-items: center; gap: 5px; margin: 0 0 8px; padding: 2px 8px; border: 1px solid rgba(215,167,71,.35); border-radius: 2px; color: var(--brass); font-size: 9px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }
	.audio-player { width: 100%; height: 36px; border-radius: 2px; background: rgba(0,0,0,.14); color-scheme: dark; accent-color: var(--brass); }
	.audio-note { margin: 0; font-size: 11px; color: var(--ash); }
	.events-body { flex: 1; min-height: 0; overflow: auto; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.event-row { width: 100%; display: grid; grid-template-columns: auto auto 1fr; align-items: baseline; gap: 4px 8px; padding: 7px 10px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: inherit; text-align: left; cursor: pointer; transition: background 120ms ease; }
	.event-row:hover { background: rgba(255,255,255,.03); }
	.event-row:last-child { border-bottom: 0; }
	.event-ts { font: 10px/1.4 "SFMono-Regular", Consolas, monospace; color: var(--brass); font-variant-numeric: tabular-nums; }
	.event-kind { color: var(--ash); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
	.event-summary { color: #c7bbad; font-size: 11px; line-height: 1.45; overflow-wrap: anywhere; }
	.event-mentions { grid-column: 3; display: flex; flex-wrap: wrap; gap: 4px; }
	.event-mention { padding: 1px 6px; border-radius: 2px; background: rgba(215,167,71,.08); color: var(--brass); font-size: 9px; font-weight: 650; line-height: 1.4; }
	.artifact-panel { flex: 1 1 auto; min-height: 220px; display: flex; flex-direction: column; overflow: hidden; background: rgba(0,0,0,.22); border-radius: 3px; box-shadow: inset 0 1px 3px rgba(0,0,0,.4); }
	/* Keep-alive shell: visited views stay mounted (switching back is free —
	 * no re-parse of marked/DOMPurify), hidden ones skip layout and paint
	 * entirely. Only views whose tab was ever activated render content, so
	 * the mount of a single view never parses all five artifacts at once. */
	.artifact-view { flex: 1; min-height: 0; display: flex; flex-direction: column; }
	.artifact-view.hidden { display: none; }
	.artifact-view pre { flex: 1; min-height: 0; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap; color: #c7bbad; font: 11px/1.6 "SFMono-Regular", Consolas, monospace; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	/* JSON artifacts are one huge <pre> — no block boundaries to skip per
	 * child, so windowed rendering needs the pre itself to be the
	 * content-visibility root. Markdown bodies get the same treatment on
	 * their children inside Markdown.svelte. */
	.artifact-view pre { content-visibility: auto; contain-intrinsic-size: auto 500px; }
	.tab-placeholder { margin: auto; padding: 18px; color: var(--ash); font-size: 11px; }
	.tab-error { margin: auto; padding: 18px; color: #f36b60; font-size: 11px; }
	.rename-row { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 5px; }
	.rename-row input { min-height: 32px; height: 32px; }
	.rename-row button { min-height: 32px; padding: 0 10px; border: 1px solid rgba(215,167,71,.26); border-radius: 2px; background: rgba(215,167,71,.06); color: var(--brass); font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.rename-row button:hover { border-color: var(--brass); background: rgba(215,167,71,.12); }
	.inline-error { margin: 0; color: #f36b60; font-size: 11px; }
	.archive-error { display: grid; gap: 4px; padding: 11px 12px; border-left: 2px solid var(--red); background: rgba(213,45,36,.08); font-size: 12px; }
	.archive-error strong { color: var(--red); font-size: 10px; font-weight: 700; }
	.archive-error span { color: #c6baaa; }
</style>
