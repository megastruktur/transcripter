import type { Recording, TagCount } from './api.svelte';

/** One row of the tag-picker dropdown: a server-known freehand tag plus
 * how many recordings already carry it. Sourced from GET /tags (recent
 * freehand tags), with an offline fallback built from the recordings list. */
export type TagSuggestion = {
	tag: string;
	/** How many recordings carry the tag ("×3") — server counts from
	 * GET /tags; the recordings fallback shows "recent" instead. */
	count: number | null;
	/** True when the row comes from the recordings-list fallback (the
	 * /tags endpoint was unreachable). */
	recent: boolean;
};

/** Tag helpers shared by the record page and the recording-detail page.
 * Client-side mirror of the server's `_normalize_tags`
 * (server/api/app/routes/recordings.py): trim, lowercase, drop blanks,
 * preserve first-seen order, dedupe. Mirroring it here means chips render
 * in their final canonical form and request bodies never carry whitespace
 * variants the server would silently drop. */
export function normalizeTag(raw: string): string | null {
	const norm = raw.trim().toLowerCase();
	return norm.length > 0 ? norm : null;
}

/** Merges an in-progress draft into the tag list. Comma splits allow
 * paste-friendly multi-tag entry ("a, b, c"); already-present tags are
 * skipped. Returns a new array; same length as `tags` means no change. */
export function mergeDraftTags(tags: string[], draft: string): string[] {
	const next = [...tags];
	for (const part of draft.split(',')) {
		const norm = normalizeTag(part);
		if (norm !== null && !next.includes(norm)) next.push(norm);
	}
	return next;
}

/** Folds the GET /tags payload into picker rows. The server already
 * orders by count desc then tag asc; that order is preserved. Already-
 * added tags are NOT filtered here — TagChips does that per render so the
 * same suggestion list works for every input instance. */
export function buildTagSuggestions(tagCounts: TagCount[]): TagSuggestion[] {
	return tagCounts.map((tc) => ({ tag: tc.tag, count: tc.count, recent: false }));
}

/** Offline fallback: derive "recent tags" from a recordings list when
 * GET /tags is unavailable. Newest first (the caller passes the page it
 * already has, newest-first), later occurrences win over earlier ones,
 * deduped. Counts are unknown → `count: null`. */
export function buildRecentSuggestions(recordings: Recording[]): TagSuggestion[] {
	const seen = new Set<string>();
	const rows: TagSuggestion[] = [];
	for (const rec of recordings) {
		for (const raw of rec.tags) {
			const tag = normalizeTag(raw);
			if (tag === null || seen.has(tag)) continue;
			seen.add(tag);
			rows.push({ tag, count: null, recent: true });
		}
	}
	return rows;
}
