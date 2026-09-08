/**
 * Client identity for diagnostics: platform name + build version.
 *
 * Platform detection is UA-based on purpose — `@tauri-apps/api` is an
 * async IPC call, but every API request needs the header synchronously
 * (a cached async platform would race the first requests). The Tauri
 * webview UA carries the OS token, and `isAndroidTauri()` already
 * disambiguates the Android WebView from desktop Linux hosts.
 */

import { isAndroidTauri } from '$lib/mobile-recorder';

export type ClientPlatform = 'windows' | 'macos' | 'android' | 'linux' | 'web';

export function clientPlatform(): ClientPlatform {
	if (typeof navigator === 'undefined') return 'web';
	const ua = navigator.userAgent;
	if (isAndroidTauri()) return 'android';
	if (/Windows/i.test(ua)) return 'windows';
	if (/Macintosh|Mac OS X/i.test(ua)) return 'macos';
	if (/Linux|X11/i.test(ua) && !/Android/i.test(ua)) return 'linux';
	return 'web';
}

/**
 * X-Client header value: `<version>/<platform>` (e.g. `0.31.0/windows`).
 * One composed header shared by every API call site (req + the two raw
 * fetches in testConnection) — the server logs it verbatim; splitting
 * is a one-liner when a handler needs the parts.
 */
export function clientHeader(): string {
	return `${__APP_VERSION__}/${clientPlatform()}`;
}
