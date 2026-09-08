import { clientHeader } from '$lib/platform';

export type ApiConfig = {
	baseUrl: string;
	token: string;
};

export type StageStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped';


export type Recording = {
	id: string;
	title: string;
	tags: string[];
	state: 'uploading' | 'processing' | 'done' | 'failed';
	committed_bytes: number;
	total_bytes: number | null;
	duration_sec: number | null;
	created_at: string;
	/** Pipeline type slug (`meeting`, `ttrpg`, …); null = default pipeline. */
	type: string | null;
	/** When the audio actually sounded (import backdate); null = unknown —
	 * clients display coalesce(recorded_at, created_at). */
	recorded_at: string | null;
	stages: Stage[];
};

export type ProfileInfo = {
	id: string;
	version: number;
	display_name: string;
	description: string;
	/** Slug of the pipeline this profile routes to (`meeting`, `ttrpg`…). */
	type: string | null;
	/** Kept for the transition: legacy profiles may still carry tags. */
	tags: string[];
	has_enrich: boolean;
};

export type RecordingPage = {
	items: Recording[];
	total: number;
	limit: number;
	offset: number;
};

export type ListParams = {
	limit: number;
	offset: number;
	q?: string;
	state?: Recording['state'] | 'all';
};

export type UpdateRecordingPatch = {
	title?: string;
	tags?: string[];
	/** Type slug; null clears the type (back to the default pipeline). */
	type?: string | null;
	/** ISO-8601 timestamp; null clears the backdate. */
	recorded_at?: string | null;
};

export type Stage = {
	kind: 'chunk' | 'transcribe' | 'diarize' | 'merge_speakers' | 'summarize' | 'enrich' | 'separate';
	status: StageStatus;
	attempts: number;
	last_error: string | null;
	updated_at: string;
	/** Stage-reported extra state, e.g. summarize's recap marker
	 * ({ recap: { used, sessions, chars } }); absent unless set. */
	details?: Record<string, unknown>;
};

const STORAGE_KEY = 'transcripter.apiConfig';

export function loadApiConfig(): ApiConfig {
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (raw) return JSON.parse(raw) as ApiConfig;
	} catch {
		// ignore corrupt storage
	}
	return { baseUrl: 'http://localhost:8090', token: '' };
}

export function saveApiConfig(cfg: ApiConfig): void {
	localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
}

async function req(
	cfg: ApiConfig,
	path: string,
	init?: RequestInit
): Promise<Response> {
	const resp = await fetch(`${cfg.baseUrl.replace(/\/$/, '')}${path}`, {
		...init,
		headers: {
			authorization: `Bearer ${cfg.token}`,
			'content-type': 'application/json',
			'x-client': clientHeader(),
			...(init?.headers ?? {})
		}
	});
	if (resp.status === 401) throw Object.assign(new Error('unauthorized: check token in Settings'), { status: 401 });
	return resp;
}

export async function testConnection(cfg: ApiConfig): Promise<string> {
	const base = cfg.baseUrl.replace(/\/$/, '');
	const client = { 'x-client': clientHeader() };
	const health = await fetch(`${base}/health`, { headers: client });
	if (!health.ok) throw new Error(`health ${health.status}`);
	const authed = await fetch(`${base}/recordings`, {
		headers: { authorization: `Bearer ${cfg.token}`, ...client }
	});
	if (authed.status === 401) throw new Error('unauthorized: wrong token');
	if (!authed.ok) throw new Error(`recordings ${authed.status}`);
	// Pre-v0.32 servers have no version in /health — keep '' so the
	// Settings panel degrades to a dash instead of "undefined".
	const version = (await health.json().catch(() => null))?.version;
	return typeof version === 'string' ? version : '';
}
export async function listRecordings(cfg: ApiConfig, params: ListParams): Promise<RecordingPage> {
	const search = new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) });
	const q = params.q?.trim();
	if (q) search.set('q', q);
	if (params.state && params.state !== 'all') search.set('state', params.state);
	const resp = await req(cfg, `/recordings?${search}`);
	if (!resp.ok) throw new Error(`list ${resp.status}`);
	return resp.json();
}

export async function listProfiles(cfg: ApiConfig): Promise<ProfileInfo[]> {
	const resp = await req(cfg, '/profiles');
	if (!resp.ok) throw new Error(`profiles ${resp.status}`);
	return resp.json();
}

export type TagCount = {
	tag: string;
	count: number;
	/** Registry row exists (tag_defs): the tag was created on the Tags
	 * page or auto-registered by a recording. */
	registered?: boolean;
	/** Hot-word entries in the tag's vocabulary. */
	vocabulary_count?: number;
};

/** Distinct freehand tags with session counts, ordered count desc then
 * tag asc — the server's ordering; no client re-sort. Source for the
 * recorder/import tag pickers (recent freehand tags), replacing the old
 * profile-tags source. */
export async function fetchTags(cfg: ApiConfig): Promise<TagCount[]> {
	const resp = await req(cfg, '/tags');
	if (!resp.ok) throw new Error(`tags ${resp.status}`);
	const body = (await resp.json()) as { items: TagCount[] };
	return body.items;
}

/** One registry row (GET /tags/{tag}): vocabulary + context + counts. */
export type TagDef = {
	name: string;
	vocabulary: string[];
	/** Operator-written LLM context (summarize / enrich / digest prompts). */
	context: string;
	recordings: number;
	created_at: string;
};

/** Create a tag in the registry before any recording carries it.
 * Throws .status 409 (exists), 400 (bad name). */
export async function createTag(
	cfg: ApiConfig,
	name: string,
	vocabulary: string[] = [],
	context = ''
): Promise<TagDef> {
	const resp = await req(cfg, '/tags', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ name, vocabulary, context })
	});
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(new Error(detail || `create tag ${resp.status}`), { status: resp.status });
	}
	return resp.json();
}

/** Registry row for the tag editor. Throws .status 404 (unregistered). */
export async function fetchTagDef(cfg: ApiConfig, tag: string): Promise<TagDef> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}`);
	if (!resp.ok) throw Object.assign(new Error(`tag ${resp.status}`), { status: resp.status });
	return resp.json();
}

/** Update registry fields (full-replace per field, absent = unchanged).
 * Upserts: a tag that only exists on recordings gains a registry row. */
export async function updateTag(
	cfg: ApiConfig,
	tag: string,
	fields: { vocabulary?: string[]; context?: string }
): Promise<TagDef> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}`, {
		method: 'PATCH',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(fields)
	});
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(new Error(detail || `update tag ${resp.status}`), { status: resp.status });
	}
	return resp.json();
}

/** Remove the registry row (vocabulary). 409 while recordings carry the
 * tag; the tag's memory has its own purge path. */
export async function deleteTagDef(cfg: ApiConfig, tag: string): Promise<void> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' });
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(new Error(detail || `delete tag ${resp.status}`), { status: resp.status });
	}
}

export type TimelineEvent = {
	/** Deterministic event identity (server-computed for legacy files) —
	 * the address for phase-A event edits. */
	event_key: string;
	/** In-recording offset as written by enrich ("mm:ss" / "hh:mm:ss"). */
	ts: string;
	kind: string;
	summary: string;
	mentions: string[];
};

export type TimelineSession = {
	recording_id: string;
	title: string;
	/** ISO-8601 UTC — coalesce(recorded_at, created_at). */
	date: string;
	type: string | null;
	duration_sec: number | null;
	events: TimelineEvent[];
	entity_count: number;
};

export type TagEntity = {
	slug: string;
	label: string;
	type: string;
	sessions: number;
	/** ISO-8601 UTC — newest session mentioning the entity. */
	last_seen: string;
	/** Entity dossier (who/what it is in the story) as of its last
	 * session appearance; absent when the describe pass never ran or
	 * returned nothing for this entity. */
	description?: string;
};

export type TimelineResponse = {
	tag: string;
	/** Newest first. */
	sessions: TimelineSession[];
	/** Aggregated by last_seen DESC. */
	entities: TagEntity[];
	digest_generated: boolean;
};

export type VaultItem = {
	tag: string;
	sessions: number;
	entities: number;
	last_activity: string;
	/** ready = digest note present; stale = note older than the newest
	 * session; none = no note. Registry-only tags (0 sessions) send an
	 * empty last_activity and digest "none". */
	digest: 'ready' | 'stale' | 'none';
	/** Words in the tag's registry vocabulary (0 = no registry row). */
	vocabulary_count: number;
};

export type VaultResponse = {
	items: VaultItem[];
};

/** Per-tag timeline (Phase 3): sessions newest-first with their extracted
 * events plus the tag's aggregated entities. */
export async function fetchTimeline(cfg: ApiConfig, tag: string): Promise<TimelineResponse> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/timeline`);
	if (!resp.ok) throw Object.assign(new Error(`timeline ${resp.status}`), { status: resp.status });
	return resp.json();
}

/** Vault overview (Phase 3): one row per free tag with session/entity
 * counts and digest freshness. */
export async function fetchVault(cfg: ApiConfig): Promise<VaultResponse> {
	const resp = await req(cfg, '/vault');
	if (!resp.ok) throw Object.assign(new Error(`vault ${resp.status}`), { status: resp.status });
	return resp.json();
}

/** Shape of the enrich stage's meta/events.json artifact (written by
 * worker/enrich.write_events_json; served via the enrich artifact route). */
export type EventsArtifact = {
	recording_id: string;
	recording_date: string;
	recording_title: string;
	profile_id: string;
	namespaces: string[];
	events: TimelineEvent[];
	entities: { slug: string; label: string; type: string }[];
	relations: { from: string; to: string; type: string }[];
};

export type DigestReference = {
	/** Recording id for click-through navigation. */
	id: string;
	/** Display title ('(untitled)' placeholder server-side). */
	title: string;
	/** ISO-8601 — coalesce(recorded_at, created_at). */
	recorded_at: string | null;
};

export type DigestNote = {
	tag: string;
	/** Frontmatter value, host metadata (shown in the panel tooltip). */
	generated_at: string;
	/** Markdown body WITHOUT frontmatter — ready for the Markdown view. */
	body: string;
	/** Reference recordings (frontmatter ids resolved against the
	 * catalog; deleted ids dropped server-side), oldest first. */
	recordings: DigestReference[];
};

/** Per-tag digest note produced by the enrich workflow, served as
 * structured JSON (2026-09-08): body + reference recordings. Throws
 * with .status 404 when the digest is not generated yet. */
export async function fetchDigest(cfg: ApiConfig, tag: string): Promise<DigestNote> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/digest`);
	if (!resp.ok) throw Object.assign(new Error(`digest ${resp.status}`), { status: resp.status });
	return resp.json();
}

/** Trigger digest (re)generation: the server replies 202 and runs the
 * workflow asynchronously — poll fetchDigest until the note appears.
 * Body `{}` keeps the POST a JSON request (the endpoint accepts a bare
 * POST too, but an explicit empty object documents the contract and is
 * safe against future required fields). */
export async function regenerateDigest(cfg: ApiConfig, tag: string): Promise<void> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/digest`, {
		method: 'POST',
		body: JSON.stringify({})
	});
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `digest ${resp.status}`),
			{ status: resp.status }
		);
	}
}

export type MemoryWorkflowStatus = {
	state: 'running' | 'done' | 'failed' | 'unknown';
	progress?: { total: number; done: number; current: string | null };
	result?: { purged?: Record<string, number>; rebuilt?: number; failed?: string[] };
	detail?: string;
};

/** Admin: wipe the tag's memory (graph namespace, edits, digest,
 * semantic index) — recordings stay. `rebuild` also re-runs the LLM
 * stages per recording (oldest first); `startStage` picks where the
 * children restart: 'summarize' (default — prompts/tag context changed,
 * summaries rebuild too) or 'enrich' (graph only). 202 + workflow; poll
 * with fetchMemoryStatus. Throws with .status 404 (no done recordings),
 * 409 (graph off | recording processing | already running), 422 (bad
 * stage), 503. */
export async function purgeTagMemory(
	cfg: ApiConfig,
	tag: string,
	rebuild: boolean,
	startStage: 'summarize' | 'enrich' = 'summarize'
): Promise<{ workflow_id: string }> {
	const resp = await req(
		cfg,
		rebuild ? `/tags/${encodeURIComponent(tag)}/rebuild` : `/tags/${encodeURIComponent(tag)}/memory`,
		rebuild ? { method: 'POST', body: JSON.stringify({ start_stage: startStage }) } : { method: 'DELETE' }
	);
	if (!resp.ok) {
		const detail = await resp.json().catch(() => ({ detail: resp.status }));
		throw Object.assign(new Error(detail.detail ?? `memory ${resp.status}`), { status: resp.status });
	}
	return resp.json();
}

export async function fetchMemoryStatus(cfg: ApiConfig, tag: string, workflowId: string): Promise<MemoryWorkflowStatus> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/memory/${encodeURIComponent(workflowId)}`);
	if (!resp.ok) throw new Error(`memory status ${resp.status}`);
	return resp.json();
}

/** Phase 4: rename one entity (label ± type) in the tag's namespace.
 * The slug is identity and never changes; the server replies 202 and
 * applies the write in a Temporal workflow — the caller renders the
 * optimistic label immediately and rolls back on error. Throws with
 * .status 404 (unknown tag/slug), 409 (graph off), 503 (temporal down). */
export async function patchEntity(
	cfg: ApiConfig,
	tag: string,
	slug: string,
	label: string,
	type?: string
): Promise<{ workflow_id: string; label: string }> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/entities/${encodeURIComponent(slug)}`, {
		method: 'PATCH',
		body: JSON.stringify(type !== undefined ? { label, type } : { label })
	});
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `rename ${resp.status}`),
			{ status: resp.status }
		);
	}
	return resp.json();
}

/** Manual dossier edit: write the user's text onto the entity node
 * (arms description_edited — generated passes never stomp it) and
 * propagate into the tag's events.json copies. 202 + Temporal, same
 * shape as patchEntity. */
export async function setEntityDescription(
	cfg: ApiConfig,
	tag: string,
	slug: string,
	description: string
): Promise<{ workflow_id: string }> {
	const resp = await req(
		cfg,
		`/tags/${encodeURIComponent(tag)}/entities/${encodeURIComponent(slug)}/description`,
		{ method: 'PATCH', body: JSON.stringify({ description }) }
	);
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `dossier set ${resp.status}`),
			{ status: resp.status }
		);
	}
	return resp.json();
}

/** Manual dossier REFRESH: rebuild the card from the full material
 * (every mentioning event + neighbors) via one worker LLM call.
 * Refused with 409 when the dossier is user-edited (description_edited). */
export async function refreshEntityDescription(
	cfg: ApiConfig,
	tag: string,
	slug: string
): Promise<{ workflow_id: string }> {
	const resp = await req(
		cfg,
		`/tags/${encodeURIComponent(tag)}/entities/${encodeURIComponent(slug)}/refresh-description`,
		{ method: 'POST' }
	);
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `dossier refresh ${resp.status}`),
			{ status: resp.status }
		);
	}
	return resp.json();
}

// ---------------------------------------------------------------------------
// Phase A/D: knowledge-graph editing + the Lattice tab's read model.
// ---------------------------------------------------------------------------

export type GraphEntity = {
	slug: string;
	label: string;
	type: string;
	sessions: number;
	/** Dossier from the tag's events.json copies ('' when none). */
	description?: string;
};

export type GraphRelation = {
	from: string;
	to: string;
	type: string;
	/** How many of the tag's sessions carry the edge. */
	sessions: number;
};

export type GraphResponse = {
	tag: string;
	entities: GraphEntity[];
	relations: GraphRelation[];
};

/** Phase A: nodes + edges for the Lattice tab, aggregated from
 * events.json (no Neo4j session in the API). Throws with .status 404
 * (unknown tag) / 409 (graph off). */
export async function fetchGraph(cfg: ApiConfig, tag: string): Promise<GraphResponse> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/graph`);
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `graph ${resp.status}`),
			{ status: resp.status }
		);
	}
	return resp.json();
}
export type GraphEditRow = {
	id: number;
	tag: string;
	target: 'event' | 'entity' | 'relation';
	op: 'update' | 'delete' | 'create' | 'merge';
	obj_key: string;
	anchor: Record<string, unknown>;
	before: Record<string, unknown>;
	after: Record<string, unknown>;
	feedback_text: string | null;
	source: 'user' | 'agent';
	status: 'applied' | 'orphaned' | 'retired';
	created_at: string;
};

// ---------------------------------------------------------------------------
// Phase A edit surface (deterministic ops) — every call is 202 + a Temporal
// workflow that applies the mutation to the graph + events.json copies and
// signals digest maintenance. Throws with .status 404 (unknown target) /
// 409 (graph off) / 503 (temporal down).
// ---------------------------------------------------------------------------

async function _editReq(
	cfg: ApiConfig,
	path: string,
	method: string,
	body?: unknown
): Promise<{ workflow_id: string; edit_id: number; tag: string }> {
	const resp = await req(cfg, path, {
		method,
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `edit ${resp.status}`),
			{ status: resp.status }
		);
	}
	return resp.json();
}

/** Edit one event (any of ts/kind/summary/mentions); feedback_text is the
 * natural-language rule stored for the enrich prompt block. */
export async function patchGraphEvent(
	cfg: ApiConfig,
	tag: string,
	eventKey: string,
	patch: { ts?: string; kind?: string; summary?: string; mentions?: string[]; feedback_text?: string }
): Promise<{ workflow_id: string; edit_id: number }> {
	return _editReq(cfg, `/tags/${encodeURIComponent(tag)}/events/${encodeURIComponent(eventKey)}`, 'PATCH', patch);
}

/** Delete one event from the timeline. */
export async function deleteGraphEvent(
	cfg: ApiConfig,
	tag: string,
	eventKey: string
): Promise<{ workflow_id: string; edit_id: number }> {
	return _editReq(cfg, `/tags/${encodeURIComponent(tag)}/events/${encodeURIComponent(eventKey)}`, 'DELETE');
}

/** Create a user-authored relation (overlay re-creates it after every
 * regenerate). */
export async function createGraphRelation(
	cfg: ApiConfig,
	tag: string,
	fromSlug: string,
	toSlug: string,
	type: string,
	feedbackText?: string
): Promise<{ workflow_id: string; edit_id: number }> {
	return _editReq(cfg, `/tags/${encodeURIComponent(tag)}/relations`, 'POST', {
		from_slug: fromSlug,
		to_slug: toSlug,
		type,
		feedback_text: feedbackText || undefined
	});
}

/** Delete a relation (tombstone — user decisions outrank the model). */
export async function deleteGraphRelation(
	cfg: ApiConfig,
	tag: string,
	fromSlug: string,
	toSlug: string,
	type: string
): Promise<{ workflow_id: string; edit_id: number }> {
	return _editReq(cfg, `/tags/${encodeURIComponent(tag)}/relations`, 'DELETE', {
		from_slug: fromSlug,
		to_slug: toSlug,
		type
	});
}

/** Delete an entity (node + edges, slug pruned from every events.json). */
export async function deleteGraphEntity(
	cfg: ApiConfig,
	tag: string,
	slug: string
): Promise<{ workflow_id: string; edit_id: number }> {
	return _editReq(cfg, `/tags/${encodeURIComponent(tag)}/entities/${encodeURIComponent(slug)}`, 'DELETE');
}

/** Fold source into target (redirect edges, union sessions, tombstone
 * the source slug). */
export async function mergeGraphEntities(
	cfg: ApiConfig,
	tag: string,
	sourceSlug: string,
	targetSlug: string,
	feedbackText?: string
): Promise<{ workflow_id: string; edit_id: number }> {
	return _editReq(cfg, `/tags/${encodeURIComponent(tag)}/entities/merge`, 'POST', {
		source_slug: sourceSlug,
		target_slug: targetSlug,
		feedback_text: feedbackText || undefined
	});
}

/** Retire a correction rule: the row leaves the {corrections} prompt
 * block and the overlay stops re-applying it. Deterministic, no workflow. */
export async function retireGraphEdit(cfg: ApiConfig, tag: string, editId: number): Promise<void> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/edits/${editId}/retire`, { method: 'POST' });
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `retire ${resp.status}`),
			{ status: resp.status }
		);
	}
}

export type DigestStatus = { state: 'fresh' | 'queued'; last_edit_at: string | null; debounce_sec: number };

/** Digest renewal state — `queued` while the newest edit is younger than
 * the digest note's mtime (the Digest tab's brass lamp). */
export async function fetchDigestStatus(cfg: ApiConfig, tag: string): Promise<DigestStatus> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/digest/status`);
	if (!resp.ok) throw Object.assign(new Error(`digest status ${resp.status}`), { status: resp.status });
	return resp.json();
}

// ---------------------------------------------------------------------------
// Phase C: "Correct the record" — AI-translated fixes (preview → confirm →
// apply). The LLM proposes; the user confirms; fix-apply re-validates and
// mutates all-or-nothing through the phase-A updaters.
// ---------------------------------------------------------------------------

export type FixOp = {
	op:
		| 'event_update'
		| 'event_delete'
		| 'relation_create'
		| 'relation_delete'
		| 'entity_rename'
		| 'entity_merge'
		| 'entity_delete';
	event_key?: string;
	/** event_update only — changed fields (summary/kind/ts/mentions). */
	after?: Record<string, unknown>;
	slug?: string;
	/** entity_rename only. */
	label?: string;
	/** entity_rename only (optional new type); relation ops (type of the edge). */
	type?: string;
	/** relation ops. */
	from?: string;
	to?: string;
	/** entity_merge only. */
	source?: string;
	target?: string;
	[key: string]: unknown;
};

export type FixProposal = { ops: FixOp[]; rationale?: string[] };

export type FixPreviewPoll =
	| { state: 'running' | 'unknown' | 'busy' | 'unparseable' | 'invalid' | 'failed'; detail?: string }
	| { state: 'ready'; proposal: FixProposal; context?: Record<string, unknown> };

/** Request a proposal for ONE natural-language instruction (ONE LLM
 * call in one activity). 202 + workflow id; poll with pollFixPreview.
 * Throws .status 409 (one already running) / 429 (cooldown). */
export async function startFixPreview(
	cfg: ApiConfig,
	tag: string,
	instruction: string,
	recordingId?: string
): Promise<{ workflow_id: string }> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/fix-preview`, {
		method: 'POST',
		body: JSON.stringify({ instruction, recording_id: recordingId || undefined })
	});
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `fix-preview ${resp.status}`),
			{ status: resp.status }
		);
	}
	return resp.json();
}

export async function pollFixPreview(cfg: ApiConfig, tag: string, workflowId: string): Promise<FixPreviewPoll> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/fix-preview/${encodeURIComponent(workflowId)}`);
	if (!resp.ok) throw Object.assign(new Error(`fix-preview poll ${resp.status}`), { status: resp.status });
	return resp.json();
}

export type FixApplyPoll =
	| { state: 'running' | 'unknown' | 'failed'; detail?: string }
	| { state: 'ok'; applied: number; edit_ids: number[] }
	| { state: 'stale'; rejections: { op_index: number; reason: string }[] };

export async function startFixApply(
	cfg: ApiConfig,
	tag: string,
	proposal: FixProposal,
	feedbackText?: string
): Promise<{ workflow_id: string }> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/fix-apply`, {
		method: 'POST',
		body: JSON.stringify({ proposal, feedback_text: feedbackText || undefined })
	});
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : `fix-apply ${resp.status}`),
			{ status: resp.status }
		);
	}
	return resp.json();
}

export async function pollFixApply(cfg: ApiConfig, tag: string, workflowId: string): Promise<FixApplyPoll> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/fix-apply/${encodeURIComponent(workflowId)}`);
	if (!resp.ok) throw Object.assign(new Error(`fix-apply poll ${resp.status}`), { status: resp.status });
	return resp.json();
}


/** Phase A audit: every edit row of the tag, newest first. */
export async function fetchGraphEdits(cfg: ApiConfig, tag: string): Promise<GraphEditRow[]> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/edits`);
	if (!resp.ok) throw new Error(`edits ${resp.status}`);
	const body = (await resp.json()) as { items: GraphEditRow[] };
	return body.items;
}

export type SearchHit = {
	recording_id: string;
	session_title: string;
	/** Seconds into the recording. */
	ts_start: number;
	ts_end: number;
	speaker: string;
	snippet: string;
	/** vec0 distance — smaller is closer. */
	distance: number;
};

export type SearchResponse = {
	tag: string;
	query: string;
	hits: SearchHit[];
};

export type SearchUnavailable = {
	available: false;
	reason: string;
};

/** Phase 3.5: semantic search over the tag's indexed transcript
 * segments. Throws with .status 503 + .reason when the backend/index is
 * unavailable (the caller surfaces the hint, e.g. run backfill). */
export async function searchTag(
	cfg: ApiConfig,
	tag: string,
	q: string,
	k = 20
): Promise<SearchResponse> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/search?q=${encodeURIComponent(q)}&k=${k}`);
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail as SearchUnavailable | string | null;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : (detail?.reason ?? `search ${resp.status}`)),
			{ status: resp.status, reason: typeof detail === 'object' && detail ? detail.reason : undefined }
		);
	}
	return resp.json();
}


export type GlobalSearchHit = SearchHit & {
	/** Source namespace: the index-file slug of the tag the segment
	 * was indexed under (spaces → dashes, lowercased). */
	tag: string;
};

export type GlobalSearchResponse = {
	query: string;
	k: number;
	hits: GlobalSearchHit[];
};

/** Phase 3.75: global cross-tag semantic search — KNN merged across
 * every per-tag index. Same 503 {available: false} shape as searchTag
 * when the backend is down or nothing is indexed at all. */
export async function fetchGlobalSearch(
	cfg: ApiConfig,
	q: string,
	k = 20
): Promise<GlobalSearchResponse> {
	const resp = await req(cfg, `/search?q=${encodeURIComponent(q)}&k=${k}`);
	if (!resp.ok) {
		const detail = (await resp.json().catch(() => null))?.detail as SearchUnavailable | string | null;
		throw Object.assign(
			new Error(typeof detail === 'string' ? detail : (detail?.reason ?? `search ${resp.status}`)),
			{ status: resp.status, reason: typeof detail === 'object' && detail ? detail.reason : undefined }
		);
	}
	return resp.json();
}

export async function getRecording(cfg: ApiConfig, id: string): Promise<Recording> {
	const resp = await req(cfg, `/recordings/${id}`);
	if (!resp.ok) throw Object.assign(new Error(`get ${resp.status}`), { status: resp.status });
	return resp.json();
}

export async function updateRecording(
	cfg: ApiConfig,
	id: string,
	patch: UpdateRecordingPatch
): Promise<Recording> {
	const resp = await req(cfg, `/recordings/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(patch)
	});
	if (!resp.ok) {
		const detail = await resp.json().catch(() => ({ detail: resp.status }));
		throw new Error(detail.detail ?? `update ${resp.status}`);
	}
	return resp.json();
}

export function audioUrl(cfg: ApiConfig, id: string): string {
	return `${cfg.baseUrl.replace(/\/$/, '')}/recordings/${id}/audio?token=${encodeURIComponent(cfg.token)}`;
}

export async function regenerate(
	cfg: ApiConfig,
	id: string,
	stage: string
): Promise<void> {
	const resp = await req(cfg, `/recordings/${id}/regenerate`, {
		method: 'POST',
		body: JSON.stringify({ stage })
	});
	if (!resp.ok) {
		const detail = await resp.json().catch(() => ({ detail: resp.status }));
		throw new Error(detail.detail ?? `regenerate ${resp.status}`);
	}
}

export async function fetchArtifact(
	cfg: ApiConfig,
	id: string,
	stage: string,
	file?: string
): Promise<string> {
	const q = file ? `?file=${encodeURIComponent(file)}` : '';
	const resp = await req(cfg, `/recordings/${id}/artifacts/${stage}${q}`);
	if (!resp.ok) throw Object.assign(new Error(`artifact ${resp.status}`), { status: resp.status });
	return resp.text();
}
export async function deleteRecording(cfg: ApiConfig, id: string): Promise<void> {
	const resp = await req(cfg, `/recordings/${id}`, { method: 'DELETE' });
	if (!resp.ok) {
		const detail = await resp.json().catch(() => ({ detail: resp.status }));
		throw Object.assign(new Error(detail.detail ?? `delete ${resp.status}`), { status: resp.status });
	}
}

/**
 * Upload a single audio blob through the REST multipart endpoint
 * POST /recordings/direct (mobile webview capture -> WebM/Opus, import
 * surface -> FLAC/WAV/MP3). The server transcodes to canonical FLAC and
 * seeds the standard pipeline.
 *
 * `opts` carries the Phase-0 meta fields with their exact wire names:
 * `type` is the pipeline-type slug, `recordedAt` (client-side camelCase)
 * maps to the `recorded_at` ISO-8601 multipart field (import backdate).
 * Both are optional; absent fields are simply not sent.
 *
 * Tags and duration_sec mirror the desktop JSON create path (server
 * normalizes tags the same way); title and duration_sec are optional in
 * the multipart body but the server tolerates them being absent.
 */
export type UploadDirectOpts = {
	/** Pipeline type slug (`meeting`, `ttrpg`…). */
	type?: string;
	/** ISO-8601 "when it actually sounded" backdate. */
	recordedAt?: string;
};

export async function uploadDirect(
	cfg: ApiConfig,
	file: Blob,
	title: string | null,
	tags: string[] = [],
	durationSec: number | null = null,
	opts: UploadDirectOpts = {}
): Promise<{ id: string }> {
	const form = new FormData();
	form.append('file', file, filenameForBlob(file));
	if (title && title.trim().length > 0) form.append('title', title.trim());
	if (tags.length > 0) form.append('tags', JSON.stringify(tags));
	if (durationSec !== null && Number.isFinite(durationSec)) {
		form.append('duration_sec', durationSec.toFixed(3));
	}
	if (opts.type) form.append('type', opts.type);
	if (opts.recordedAt) form.append('recorded_at', opts.recordedAt);
	const url = `${cfg.baseUrl.replace(/\/$/, '')}/recordings/direct`;
	const resp = await fetch(url, {
		method: 'POST',
		headers: {
			authorization: `Bearer ${cfg.token}`
			// Intentionally NO content-type: the browser sets
			// multipart/form-data; boundary=... when the body is a FormData.
		},
		body: form
	});
	if (resp.status === 401) {
		throw Object.assign(new Error('unauthorized: check token in Settings'), { status: 401 });
	}
	if (!resp.ok) {
		const detail = await resp.json().catch(() => ({ detail: resp.status }));
		throw new Error(detail.detail ?? `direct upload ${resp.status}`);
	}
	return resp.json();
}

/** Pick a sensible filename for the multipart "file" part. Server-side
 * debug logs are easier to read with a real extension, and any content-type
 * sniffer (FFmpeg auto-detect, magic-byte scan) prefers named files. */
function filenameForBlob(blob: Blob): string {
	const mime = blob.type.toLowerCase();
	if (mime.includes('webm')) return 'recording.webm';
	if (mime.includes('ogg')) return 'recording.ogg';
	if (mime.includes('mp4') || mime.includes('m4a') || mime.includes('aac')) return 'recording.m4a';
	if (mime.includes('flac')) return 'recording.flac';
	if (mime.includes('wav')) return 'recording.wav';
	return 'recording.bin';
}
