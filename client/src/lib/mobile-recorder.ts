/**
 * Mobile (Android Tauri WebView) capture path.
 *
 * On Android the desktop Rust capture stack is gated out (see ANDROID_POC.md);
 * recording is a front-end concern: getUserMedia({ audio: true }) ->
 * MediaRecorder (webm/opus) -> on stop, a single multipart POST
 * /recordings/direct through `uploadDirect`.
 *
 * Desktop flow is untouched: it goes through `stores.svelte.ts` ->
 * `commands.startRecording` / `stopRecording` (Rust IPC). This module is only
 * reached when the runtime is BOTH a Tauri webview (`__TAURI_INTERNALS__` is
 * injected by wry on every Tauri target, including android) AND the user agent
 * looks like Android. Browser-only sessions stay on the read-only path as
 * before — `isAndroidTauri()` is the single switch the capture page reads.
 */

import { startService, stopService } from 'tauri-plugin-background-service';

const ANDROID_UA = /Android/i;
/** Pseudo device id the shared store reports on Android: the platform owns
 * device routing (system mic via getUserMedia), so there is exactly one
 * selectable "device" and the desktop IPC enumeration does not exist. */
export const ANDROID_MIC_ID = 'android-system-mic';

/**
 * Raise the mic-type foreground service that keeps the process (and this
 * WebView's MediaRecorder) alive when the user backgrounds the app. MUST be
 * called while the app is in the foreground — Android rejects mic-type FGS
 * starts from the background (while-in-use restriction on RECORD_AUDIO).
 * A failure here means a backgrounded recording would die silently, so
 * callers must treat it as fatal for the recording start (fail-loud).
 */
export async function startRecordingKeepalive(): Promise<void> {
	await startService({ serviceLabel: 'Идёт запись', foregroundServiceType: 'microphone' });
}

/** Best-effort stop: safe to call when the service was never started or is
 * already gone (e.g. system killed it) — errors are swallowed on purpose. */
export async function stopRecordingKeepalive(): Promise<void> {
	try {
		await stopService();
	} catch {
		// keepalive teardown must never mask the recording's own stop path
	}
}

export function isTauriWebview(): boolean {
	return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

export function isAndroidTauri(): boolean {
	if (!isTauriWebview()) return false;
	const ua = typeof navigator !== 'undefined' ? navigator.userAgent : '';
	return ANDROID_UA.test(ua);
}

/**
 * Pick the best MediaRecorder mimeType we can. webm/opus is the canonical
 * container for the WebView pipeline (see ANDROID_POC.md); most modern
 * Android System WebViews support it. We probe `isTypeSupported` and fall
 * back to the recorder's default (still wrapped in a Blob — the server
 * transcodes anything that isn't FLAC magic to canonical FLAC).
 */
export function pickRecorderMime(): string | undefined {
	if (typeof MediaRecorder === 'undefined') return undefined;
	const candidates = [
		'audio/webm;codecs=opus',
		'audio/webm',
		'audio/ogg;codecs=opus',
		'audio/ogg',
		'audio/mp4;codecs=mp4a.40.2'
	];
	for (const mime of candidates) {
		try {
			if (MediaRecorder.isTypeSupported(mime)) return mime;
		} catch {
			// Some webviews throw on unknown mimes instead of returning false.
		}
	}
	return undefined;
}

export type MobileRecorder = {
	/** Resolves once the MediaRecorder is actually capturing; rejects with
	 * a normalized error when the mic prompt is denied or setup fails.
	 * Callers MUST gate their "recording" UI state on this — without it a
	 * denied prompt looks like a running recording until Stop. */
	ready: Promise<void>;
	stop(): Promise<Blob>;
	cancel(): void;
	mimeType: string;
};

/**
 * Start a MediaRecorder on a microphone MediaStream. Caller is responsible
 * for stopping the stream when done — the returned `stop()` releases
 * everything (MediaRecorder stop event then track.stop on each track).
 *
 * `onChunk` fires for every `ondataavailable` event, which lets a future
 * iteration stream-upload chunks; for the PoC we keep everything in memory
 * and POST the final Blob. The signature exists so the call site does not
 * need to change when chunked upload lands.
 */
export function startMobileRecorder(opts: {
	onChunk?: (chunk: Blob) => void;
}): MobileRecorder {
	if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
		throw new Error('microphone API unavailable in this WebView');
	}
	// getUserMedia is async on every WebView we care about; capture the
	// resolver pair synchronously so the rest of the wiring is linear.
	let resolveStream!: (stream: MediaStream) => void;
	let rejectStream!: (error: unknown) => void;
	const streamReady = new Promise<MediaStream>((res, rej) => {
		resolveStream = res;
		rejectStream = rej;
	});
	void navigator.mediaDevices
		.getUserMedia({ audio: true })
		.then(resolveStream, (error: unknown) => rejectStream(normalizeMediaError(error)));

	const mimeType = pickRecorderMime();
	const recorderOptions: MediaRecorderOptions = {};
	if (mimeType) recorderOptions.mimeType = mimeType;
	// 1 s timeslice keeps `ondataavailable` flowing without flooding the JS
	// event loop; future chunked-upload iterations reuse these chunks.
	recorderOptions.audioBitsPerSecond = 64_000;

	let recorder: MediaRecorder | null = null;
	let stream: MediaStream | null = null;
	// cancel() during a PENDING getUserMedia must win over a late grant:
	// without this flag the .then below would construct and start a
	// MediaRecorder on a handle the caller already abandoned.
	let cancelled = false;
	const chunks: Blob[] = [];
	let resolveStop!: (blob: Blob) => void;
	let rejectStop!: (error: unknown) => void;
	const stopDone = new Promise<Blob>((res, rej) => {
		resolveStop = res;
		rejectStop = rej;
	});
	let resolveReady!: () => void;
	let rejectReady!: (error: unknown) => void;
	const ready = new Promise<void>((res, rej) => {
		resolveReady = res;
		rejectReady = rej;
	});
	// A denial/failure path rejects BOTH promises: `ready` flips the UI out
	// of its pending state, `stopDone` unblocks a racing stop() call.

	streamReady
		.then((s) => {
			if (cancelled) {
				for (const track of s.getTracks()) track.stop();
				throw new Error('cancelled');
			}
			stream = s;
			const r = new MediaRecorder(s, recorderOptions);
			recorder = r;
			r.ondataavailable = (event: BlobEvent) => {
				if (event.data && event.data.size > 0) {
					chunks.push(event.data);
					opts.onChunk?.(event.data);
				}
			};
			// MediaRecorderErrorEvent is the standard event shape from the
			// dom-mediacapture-record lib; `error` is a DOMException with a
			// `message` on every browser engine that ships MediaRecorder.
			// TS's built-in DOM lib types MediaRecorder.onerror as ErrorEvent
			// (the older dom-mediacapture-record types define a separate
			// MediaRecorderErrorEvent, but it isn't part of the bundled lib).
			r.onerror = (event: Event) => {
				const message = (event as ErrorEvent).message;
				rejectStop(new Error(message || 'MediaRecorder error'));
			};
			r.onstop = () => {
				const blob = new Blob(chunks, {
					type: (mimeType ?? r.mimeType) || 'application/octet-stream'
				});
				resolveStop(blob);
			};
			r.start(1000);
			resolveReady();
		})
		.catch((error) => {
			rejectReady(error);
			rejectStop(error);
		});

	return {
		ready,
		mimeType: mimeType ?? '',
		stop(): Promise<Blob> {
			try {
				if (recorder && recorder.state !== 'inactive') recorder.stop();
				else if (!recorder) rejectStop(new Error('recorder never started'));
			} catch (error) {
				rejectStop(error);
			}
			return stopDone.finally(() => {
				if (stream) {
					for (const track of stream.getTracks()) track.stop();
					stream = null;
				}
			});
		},
		cancel(): void {
			cancelled = true;
			try {
				if (recorder && recorder.state !== 'inactive') {
					recorder.stop();
					chunks.length = 0;
				}
			} catch {
				// best-effort: the stream is still cleaned up by the .finally
			}
			if (stream) {
				for (const track of stream.getTracks()) track.stop();
				stream = null;
			}
			rejectStop(new Error('cancelled'));
		}
	};
}

/**
 * Surface a useful error string for the two failure modes we actually see
 * on Android: NotAllowedError (user denied the prompt, or the manifest
 * doesn't declare RECORD_AUDIO) and NotFoundError (no microphone at all).
 * Anything else gets the raw message.
 */
function normalizeMediaError(error: unknown): Error {
	if (error instanceof Error) {
		const name = (error as DOMException).name ?? '';
		if (name === 'NotAllowedError' || name === 'SecurityError') {
			return new Error('microphone blocked — allow it in the system prompt');
		}
		if (name === 'NotFoundError' || name === 'OverconstrainedError') {
			return new Error('no microphone available');
		}
		return error;
	}
	return new Error(String(error));
}
