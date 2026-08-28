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
	stages: Stage[];
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
 * Upload a single captured audio blob directly through the REST multipart
 * endpoint used by the Android capture path (mobile webview -> WebM/Opus ->
 * POST /recordings/direct). The server handles transcoding to canonical
 * FLAC and seeds the standard pipeline (same as a desktop recording finalize).
 *
 * `onProgress` is optional: a single indeterminate tick after the body is
 * sent is enough for the PoC UI; per-byte progress on a single multipart
 * POST would need the fetch-stream ReadableStream tee trick and is out of
 * scope for this gate. The hook exists so callers can wire richer progress
 * later without another signature break.
 *
 * Tags and duration_sec mirror the desktop JSON create path (server
 * normalizes tags the same way); title and duration_sec are optional in the
 * multipart body but the server treats absent title as empty and absent
 * duration_sec as null (both are tolerated).
 */
export async function uploadDirect(
	cfg: ApiConfig,
	file: Blob,
	title: string | null,
	tags: string[] = [],
	durationSec: number | null = null,
	onProgress?: (deltaBytes: number) => void
): Promise<{ id: string }> {
	const form = new FormData();
	form.append('file', file, filenameForBlob(file));
	if (title && title.trim().length > 0) form.append('title', title.trim());
	if (tags.length > 0) form.append('tags', JSON.stringify(tags));
	if (durationSec !== null && Number.isFinite(durationSec)) {
		form.append('duration_sec', durationSec.toFixed(3));
	}

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
	// Indeterminate progress tick after the body has been sent. Real per-byte
	// progress is out of scope for the PoC (see docstring above).
	onProgress?.(file.size);

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
