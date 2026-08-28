import type { ProfileInfo } from './api.svelte';

/** One row of the tag-picker dropdown: a server-known tag plus the
 * "parsing logic" (profile) it activates. Several profiles may claim the
 * same tag (the server's match rule picks the first by sorted id) — the
 * row folds them into one entry naming all of them. */
export type TagSuggestion = {
	tag: string;
	/** Display names of every profile declaring this tag. */
	profiles: string[];
	/** First profile's description — shown as the row's subtitle. */
	description: string;
	/** True when any claiming profile extracts into the knowledge graph. */
	hasEnrich: boolean;
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

/** Folds the profile list into picker rows, one per tag, sorted by tag.
 * Already-added tags are NOT filtered here — TagChips does that per
 * render so the same suggestion list works for every input instance. */
export function buildTagSuggestions(profiles: ProfileInfo[]): TagSuggestion[] {
	const byTag = new Map<string, TagSuggestion>();
	for (const profile of profiles) {
		for (const raw of profile.tags) {
			const tag = normalizeTag(raw);
			if (tag === null) continue;
			const row = byTag.get(tag);
			if (row) {
				if (!row.profiles.includes(profile.display_name)) {
					row.profiles.push(profile.display_name);
				}
				row.hasEnrich = row.hasEnrich || profile.has_enrich;
			} else {
				byTag.set(tag, {
					tag,
					profiles: [profile.display_name],
					description: profile.description,
					hasEnrich: profile.has_enrich
				});
			}
		}
	}
	return [...byTag.values()].sort((a, b) => a.tag.localeCompare(b.tag));
}
