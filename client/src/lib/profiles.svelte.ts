/** Cross-mount cache for GET /profiles (same pattern as audioDevices in
 * stores.svelte.ts): remounts render instantly from the cache while a
 * background refresh fills in changes. Profiles change only when the user
 * edits yaml files on the server, so a session-long cache with a silent
 * refresh per mount is the right staleness budget.
 *
 * Failure mode: the cache simply stays empty and tag inputs degrade to
 * free-form entry — the suggestions dropdown is an accelerator, never a
 * gate. */
import { listProfiles, type ApiConfig, type ProfileInfo } from './api.svelte';

export const profilesCache = $state<{ items: ProfileInfo[]; loaded: boolean }>({
	items: [],
	loaded: false
});

let inflight: Promise<void> | null = null;

export async function ensureProfiles(cfg: ApiConfig): Promise<void> {
	if (profilesCache.loaded) return;
	inflight ??= listProfiles(cfg)
		.then((profiles) => {
			profilesCache.items = profiles;
			profilesCache.loaded = true;
		})
		.catch(() => {
			/* offline or unauthorized: free-form tags still work */
		})
		.finally(() => {
			inflight = null;
		});
	return inflight;
}
