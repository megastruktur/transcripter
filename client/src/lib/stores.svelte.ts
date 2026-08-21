import { commands, type PreFlightReport } from '$lib/tauri';
import { loadApiConfig } from '$lib/api.svelte';

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

export const settings = $state({ baseUrl: '', token: '' });

let pumpTimer: ReturnType<typeof globalThis.setInterval> | null = null;

export async function startRecording(title: string): Promise<void> {
	const report = await commands.preFlight(true);
	preflight.current = report;
	if (!report.mic_device_present || report.error) {
		recorder.warnings.push(report.error ?? 'no microphone available');
		return;
	}
	if (report.mic_signal === false) {
		recorder.warnings.push('no mic signal detected (check input device/mute)');
		return;
	}
	if (!report.system_device_present) {
		recorder.warnings.push('system audio unavailable — recording mic only');
	}
	recorder.sessionId = await commands.startRecording(title || null);
	recorder.recording = true;
	recorder.frames = 0;
	const myTimer: ReturnType<typeof globalThis.setInterval> = setInterval(async () => {
		if (myTimer !== pumpTimer) return; // superseded by a new recording
		try {
			recorder.frames += await commands.pump();
		} catch (e) {
			if (String(e).includes('no active session')) {
				// Stop raced us: genuinely no session left to drain.
				clearInterval(myTimer);
				if (pumpTimer === myTimer) pumpTimer = null;
			} else {
				// Transient pump error (disk, writer): keep draining.
				recorder.warnings.push(`pump error: ${e}`);
			}
		}
	}, 500);
	pumpTimer = myTimer;
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
		// Confirmed stop: stop draining and reset state.
		if (pumpTimer) {
			clearInterval(pumpTimer);
			pumpTimer = null;
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
			// Writer consumed, recording unrecoverable: reset to idle.
			if (pumpTimer) {
				clearInterval(pumpTimer);
				pumpTimer = null;
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
