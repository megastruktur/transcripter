import { commands, type PreFlightReport } from '$lib/tauri';

export type UploadState = {
	sessionId: string;
	committed: number;
	total: number;
};

// UI state shared across pages (Svelte 5 runes).
export const recorder = $state({
	recording: false,
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
	pumpTimer = setInterval(async () => {
		recorder.frames += await commands.pump();
	}, 500);
}

export async function stopRecording(): Promise<void> {
	if (pumpTimer) {
		clearInterval(pumpTimer);
		pumpTimer = null;
	}
	await commands.stopRecording();
	recorder.recording = false;
	// Uploader service (T9 rust side) picks up the spool entry on its loop.
}
