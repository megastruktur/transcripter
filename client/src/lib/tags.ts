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
