import { commands } from '$lib/tauri';
import type { AudioDevices, PreFlightReport } from '$lib/tauri';
import { loadApiConfig, saveApiConfig, testConnection } from '$lib/api.svelte';
import type { ApiConfig, Stage } from '$lib/api.svelte';
import { listen } from '@tauri-apps/api/event';
import { ANDROID_MIC_ID, isAndroidTauri } from '$lib/mobile-recorder';

export type UploadState = {
	sessionId: string;
	title: string;
	state: 'queued' | 'uploading' | 'done' | 'failed';
	committed: number;
	total: number;
	error: string | null;
}

// UI state shared across pages (Svelte 5 runes).
export const recorder = $state({
	recording: false,
	stopping: false,
	sessionId: '',
	frames: 0,
	warnings: [] as string[]
});

export const preflight = $state<{ current: PreFlightReport | null }>({ current: null });

export type ArtifactTabKey = 'transcript' | 'speakers' | 'events' | 'summary' | 'json';

/** Active artifact tab on the recording detail page. The rail tab buttons
 * (layout) write this; the detail page reads it and loads the artifact. */
export const artifactTab = $state<{ active: ArtifactTabKey }>({ active: 'transcript' });

/** Canonical pipeline stage order and display names. Shared by the detail
 * page (stage chips, error lines) and the layout context-bar (re-run cluster). */
export const STAGES = ['chunk', 'transcribe', 'diarize', 'merge_speakers', 'summarize', 'enrich'] as const;
export type StageKind = (typeof STAGES)[number];
export const stageNames: Record<StageKind, string> = {
	chunk: 'Chunks',
	transcribe: 'Transcript',
	diarize: 'Diarize',
	merge_speakers: 'Speakers',
	summarize: 'Summary',
	enrich: 'Enrich'
};
/** Short labels for the compact re-run buttons in the context bar; the full
 * stageNames go into tooltips and accessible names. */
export const stageShortNames: Record<StageKind, string> = {
	chunk: 'Chk',
	transcribe: 'Trn',
	diarize: 'Dia',
	merge_speakers: 'Spk',
	summarize: 'Sum',
	enrich: 'Enr'
};

/** Re-run context published by the recording detail page. The layout
 * context-bar reads it to render per-stage re-run buttons above the title. */
export const stageRetry = $state<{
	stages: Stage[];
	enabled: boolean;
	rerun: ((kind: StageKind) => void) | null;
}>({ stages: [], enabled: false, rerun: null });

/** Live upload ledger: session id → current upload state. */
export const uploads = $state<{ [id: string]: UploadState }>({});

type UploadStatusEvent = {
	session_id: string;
	title: string;
	state: 'queued' | 'uploading' | 'done' | 'failed';
	committed: number;
	total: number;
	error: string | null;
};
/** Seed the ledger from the spool (startup) and subscribe to Rust events. */
export async function initUploadTracking(): Promise<void> {
	if (uploadEventsBound) return;
	const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
	if (!isTauri) return;
	uploadEventsBound = true;
	await listen<UploadStatusEvent>('upload://status', (event) => {
		const p = event.payload;
		uploads[p.session_id] = {
			sessionId: p.session_id,
			title: p.title,
			state: p.state,
			committed: p.committed,
			total: p.total,
			error: p.error
		};
	});
	try {
		for (const s of await commands.pendingUploads()) {
			if (!uploads[s.id]) {
				uploads[s.id] = {
					sessionId: s.id,
					title: s.title,
					state: 'queued',
					committed: s.uploaded_offset,
					total: 0,
					error: null
				};
			}
		}
	} catch {
		// Spool read failed; events will still drive the UI once uploads run.
	}
	// "done" entries only need a short-lived confirmation; drop them so the
	// footer returns to a quiet state.
	if (Object.keys(uploads).length) {
		setTimeout(pruneDoneUploads, 8000);
	}
}

let uploadEventsBound = false;

function pruneDoneUploads(): void {
	for (const id of Object.keys(uploads)) {
		if (uploads[id].state === 'done') delete uploads[id];
	}
}

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
	checkSystem: boolean,
	probe = false
): Promise<PreFlightReport> {
	const report = await commands.preFlight(probe, microphone, systemOutput, checkSystem);
	preflight.current = report;
	// A published report always carries its selection key, no matter which
	// entry point produced it — remounts compare keys to decide whether a
	// fresh check is due, and a keyless report would force a redundant one.
	preflightSelectionKey = selectionKey(microphone ?? '', checkSystem ? (systemOutput ?? '') : SYSTEM_AUDIO_OFF);
	return report;
}

export const SYSTEM_AUDIO_OFF = '__off__';

/**
 * Audio device cache shared across page mounts. Enumerating CoreAudio and
 * running the availability check takes 1-2s on macOS, so a page
 * remount (tab switch, window expand) renders from this cache instantly and
 * refreshes silently in the background instead of blocking on invoke calls.
 */
export const audioDevices = $state({
	devices: { microphones: [], system_outputs: [], default_microphone: null, default_system_output: null } as AudioDevices,
	// True only until the first enumeration settles; remounts never see it.
	loading: true,
	checking: false,
	error: '',
	selectedMicrophone: '',
	selectedSystemOutput: SYSTEM_AUDIO_OFF
});

let audioDevicesRequest: Promise<void> | null = null;
let audioSelectionInitialized = false;
// True while the effective selection diverges from the saved preference
// (hot-unplug fallback): the saved device is restored the moment it
// reappears, however many enumerations pass while it stays unplugged.
let selectionIsFallback = false;
// Selection the current preflight report belongs to ('' = no valid report).
let preflightSelectionKey = '';

function selectionKey(microphone: string, systemOutput: string): string {
	const withSystem = systemOutput !== SYSTEM_AUDIO_OFF;
	return `${microphone} ${withSystem ? systemOutput : ''} ${withSystem}`;
}

/** Enumerate devices; deduped. Never re-blocks the UI after the first load. */
export function refreshAudioDevices(): Promise<void> {
	if (audioDevicesRequest) return audioDevicesRequest;
	audioDevicesRequest = (async () => {
		try {
			if (isAndroidTauri()) {
				// Android owns device routing: recording goes through the system mic
				// via getUserMedia (mobile-recorder.ts), and the desktop
				// cmd_list_audio_devices command is not even registered in the
				// android build. Report a single pseudo device so every consumer
				// (record page, settings) sees a valid, selected microphone.
				audioDevices.devices = {
					microphones: [{ id: ANDROID_MIC_ID, label: 'System microphone', is_default: true }],
					system_outputs: [],
					default_microphone: ANDROID_MIC_ID,
					default_system_output: null
				};
				audioDevices.error = '';
				audioDevices.selectedMicrophone = ANDROID_MIC_ID;
				audioDevices.selectedSystemOutput = SYSTEM_AUDIO_OFF;
				return;
			}
			const devices = await commands.listAudioDevices();
			audioDevices.devices = devices;
			audioDevices.error = '';
			const keyBefore = selectionKey(audioDevices.selectedMicrophone, audioDevices.selectedSystemOutput);
			const savedMicrophone = localStorage.getItem('transcripter.microphone');
			const savedSystemOutput = localStorage.getItem('transcripter.system-output');
			if (!audioSelectionInitialized) {
				audioSelectionInitialized = true;
				audioDevices.selectedMicrophone = savedMicrophone ?? '';
				audioDevices.selectedSystemOutput = savedSystemOutput ?? SYSTEM_AUDIO_OFF;
			} else if (selectionIsFallback) {
				// The fallback is ephemeral and never persisted: prefer the user's
				// saved choice the moment it reappears in an enumeration (replug).
				if (savedMicrophone && devices.microphones.some((d) => d.id === savedMicrophone)) {
					audioDevices.selectedMicrophone = savedMicrophone;
				}
				if (savedSystemOutput && (savedSystemOutput === SYSTEM_AUDIO_OFF || devices.system_outputs.some((d) => d.id === savedSystemOutput))) {
					audioDevices.selectedSystemOutput = savedSystemOutput;
				}
			}
			// Validate the selection against the fresh list; fall back only when
			// the chosen device disappeared (hot-unplug). A rewritten selection
			// invalidates the report — it was computed for the vanished device.
			if (!devices.microphones.some((d) => d.id === audioDevices.selectedMicrophone)) {
				audioDevices.selectedMicrophone = devices.default_microphone ?? devices.microphones[0]?.id ?? '';
			}
			if (
				audioDevices.selectedSystemOutput !== SYSTEM_AUDIO_OFF &&
				!devices.system_outputs.some((d) => d.id === audioDevices.selectedSystemOutput)
			) {
				audioDevices.selectedSystemOutput = devices.default_system_output ?? devices.system_outputs[0]?.id ?? SYSTEM_AUDIO_OFF;
			}
			// The flag derives from DIVERGENCE from the saved preference, not from
			// this enumeration's rewrite — otherwise a second mount while still
			// unplugged would clear it (the fallback device IS present in the
			// list) and kill the restore path before the replug arrives.
			const savedMic = savedMicrophone ?? '';
			const savedSys = savedSystemOutput ?? SYSTEM_AUDIO_OFF;
			selectionIsFallback =
				(savedMic !== '' || savedSys !== SYSTEM_AUDIO_OFF) &&
				(audioDevices.selectedMicrophone !== savedMic || audioDevices.selectedSystemOutput !== savedSys);
			if (selectionKey(audioDevices.selectedMicrophone, audioDevices.selectedSystemOutput) !== keyBefore) {
				preflight.current = null;
				preflightSelectionKey = '';
			}
		} catch (error) {
			audioDevices.error = String(error);
		} finally {
			audioDevices.loading = false;
			audioDevicesRequest = null;
		}
	})();
	return audioDevicesRequest;
}

/**
 * Apply an explicit user selection and re-check availability (device switch
 * moment). Patch-shaped: only the dimension the user actually touched is
 * persisted — persisting the untouched one would clobber the saved preference
 * with a hot-unplug fallback value and defeat the replug-restore. The
 * fallback flag is deliberately NOT cleared here: refreshAudioDevices
 * recomputes it from divergence, so an untouched fallback dimension keeps
 * its restore path.
 */
export function selectAudioDevices(patch: { microphone?: string; systemOutput?: string }): void {
	if (patch.microphone !== undefined) {
		audioDevices.selectedMicrophone = patch.microphone;
		localStorage.setItem('transcripter.microphone', patch.microphone);
	}
	if (patch.systemOutput !== undefined) {
		audioDevices.selectedSystemOutput = patch.systemOutput;
		localStorage.setItem('transcripter.system-output', patch.systemOutput);
	}
	preflight.current = null;
	preflightSelectionKey = '';
	void checkAudioDevices(false);
}

/**
 * Page-mount entry point (Record and Settings). Instant when the cache is warm: the list is
 * re-enumerated in the background and the availability check runs only when
 * there is no report for the current selection (startup, or the selection
 * changed underneath us — the two sanctioned check moments).
 */
export async function ensureAudioDevices(): Promise<void> {
	await refreshAudioDevices();
	if (!audioDevices.selectedMicrophone) return;
	if (preflightSelectionKey === selectionKey(audioDevices.selectedMicrophone, audioDevices.selectedSystemOutput)) return;
	await checkAudioDevices(false);
	// macOS first run: the mic permission prompt only appears when an input
	// stream is opened, so probe once while undetermined.
	if (preflight.current?.mic_permission === 'not_determined' && audioDevices.selectedMicrophone) {
		await checkAudioDevices(true);
	}
}

let audioCheckSeq = 0;
let audioChecksInFlight = 0;

export async function checkAudioDevices(probe = true): Promise<void> {
	// No preflight on Android: the capture stack is the WebView's, and the
	// only check that matters (mic permission) happens at record start.
	if (isAndroidTauri()) return;
	const microphone = audioDevices.selectedMicrophone;
	const systemOutput = audioDevices.selectedSystemOutput;
	if (!microphone) {
		recorder.warnings.push('no microphone available');
		return;
	}
	// In-flight guard: a device switch fires a check and resets the key, and a
	// concurrent remount (ensureAudioDevices) can fire another. Only the newest
	// check may publish its report — a stale completion must not pin a status
	// computed for a device that is no longer selected.
	const seq = ++audioCheckSeq;
	audioChecksInFlight += 1;
	audioDevices.checking = true;
	if (probe) clearWarnings();
	try {
		const withSystem = systemOutput !== SYSTEM_AUDIO_OFF;
		const report = await commands.preFlight(probe, microphone, withSystem ? systemOutput : null, withSystem);
		if (seq !== audioCheckSeq || audioDevices.selectedMicrophone !== microphone || audioDevices.selectedSystemOutput !== systemOutput) return;
		preflight.current = report;
		preflightSelectionKey = selectionKey(microphone, systemOutput);
	} catch (error) {
		// Same staleness rule as the success path: a hot-unplug rewrite changes
		// the selection without bumping the seq, and the vanished device's
		// error must not surface either.
		if (seq === audioCheckSeq && audioDevices.selectedMicrophone === microphone && audioDevices.selectedSystemOutput === systemOutput) {
			recorder.warnings.push(String(error));
		}
	} finally {
		// `checking` tracks ANY outstanding probe: a discarded stale check
		// still holds its audio stream, so the button stays busy until the
		// last in-flight invoke settles.
		audioChecksInFlight -= 1;
		if (audioChecksInFlight === 0) audioDevices.checking = false;
	}
}

export async function startRecording(
	title: string,
	tags: string[],
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
	if (captureSystem && ['permission_denied', 'unavailable', 'failed'].includes(report.system_state)) {
		recorder.warnings.push(report.error ?? 'system audio unavailable');
		return;
	}
	recorder.sessionId = await commands.startRecording(title || null, tags, microphone, systemOutput, captureSystem);
	recorder.recording = true;
	recorder.frames = 0;
	recordingStatusTimer = globalThis.setInterval(async () => {
		try {
			recorder.frames = await commands.recordingFrames();
			const degraded = await commands.recordingDegraded();
			if (degraded && !recorder.warnings.includes(degraded)) {
				recorder.warnings.push(`${degraded} — recording continues on microphone`);
			}
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
