export type ApiConfig = {
	baseUrl: string;
	token: string;
};

export type StageStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped';

export type Stage = {
	kind: 'chunk' | 'transcribe' | 'diarize' | 'merge_speakers' | 'summarize';
	status: StageStatus;
	attempts: number;
	last_error: string | null;
	updated_at: string;
};

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
			...(init?.headers ?? {})
		}
	});
	if (resp.status === 401) throw Object.assign(new Error('unauthorized: check token in Settings'), { status: 401 });
	return resp;
}

export async function testConnection(cfg: ApiConfig): Promise<string> {
	const base = cfg.baseUrl.replace(/\/$/, '');
	const health = await fetch(`${base}/health`);
	if (!health.ok) throw new Error(`health ${health.status}`);
	const authed = await fetch(`${base}/recordings`, {
		headers: { authorization: `Bearer ${cfg.token}` }
	});
	if (authed.status === 401) throw new Error('unauthorized: wrong token');
	if (!authed.ok) throw new Error(`recordings ${authed.status}`);
	return 'ok';
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

/** Per-tag digest note (markdown with frontmatter) produced by the enrich
 * workflow. Throws with .status 404 when the digest is not generated yet. */
export async function fetchDigest(cfg: ApiConfig, tag: string): Promise<string> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/digest`);
	if (!resp.ok) throw Object.assign(new Error(`digest ${resp.status}`), { status: resp.status });
	return resp.text();
}

/** Trigger digest (re)generation: the server replies 202 and runs the
 * workflow asynchronously — poll fetchDigest until the note appears. */
export async function regenerateDigest(cfg: ApiConfig, tag: string): Promise<void> {
	const resp = await req(cfg, `/tags/${encodeURIComponent(tag)}/digest`, { method: 'POST' });
	if (!resp.ok) {
		const detail = await resp.json().catch(() => ({ detail: resp.status }));
		throw new Error(detail.detail ?? `digest ${resp.status}`);
	}
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
