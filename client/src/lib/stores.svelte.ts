import { commands } from '$lib/tauri';
import type { AudioDevices, PreFlightReport } from '$lib/tauri';
import { getRecording, loadApiConfig, saveApiConfig, testConnection, uploadDirect } from '$lib/api.svelte';
import type { ApiConfig, Stage } from '$lib/api.svelte';
import { listen } from '@tauri-apps/api/event';
import { ANDROID_MIC_ID, isAndroidTauri, startMobileRecorder, startRecordingKeepalive, stopRecordingKeepalive } from '$lib/mobile-recorder';
import type { MobileRecorder } from '$lib/mobile-recorder';
import { buildRecordingTitle, getActivePreset } from '$lib/presets.svelte';
import { mergeDraftTags } from '$lib/tags';
import type { IconName } from '$lib/Icon.svelte';
import { setMascotState } from '@orbitkit/ui';

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
	/** Start transition in flight (device probes, mic prompt, FGS raise).
	 * The Record page ORs it with its local `starting` for the button. */
	busy: false,
	sessionId: '',
	frames: 0,
	warnings: [] as string[]
});

export const preflight = $state<{ current: PreFlightReport | null }>({ current: null });
/** Capture-form draft (name, type, tags). The Record page binds its fields
 * here instead of local $state: collapsing the window to the mark unmounts
 * the page, and — like the recorder session itself — the draft must survive
 * the round-trip. */
export const captureDraft = $state({
	title: '',
	tags: [] as string[],
	tagDraft: '',
	recType: null as string | null
});

// ── Android capture session ────────────────────────────────────────────────
// Lives at module scope ON PURPOSE: the Record page unmounts on every
// navigation/window collapse, and a recording must survive that (the old
// component-owned handle cancelled the MediaRecorder on page leave — the
// bug this card fixes). The desktop session lives Rust-side and always
// survived; now both do.
/** Mirrors CAPTURE_RATE in src-tauri/src/capture.rs: recorder.frames is a
 * written-frame count, so elapsed = floor(frames / CAPTURE_RATE). */
const CAPTURE_RATE = 48_000;
let mobileInstance: MobileRecorder | null = null;
let mobileFramesTimer: number | null = null;
let mobileStartedAt = 0;
let mobileMeta: { title: string; tags: string[]; recType: string | null } = {
	title: '',
	tags: [],
	recType: null
};

/** Android takes that finished recording but have not (yet) reached the
 * server: uploaded in the background, retried or discarded by hand. Kept
 * in the shared store so a failure is visible from Library / the Record
 * banner and NEVER lost by leaving a page (the desktop path has the
 * on-disk spool for this; Card 3 adds IndexedDB persistence here). */
export type MobilePendingUpload = {
	id: string;
	blob: Blob;
	filename: string;
	title: string;
	tags: string[];
	recType: string | null;
	durationSec: number;
	createdAt: number;
	status: 'uploading' | 'failed';
	error: string;
};
export const mobilePendingUploads = $state<MobilePendingUpload[]>([]);

function removeMobilePendingUpload(id: string): void {
	const index = mobilePendingUploads.findIndex((u) => u.id === id);
	if (index !== -1) mobilePendingUploads.splice(index, 1);
}

/** Give up on a failed take and drop its audio. */
export function discardMobileUpload(id: string): void {
	removeMobilePendingUpload(id);
}

/** Retry a failed upload in place (no-op while it is already uploading). */
export async function retryMobileUpload(id: string): Promise<void> {
	const entry = mobilePendingUploads.find((u) => u.id === id);
	if (!entry || entry.status === 'uploading') return;
	entry.status = 'uploading';
	entry.error = '';
	await uploadMobileEntry(entry);
}

export type ArtifactTabKey = 'transcript' | 'speakers' | 'events' | 'summary' | 'json';


/** Canonical pipeline stage order and display names. Shared by the detail
 * page (stage error lines) and the layout context-bar (status icons, re-run menu). */
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
/** Per-stage glyphs for the pipeline status icons in the layout context-bar. */
export const stageIcons: Record<StageKind, IconName> = {
	chunk: 'chunk',
	transcribe: 'transcript',
	diarize: 'diarize',
	merge_speakers: 'speakers',
	summarize: 'summary',
	enrich: 'enrich'
};

/** Re-run context published by the recording detail page. The layout
 * context-bar reads it to render the pipeline status icons next to the
 * title and the re-run entries inside the ellipsis actions menu.
 * `stages` is narrowed to KNOWN pipeline kinds (StageKind): the detail
 * page filters+sorts the API list on publish (a legacy `separate` row —
 * retired stage, tombstone enum on old recordings — must never reach
 * the icon/name maps). */
export type RetryStage = Stage & { kind: StageKind };
export const stageRetry = $state<{
	stages: RetryStage[];
	enabled: boolean;
	rerun: ((kind: StageKind) => void) | null;
}>({ stages: [], enabled: false, rerun: null });

/** Recording-detail actions published to the layout context-bar ellipsis
 * menu. Populated by the detail page once a recording loads; cleared on
 * unmount and on 404 so the menu never shows stale or no-op actions. */
export const recordActions = $state<{
	loaded: boolean;
	deletable: boolean;
	rename: (() => void) | null;
	editDate: (() => void) | null;
	editType: (() => void) | null;
	remove: (() => Promise<void>) | null;
}>({ loaded: false, deletable: false, rename: null, editDate: null, editType: null, remove: null });

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
	/** Server release tag from /health (Settings > Application); empty
	 * until a successful connection check, "dev" for local uvicorn. */
	serverVersion: string;
}>({
	phase: 'unconfigured',
	detail: 'Add a bearer token in Settings.',
	serverVersion: ''
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
		if (url.protocol !== 'http:' && url.protocol !== 'https:') {
			connection.phase = 'unavailable';
			connection.detail = 'Only http:// and https:// server addresses are supported.';
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
		.then((serverVersion) => {
			if (persist) saveApiConfig(cfg);
			connection.phase = 'connected';
			connection.detail = 'Health and authorization verified.';
			connection.serverVersion = serverVersion;
			return true;
		})
		.catch((error: unknown) => {
			connection.phase = 'unavailable';
			connection.detail = error instanceof TypeError ? 'Could not reach the server.' : error instanceof Error ? error.message : String(error);
			connection.serverVersion = '';
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
/** Remove one warning by position (the ✕ on a notice). Warnings duplicate
 * freely, so dismissal is positional, not by value. */
export function dismissWarning(index: number): void {
	if (index >= 0 && index < recorder.warnings.length) recorder.warnings.splice(index, 1);
}
/** Receipt-style messages ("queued for processing …") are info, not signal
 * warnings: they must EXPIRE instead of staying on screen after the fact
 * they report is no longer true (guidelines: instrument, not dashboard).
 * `pushNotice` auto-removes after `ttlMs`; `removeNotice` pulls one early —
 * e.g. when the reported job finishes. */
export function pushNotice(text: string, ttlMs = 30_000): void {
	recorder.warnings.push(text);
	setTimeout(() => removeNotice(text), ttlMs);
}

export function removeNotice(text: string): void {
	const index = recorder.warnings.indexOf(text);
	if (index !== -1) recorder.warnings.splice(index, 1);
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
	// AC2: signal issue state when preflight detects errors or bad audio
	if (report.error || ['silent', 'permission_denied', 'unavailable', 'failed'].includes(report.mic_state) || ['silent', 'permission_denied', 'unavailable', 'failed'].includes(report.system_state)) {
		void setMascotState('issue');
	}
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

// ── Desktop frames poller ──────────────────────────────────────────────────
// Shared by sessions this window started AND sessions adopted from another
// window (quick-record popup): both drain the same Rust backend.
function startFramesPoller(): void {
	if (recordingStatusTimer !== null) return;
	recordingStatusTimer = globalThis.setInterval(async () => {
		try {
			recorder.frames = await commands.recordingFrames();
			const degraded = await commands.recordingDegraded();
			if (degraded && !recorder.warnings.includes(degraded)) {
				recorder.warnings.push(`${degraded} — recording continues on microphone`);
				void setMascotState('issue');
			}
		} catch (error) {
			if (recorder.stopping || !recorder.recording) return;
			if (recordingStatusTimer) {
				clearInterval(recordingStatusTimer);
				recordingStatusTimer = null;
			}
			// The session ended in ANOTHER window (popup Stop): reset quietly
			// instead of demanding a retry that could never succeed.
			if (String(error).includes('no active session')) {
				recorder.recording = false;
				recorder.frames = 0;
				void setMascotState('idle');
				return;
			}
			recorder.warnings.push(String(error));
			void stopRecording();
		}
	}, 500);
}

// ── Android capture (module scope: survives page unmount — THE FIX) ────────
async function startAndroidRecording(title: string, tags: string[], recType: string | null): Promise<void> {
	if (mobileInstance || recorder.busy || recorder.recording || recorder.stopping) return;
	recorder.busy = true;
	clearWarnings();
	let handle: MobileRecorder;
	try {
		handle = startMobileRecorder({});
	} catch (error) {
		recorder.warnings.push(String(error));
		recorder.busy = false;
		return;
	}
	mobileInstance = handle;
	// Gate the "recording" UI on the mic ACTUALLY streaming: a denied
	// system prompt must surface as a warning immediately, not as a
	// running clock that dies on Stop.
	try {
		await handle.ready;
	} catch (error) {
		if (mobileInstance === handle) mobileInstance = null;
		recorder.warnings.push(String(error));
		recorder.busy = false;
		return;
	}
	// Keepalive AFTER the getUserMedia grant, still before the recording UI
	// flips: a mic-type FGS requires RECORD_AUDIO to be ALREADY granted at
	// startForeground time. Fail-loud: without the service a backgrounded
	// recording dies silently.
	try {
		await startRecordingKeepalive();
	} catch (error) {
		if (mobileInstance === handle) mobileInstance = null;
		handle.cancel();
		recorder.warnings.push(`Не удалось запустить фоновую запись: ${String(error)}`);
		recorder.busy = false;
		return;
	}
	// Cancelled while the permission prompt was open — a late grant must
	// NOT flip the shared store or start a ticker nobody will clear.
	if (mobileInstance !== handle) {
		recorder.busy = false;
		return;
	}
	recorder.recording = true;
	recorder.sessionId = `mobile-${Date.now()}`;
	recorder.frames = 0;
	mobileStartedAt = Date.now();
	mobileMeta = { title, tags: [...tags], recType };
	void setMascotState('recording');
	// Drives elapsed = floor(frames / CAPTURE_RATE) exactly like the
	// desktop Rust poller, without a second source of truth.
	mobileFramesTimer = globalThis.setInterval(() => {
		if (!recorder.recording) return;
		recorder.frames = Math.floor(((Date.now() - mobileStartedAt) / 1000) * CAPTURE_RATE);
	}, 250);
	recorder.busy = false;
}

async function stopAndroidRecording(): Promise<void> {
	recorder.stopping = true;
	if (mobileFramesTimer) {
		clearInterval(mobileFramesTimer);
		mobileFramesTimer = null;
	}
	const handle = mobileInstance;
	if (!handle) return;
	mobileInstance = null;
	// The FGS guarded the capture; once the user pressed Stop it must go
	// down regardless of how the MediaRecorder/upload paths end.
	await stopRecordingKeepalive();
	let blob: Blob;
	try {
		blob = await handle.stop();
	} catch (error) {
		recorder.stopping = false;
		recorder.recording = false;
		recorder.sessionId = '';
		recorder.warnings.push(`stop failed: ${String(error)}`);
		return;
	}
	const durationSec = Math.max(0, (Date.now() - mobileStartedAt) / 1000);
	recorder.recording = false;
	recorder.sessionId = '';
	recorder.frames = 0;
	void setMascotState('idle');
	// Queue the upload entry FIRST so the UI resets immediately: the take
	// uploads in the background, and a failure stays retryable (here and in
	// Library) instead of dying with the Record page.
	const entry: MobilePendingUpload = {
		id: newPendingId(),
		blob,
		filename: mobileBlobFilename(blob),
		title: mobileMeta.title,
		tags: [...mobileMeta.tags],
		recType: mobileMeta.recType,
		durationSec,
		createdAt: Date.now(),
		status: 'uploading',
		error: ''
	};
	mobilePendingUploads.push(entry);
	recorder.stopping = false;
	void uploadMobileEntry(entry);
}

function newPendingId(): string {
	return `mobile-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function mobileBlobFilename(blob: Blob): string {
	const ext = blob.type.includes('ogg') ? 'ogg' : blob.type.includes('mp4') ? 'm4a' : 'webm';
	return `recording-${new Date().toISOString().replace(/[:.]/g, '-')}.${ext}`;
}

/** Upload one pending take: success removes it and posts a processing
 * receipt; failure flips the entry to `failed` so Retry stays available
 * and no audio is lost. */
async function uploadMobileEntry(entry: MobilePendingUpload): Promise<void> {
	const cfg = loadApiConfig();
	if (!cfg.baseUrl || !cfg.token) {
		entry.status = 'failed';
		entry.error = 'no server configured — set it in Settings to upload';
		return;
	}
	try {
		const result = await uploadDirect(cfg, entry.blob, entry.title, entry.tags, entry.durationSec, {
			type: entry.recType ?? undefined
		});
		removeMobilePendingUpload(entry.id);
		announceQueued(result.id);
	} catch (error) {
		entry.status = 'failed';
		entry.error = String(error);
	}
}

// ── Processing receipts (moved from the Record page: mobile uploads now
// outlive that page's unmount) ──────────────────────────────────────────────
const processingWatchers = new Set<number>();
const PROCESSING_WATCH_MS = 10 * 60_000;
const PROCESSING_POLL_MS = 10_000;

/** Poll the uploaded recording until the pipeline settles, then swap the
 * stale "queued" receipt for a short-lived outcome receipt. Poll errors
 * (server down, app backgrounded) just keep waiting — the receipt's TTL
 * guarantees it never outlives its truth. */
function watchProcessing(id: string, notice: string): void {
	const cfg = loadApiConfig();
	const shortId = id.slice(0, 8);
	const timer = globalThis.setInterval(async () => {
		try {
			const rec = await getRecording(cfg, id);
			if (rec.state !== 'done' && rec.state !== 'failed') return;
			clearInterval(timer);
			processingWatchers.delete(timer);
			removeNotice(notice);
			pushNotice(
				rec.state === 'done'
					? `recording processed (${shortId}…)`
					: `processing failed (${shortId}…) — see library`,
				20_000
			);
		} catch {
			// transient: keep polling until the notice TTL expires
		}
	}, PROCESSING_POLL_MS);
	processingWatchers.add(timer);
}

/** Receipt for a successful upload: expires on its own after
 * PROCESSING_WATCH_MS, replaced by the outcome receipt as soon as the
 * pipeline reports done/failed. */
function announceQueued(id: string): void {
	const notice = `recording queued for processing (${id.slice(0, 8)}…)`;
	pushNotice(notice, PROCESSING_WATCH_MS);
	watchProcessing(id, notice);
}

export async function startRecording(
	title: string,
	tags: string[],
	microphone: string | null,
	systemOutput: string | null,
	captureSystem: boolean,
	recType: string | null = null
): Promise<void> {
	if (isAndroidTauri()) {
		await startAndroidRecording(title, tags, recType);
		return;
	}
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
	void setMascotState('recording');
	startFramesPoller();
}

export async function stopRecording(): Promise<void> {
	if (recorder.stopping) return;
	// This session consumed the draft overrides: clear them so the next
	// capture starts from the preset, not a stale custom title.
	captureDraft.title = '';
	captureDraft.tagDraft = '';
	if (isAndroidTauri() && mobileInstance) {
		await stopAndroidRecording();
		return;
	}
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
		void setMascotState('idle');
		if (cfg.baseUrl && cfg.token) {
			pushNotice(`recording queued for upload (${session.id.slice(0, 8)}…)`, 60_000);
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
			void setMascotState('idle');
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

/** 1-tap start for the mascot menu and any non-Record surface: the active
 * preset supplies the title stem, type and starter tags; extra chips typed
 * on top of it (captureDraft) merge in. Device selection comes from the
 * saved preferences, so this works from any window. */
export async function startWithActivePreset(): Promise<void> {
	const preset = getActivePreset();
	const title = buildRecordingTitle(captureDraft.title, preset);
	const tags = mergeDraftTags([...preset.tags, ...captureDraft.tags], captureDraft.tagDraft);
	captureDraft.tagDraft = '';
	const recType = captureDraft.recType ?? (preset.recType || null);
	if (isAndroidTauri()) {
		await startRecording(title, tags, ANDROID_MIC_ID, null, false, recType);
		return;
	}
	const savedMicrophone = localStorage.getItem('transcripter.microphone') ?? '';
	const savedSystemOutput = localStorage.getItem('transcripter.system-output') ?? SYSTEM_AUDIO_OFF;
	await startRecording(
		title,
		tags,
		savedMicrophone || null,
		savedSystemOutput === SYSTEM_AUDIO_OFF ? null : savedSystemOutput,
		savedSystemOutput !== SYSTEM_AUDIO_OFF,
		recType
	);
}

// ── Cross-window session sync (desktop) ────────────────────────────────────
// The quick-record popup is its own webview with its own store instance; a
// session started there is invisible to this window unless we ask the Rust
// backend. `cmd_recording_frames` doubles as the liveness probe: it
// rejects with "no active session" while idle.

/** One probe: adopt a backend session this window did not start. */
export async function syncRecordingState(): Promise<void> {
	if (isAndroidTauri() || recorder.recording) return;
	try {
		const frames = await commands.recordingFrames();
		if (recorder.recording) return;
		recorder.recording = true;
		recorder.frames = frames;
		void setMascotState('recording');
		startFramesPoller();
	} catch {
		// no active session — nothing to adopt
	}
}

let recordingSyncTimer: number | null = null;
/** Slow reconciler for the main window: notices a popup-started session
 * within ~3 s so the REC pill appears while browsing other pages. */
export function startRecordingSync(): void {
	if (recordingSyncTimer !== null || isAndroidTauri()) return;
	void syncRecordingState();
	recordingSyncTimer = globalThis.setInterval(() => void syncRecordingState(), 3000);
}
