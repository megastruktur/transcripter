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
	state: 'uploading' | 'processing' | 'done' | 'failed';
	committed_bytes: number;
	total_bytes: number | null;
	duration_sec: number | null;
	created_at: string;
	stages: Stage[];
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
	if (resp.status === 401) throw new Error('unauthorized: check token in Settings');
	return resp;
}

export async function testConnection(cfg: ApiConfig): Promise<string> {
	const base = cfg.baseUrl.replace(/\/$/, '');
	const health = await fetch(`${base}/health`);
	if (!health.ok) throw new Error(`health ${health.status}`);
	// Token check: /health is public, so probe an authed endpoint too.
	const authed = await fetch(`${base}/recordings`, {
		headers: { authorization: `Bearer ${cfg.token}` }
	});
	if (authed.status === 401) throw new Error('unauthorized: wrong token');
	if (!authed.ok) throw new Error(`recordings ${authed.status}`);
	return 'ok';
}

export async function listRecordings(cfg: ApiConfig): Promise<Recording[]> {
	const resp = await req(cfg, '/recordings');
	if (!resp.ok) throw new Error(`list ${resp.status}`);
	return resp.json();
}

export async function getRecording(cfg: ApiConfig, id: string): Promise<Recording> {
	const resp = await req(cfg, `/recordings/${id}`);
	if (!resp.ok) throw new Error(`get ${resp.status}`);
	return resp.json();
}

export async function renameRecording(cfg: ApiConfig, id: string, title: string): Promise<Recording> {
	const resp = await req(cfg, `/recordings/${id}`, {
		method: 'PATCH',
		body: JSON.stringify({ title })
	});
	if (!resp.ok) {
		const detail = await resp.json().catch(() => ({ detail: resp.status }));
		throw new Error(detail.detail ?? `rename ${resp.status}`);
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
	if (!resp.ok) throw new Error(`artifact ${resp.status}`);
	return resp.text();
}
