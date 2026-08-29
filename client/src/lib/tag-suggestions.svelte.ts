/** Cross-mount cache for the tag picker source (same pattern as
 * profilesCache): GET /tags is the primary source; when it fails
 * (offline, unauthorized) the cache degrades to "recent tags" derived
 * from the first recordings page. Either way the picker is an
 * accelerator — free-form tag entry always works without it. */
import { fetchTags, listRecordings, type TagCount } from './api.svelte';
import { buildRecentSuggestions, buildTagSuggestions, type TagSuggestion } from './tags';

export const tagSuggestionsCache = $state<{
	items: TagSuggestion[];
	loaded: boolean;
	/** True when rows came from GET /tags; false = recordings fallback. */
	fromServer: boolean;
}>({ items: [], loaded: false, fromServer: false });

let inflight: Promise<void> | null = null;

export async function ensureTagSuggestions(cfg: { baseUrl: string; token: string }): Promise<void> {
	if (tagSuggestionsCache.loaded) return;
	inflight ??= (async () => {
		try {
			const counts: TagCount[] = await fetchTags(cfg);
			tagSuggestionsCache.items = buildTagSuggestions(counts);
			tagSuggestionsCache.fromServer = true;
		} catch {
			// GET /tags unavailable — derive recent tags from the recordings
			// list (newest page first). Failure leaves the cache empty.
			try {
				const page = await listRecordings(cfg, { limit: 50, offset: 0 });
				tagSuggestionsCache.items = buildRecentSuggestions(page.items);
			} catch {
				/* free-form entry still works */
			}
		} finally {
			tagSuggestionsCache.loaded = true;
			inflight = null;
		}
	})();
	return inflight;
}
