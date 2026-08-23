import { commands, type PreFlightReport } from '$lib/tauri';
import { loadApiConfig, saveApiConfig, testConnection, type ApiConfig } from '$lib/api.svelte';

export type UploadState = {
	sessionId: string;
	committed: number;
	total: number;
};

// UI state shared across pages (Svelte 5 runes).
export const recorder = $state({
	recording: false,
	stopping: false,
	sessionId: '',
	frames: 0,
	warnings: [] as string[]
});

export const preflight = $state<{ current: PreFlightReport | null }>({ current: null });

export const uploads = $state<{ [id: string]: UploadState }>({});

export type ConnectionPhase = 'unconfigured' | 'checking' | 'connected' | 'unavailable';

export const connection = $state<{
	phase: ConnectionPhase;
	detail: string;
}>({
	phase: 'unconfigured',
	detail: 'Add a bearer token in Settings.'
});

const CONNECTION_TIMEOUT_MS = 5000;
let connectionCheck: Promise<boolean> | null = null;
let connectionKey = '';

function normalizedApiConfig(cfg: ApiConfig): ApiConfig {
	return {
		baseUrl: cfg.baseUrl.trim().replace(/\/+$/, ''),
		token: cfg.token.trim()
	};
}

export async function checkServerConnection(
	input: ApiConfig = loadApiConfig(),
	persist = false
): Promise<boolean> {
	const cfg = normalizedApiConfig(input);
	const key = `${cfg.baseUrl}\u0000${cfg.token}`;

	while (connectionCheck) {
		if (connectionKey === key) return connectionCheck;
		await connectionCheck;
	}

	if (!cfg.token) {
		connection.phase = 'unconfigured';
		connection.detail = 'Add a bearer token in Settings.';
		return false;
	}

	try {
		const url = new URL(cfg.baseUrl);
		if (url.protocol !== 'http:') {
			connection.phase = 'unavailable';
			connection.detail = 'HTTPS is unsupported by the uploader in this build. Use an HTTP address on your trusted LAN.';
			return false;
		}
	} catch {
		connection.phase = 'unavailable';
		connection.detail = 'Invalid server address.';
		return false;
	}

	connection.phase = 'checking';
	connection.detail = 'Checking health and authorization…';
	connectionKey = key;

	let timeoutId: ReturnType<typeof globalThis.setTimeout>;
	let task!: Promise<boolean>;
	task = Promise.race([
		testConnection(cfg),
		new Promise<never>((_, reject) => {
			timeoutId = globalThis.setTimeout(() => reject(new Error('Connection timed out.')), CONNECTION_TIMEOUT_MS);
		})
	])
		.then(() => {
			if (persist) saveApiConfig(cfg);
			connection.phase = 'connected';
			connection.detail = 'Health and authorization verified.';
			return true;
		})
		.catch((error: unknown) => {
			connection.phase = 'unavailable';
			connection.detail = error instanceof TypeError ? 'Could not reach the server.' : error instanceof Error ? error.message : String(error);
			return false;
		})
		.finally(() => {
			globalThis.clearTimeout(timeoutId);
			if (connectionCheck === task) {
				connectionCheck = null;
				connectionKey = '';
			}
		});

	connectionCheck = task;
	return task;
}

let recordingStatusTimer: number | null = null;

export function clearWarnings(): void {
	recorder.warnings = [];
}

export async function checkAudio(
	microphone: string | null,
	systemOutput: string | null,
	checkSystem: boolean
): Promise<PreFlightReport> {
	const report = await commands.preFlight(true, microphone, systemOutput, checkSystem);
	preflight.current = report;
	return report;
}

export async function startRecording(
	title: string,
	microphone: string | null,
	systemOutput: string | null,
	captureSystem: boolean
): Promise<void> {
	const report = await checkAudio(microphone, systemOutput, captureSystem);
	if (report.mic_state === 'permission_denied') {
		const hint = navigator.userAgent.includes('Windows')
			? 'microphone blocked — enable it in Windows Settings → Privacy & security → Microphone (Let desktop apps access your microphone)'
			: 'microphone blocked — enable it in System Settings → Privacy & Security → Microphone';
		recorder.warnings.push(hint);
		return;
	}
	if (report.error || report.mic_state === 'unavailable' || report.mic_state === 'failed') {
		recorder.warnings.push(report.error ?? 'microphone unavailable');
		return;
	}
	if (report.mic_state === 'silent') {
		// Non-fatal: an open-but-quiet mic still delivers frames; the stall
		// detector in the recorder watches delivery, not signal level.
		recorder.warnings.push('mic is quiet — recording will start anyway; speak or check the input level (Windows: Settings → System → Sound → Input)');
	}
	if (captureSystem && ['permission_denied', 'unavailable', 'failed'].includes(report.system_state)) {
		recorder.warnings.push(report.error ?? 'system audio unavailable');
		return;
	}
	if (captureSystem && report.system_state === 'silent') {
		recorder.warnings.push('system audio is connected but no sound is playing yet');
	}
	recorder.sessionId = await commands.startRecording(title || null, microphone, systemOutput, captureSystem);
	recorder.recording = true;
	recorder.frames = 0;
	recordingStatusTimer = globalThis.setInterval(async () => {
		try {
			recorder.frames = await commands.recordingFrames();
		} catch (error) {
			if (recorder.stopping || !recorder.recording) return;
			if (recordingStatusTimer) {
				clearInterval(recordingStatusTimer);
				recordingStatusTimer = null;
			}
			recorder.warnings.push(String(error));
			void stopRecording();
		}
	}, 500);
}

export async function stopRecording(): Promise<void> {
	if (recorder.stopping) return;
	recorder.stopping = true;
	const cfg = loadApiConfig();
	try {
		const session = await commands.stopRecording(
			cfg.baseUrl || null,
			cfg.token || null
		);
		if (recordingStatusTimer) {
			clearInterval(recordingStatusTimer);
			recordingStatusTimer = null;
		}
		recorder.recording = false;
		if (cfg.baseUrl && cfg.token) {
			recorder.warnings.push(`recording queued for upload (${session.id.slice(0, 8)}…)`);
		} else {
			recorder.warnings.push('no server configured — recording saved locally in spool');
		}
	} catch (e) {
		const msg = String(e);
		if (msg.startsWith('FATAL_STOP')) {
			if (recordingStatusTimer) {
				clearInterval(recordingStatusTimer);
				recordingStatusTimer = null;
			}
			recorder.recording = false;
			recorder.warnings.push(`recording lost: ${msg.replace('FATAL_STOP: ', '')}`);
		} else {
			// Retryable: Rust session is still live — keep draining and
			// stay in recording state so Stop can be retried.
			recorder.warnings.push(`stop failed (retry Stop): ${msg}`);
		}
	} finally {
		recorder.stopping = false;
	}
}

/** Retry pending spool uploads (server-side scan + enqueue). */
export async function retryPendingUploads(): Promise<number> {
	const cfg = loadApiConfig();
	if (!cfg.baseUrl || !cfg.token) return 0;
	return commands.retryPending(cfg.baseUrl, cfg.token);
}
