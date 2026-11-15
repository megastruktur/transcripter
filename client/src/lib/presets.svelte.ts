/** Named capture presets: a 1-tap identity for a recording — title stem,
 * type profile and starter tags. localStorage stays the instant UI source;
 * syncPresets() reconciles with the server at boot (server-authoritative
 * last-write-wins + tombstones, W4/W5) so presets follow the owner between
 * devices. The Record page, the quick-record popup (a separate webview
 * window on desktop) and the mascot menu all seed the same set;
 * `selectPreset` mirrors the choice into the shared `captureDraft` so every
 * start path applies it. */

import { captureDraft } from '$lib/stores.svelte';
import { normalizeTag } from '$lib/tags';
import {
	deletePresetRemote,
	listPresets,
	loadApiConfig,
	putPreset,
	type ApiConfig,
	type PresetRow
} from './api.svelte';

export interface CapturePreset {
	id: string;
	name: string;
	/** '' = default pipeline; otherwise a server profile type ('dnd', …). */
	recType: string;
	tags: string[];
}

/** Persisted entry shapes under PRESETS_KEY ({presets, tombstones} since
 * W5): `updatedAt` is the server's ISO-8601 UTC stamp from the last
 * CONFIRMED upload ('' = never synced, i.e. older than any server row);
 * `dirty` = local change the server has not acknowledged yet. */
type StoredPreset = CapturePreset & { updatedAt: string; dirty?: boolean };
type StoredTombstone = { id: string; updatedAt: string; dirty?: boolean };

const PRESETS_KEY = 'transcripter.presets.v1';
const ACTIVE_KEY = 'transcripter.activePresetId.v1';

const DEFAULT_PRESETS: CapturePreset[] = [
	{ id: 'default-note', name: 'Quick Note', recType: '', tags: [] },
	{ id: 'session-dnd', name: 'Game Session', recType: '', tags: ['dnd'] },
	{ id: 'preset-vet', name: 'Vet Consultation', recType: 'vet-consultation', tags: ['vet'] }
];

export const presetState = $state<{ presets: CapturePreset[]; activeId: string }>({
	presets: [],
	activeId: ''
});

/** Sync bookkeeping, deliberately OUTSIDE presetState: the $state array
 * stays plain CapturePreset[] so the UI contract (+page.svelte,
 * QuickRecordPopup) is unchanged. Maps: id → last confirmed server
 * stamp / dirty flag. */
let presetMeta = new Map<string, { updatedAt: string; dirty: boolean }>();
/** Presets deleted locally — the server keeps matching tombstone rows so
 * the deletion itself propagates to other devices. */
let tombstoneMeta = new Map<string, { updatedAt: string; dirty: boolean }>();

function persistPresets(): void {
	if (typeof localStorage === 'undefined') return;
	const presets: StoredPreset[] = presetState.presets.map((p) => ({
		...p,
		...(presetMeta.get(p.id) ?? { updatedAt: '', dirty: false })
	}));
	const tombstones: StoredTombstone[] = [...tombstoneMeta.entries()].map(([id, m]) => ({
		id,
		...m
	}));
	localStorage.setItem(PRESETS_KEY, JSON.stringify({ presets, tombstones }));
	localStorage.setItem(ACTIVE_KEY, presetState.activeId);
}

/** Load (or seed) presets from localStorage. Accepts BOTH persisted
 * shapes under PRESETS_KEY: the pre-sync plain CapturePreset[] (each
 * entry migrates to updatedAt='' dirty=true so it uploads on first
 * sync) and the W5 {presets, tombstones} object. Seeds only when BOTH
 * lists are empty (fresh install — never fight a synced deletion).
 * No-op without storage (SSR, pre-mount): getActivePreset() still
 * answers with the module default so callers never see undefined. */
export function loadPresets(): void {
	if (typeof localStorage === 'undefined') return;
	let entries: StoredPreset[] = [];
	let tombstones: StoredTombstone[] = [];
	try {
		const parsed: unknown = JSON.parse(localStorage.getItem(PRESETS_KEY) ?? 'null');
		if (Array.isArray(parsed)) {
			// Pre-sync format (plain array). Per-entry safety comes from the
			// id filter below; the cast only re-labels the checked array.
			const legacy = parsed as CapturePreset[];
			entries = legacy.map((p) => ({ ...p, updatedAt: '', dirty: true }));
		} else if (parsed !== null && typeof parsed === 'object' && 'presets' in parsed) {
			if (Array.isArray(parsed.presets)) entries = parsed.presets as StoredPreset[];
			if ('tombstones' in parsed && Array.isArray(parsed.tombstones)) {
				tombstones = parsed.tombstones as StoredTombstone[];
			}
		}
	} catch {
		entries = [];
		tombstones = [];
	}
	presetMeta = new Map(
		entries
			.filter((e) => typeof e?.id === 'string')
			.map((e) => [e.id, { updatedAt: e.updatedAt ?? '', dirty: e.dirty ?? false }])
	);
	tombstoneMeta = new Map(
		tombstones
			.filter((t) => typeof t?.id === 'string')
			.map((t) => [t.id, { updatedAt: t.updatedAt ?? '', dirty: t.dirty ?? false }])
	);
	const stored: CapturePreset[] = [];
	for (const e of entries) {
		if (typeof e?.id !== 'string' || stored.some((p) => p.id === e.id)) continue;
		stored.push({ id: e.id, name: e.name, recType: e.recType, tags: e.tags });
	}
	if (stored.length === 0 && tombstoneMeta.size === 0) {
		stored.push(...DEFAULT_PRESETS.map((p) => ({ ...p, tags: [...p.tags] })));
		// Fresh install: the seeds upload on the first sync.
		presetMeta = new Map(stored.map((p) => [p.id, { updatedAt: '', dirty: true }]));
	}
	presetState.presets = stored;
	const active = localStorage.getItem(ACTIVE_KEY) ?? '';
	presetState.activeId =
		stored.length === 0 ? '' : stored.some((p) => p.id === active) ? active : stored[0].id;
	persistPresets();
}

/** The preset to apply right now: the selected one, else the first, else
 * the built-in default when storage was never reachable. */
export function getActivePreset(): CapturePreset {
	return (
		presetState.presets.find((p) => p.id === presetState.activeId) ??
		presetState.presets[0] ?? { ...DEFAULT_PRESETS[0], tags: [] }
	);
}

/** 1-tap selection: persists the choice and mirrors the preset into the
 * capture draft so the next start (from any surface) applies it. */
export function selectPreset(id: string): void {
	const preset = presetState.presets.find((p) => p.id === id);
	if (!preset) return;
	presetState.activeId = preset.id;
	if (typeof localStorage !== 'undefined') localStorage.setItem(ACTIVE_KEY, preset.id);
	captureDraft.recType = preset.recType === '' ? null : preset.recType;
	captureDraft.tags = [...preset.tags];
	captureDraft.tagDraft = '';
}

function newPresetId(): string {
	if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
	return `preset-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Create or update a preset, persist it and make it active. Tags are
 * normalized exactly like the chip editor would (server parity). */
export function savePreset(input: {
	id?: string;
	name: string;
	recType: string;
	tags: string[];
}): CapturePreset {
	const name = input.name.trim() || 'Untitled preset';
	const recType = input.recType ?? '';
	const tags = [...new Set(input.tags.map(normalizeTag).filter((t) => t !== null))];
	let preset = input.id ? presetState.presets.find((p) => p.id === input.id) : undefined;
	if (preset) {
		preset.name = name;
		preset.recType = recType;
		preset.tags = tags;
	} else {
		preset = { id: input.id ?? newPresetId(), name, recType, tags };
		presetState.presets.push(preset);
	}
	// Sync: the local write is newest-but-unconfirmed ('' = older than any
	// server stamp); a PUT on a former tombstone id resurrects it.
	presetMeta.set(preset.id, { updatedAt: '', dirty: true });
	tombstoneMeta.delete(preset.id);
	persistPresets();
	selectPreset(preset.id);
	void pushPreset(preset);
	return preset;
}

/** Fire-and-forget upload of one preset; on success the server stamp is
 * stored and the entry goes clean, on failure it stays dirty and the
 * next syncPresets (boot) retries. Never throws into the UI. */
function pushPreset(preset: CapturePreset): Promise<void> {
	return putPreset(loadApiConfig(), preset.id, {
		name: preset.name,
		rec_type: preset.recType,
		tags: preset.tags
	})
		.then((row) => {
			presetMeta.set(preset.id, { updatedAt: row.updated_at, dirty: false });
			persistPresets();
		})
		.catch(() => {});
}

/** Delete a preset. Deleting the last one re-seeds the default 'Quick
 * Note'; `activeId` is kept pointing at a survivor (re-selecting it also
 * re-syncs the draft when the deleted preset was active). */
export function deletePreset(id: string): void {
	const index = presetState.presets.findIndex((p) => p.id === id);
	if (index === -1) return;
	const wasActive = presetState.activeId === id;
	presetState.presets.splice(index, 1);
	if (presetState.presets.length === 0) {
		presetState.presets.push({ ...DEFAULT_PRESETS[0], tags: [] });
	}
	if (!presetState.presets.some((p) => p.id === presetState.activeId)) {
		presetState.activeId = presetState.presets[0].id;
	}
	const reseeded = presetState.presets.find((p) => p.id === id);
	if (reseeded) {
		// Deleting the LAST preset re-seeded the same id (default-note):
		// net effect is a save (resurrect/update), not a delete.
		presetMeta.set(id, { updatedAt: '', dirty: true });
		tombstoneMeta.delete(id);
		void pushPreset(reseeded);
	} else {
		// Tombstone: the row must vanish here but the deletion has to
		// propagate, so the server keeps a deleted=true row we can sync.
		presetMeta.delete(id);
		tombstoneMeta.set(id, { updatedAt: '', dirty: true });
		void deletePresetRemote(loadApiConfig(), id)
			.then(() => {
				// Acknowledged (404 = never existed server-side; the '' stamp
				// stays — nothing to propagate either way).
				const meta = tombstoneMeta.get(id);
				if (meta) tombstoneMeta.set(id, { ...meta, dirty: false });
				persistPresets();
			})
			.catch(() => {});
	}
	persistPresets();
	if (wasActive) selectPreset(presetState.activeId);
}

/** Custom title wins; otherwise `<preset name> · YYYY-MM-DD HH:mm` so an
 * untitled capture is still identifiable in the library list. */
export function buildRecordingTitle(customTitle: string, preset?: CapturePreset | null): string {
	const custom = customTitle.trim();
	if (custom.length > 0) return custom;
	const now = new Date();
	const pad = (n: number): string => String(n).padStart(2, '0');
	const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
	return `${preset?.name || 'Capture'} · ${stamp}`;
}

/** Server updated_at is ISO-8601 UTC in TWO shapes: naive
 * ("2026-11-15T17:20:00", sqlite read-back, UTC implied) and
 * offset-aware ("…+00:00", PUT/DELETE responses). NEVER string-compare:
 * normalize naive values by appending Z so Date treats them as UTC;
 * ''/undefined/unparseable → 0 (oldest). */
function ts(iso: string | undefined): number {
	const s = iso ?? '';
	const normalized = s.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(s) ? s : `${s}Z`;
	const parsed = Date.parse(normalized);
	return Number.isNaN(parsed) ? 0 : parsed;
}

/** Boot-time sync (fire-and-forget from +layout onMount): push every
 * dirty preset/tombstone, then pull the server feed and merge with
 * last-write-wins on updated_at — server-authoritative stamps, ties
 * keep local (it was just pushed). Any failure (offline, no token,
 * server down) returns silently: localStorage-only behavior is exactly
 * today's. */
export async function syncPresets(cfg: ApiConfig): Promise<void> {
	try {
		for (const preset of presetState.presets) {
			const meta = presetMeta.get(preset.id);
			if (!meta?.dirty) continue;
			try {
				const row = await putPreset(cfg, preset.id, {
					name: preset.name,
					rec_type: preset.recType,
					tags: preset.tags
				});
				presetMeta.set(preset.id, { updatedAt: row.updated_at, dirty: false });
			} catch {
				/* stays dirty; retried next boot */
			}
		}
		for (const [id, meta] of [...tombstoneMeta]) {
			if (!meta.dirty) continue;
			try {
				await deletePresetRemote(cfg, id);
				tombstoneMeta.set(id, { ...meta, dirty: false });
			} catch {
				/* stays dirty; retried next boot */
			}
		}
		mergeRemotePresets(await listPresets(cfg));
	} catch {
		/* offline: keep localStorage-only state exactly as-is */
	}
}

function mergeRemotePresets(rows: PresetRow[]): void {
	// Invariant: an id is EITHER a live preset OR a tombstone, never both.
	const next = new Map(presetState.presets.map((p) => [p.id, { ...p }]));
	const nextMeta = new Map(presetMeta);
	const nextTomb = new Map(tombstoneMeta);
	for (const row of rows) {
		const local = next.get(row.id);
		// Our stamp for the id, whether it is currently live or a tombstone.
		const localTs = local
			? ts(nextMeta.get(row.id)?.updatedAt)
			: ts(nextTomb.get(row.id)?.updatedAt);
		const remoteTs = ts(row.updated_at);
		if (row.deleted) {
			if (local && remoteTs > localTs) {
				// Deleted elsewhere, newer than our copy: drop the local row
				// and keep the tombstone (clean) so it stays propagated.
				next.delete(row.id);
				nextMeta.delete(row.id);
				nextTomb.set(row.id, { updatedAt: row.updated_at, dirty: false });
			} else if (!local && !nextTomb.get(row.id)?.dirty) {
				// Tombstone for an id never live here (or already clean):
				// remember it clean so a later local create with the same id
				// does not resurrect silently. A dirty tombstone means our
				// delete push failed — keep it for the next-boot retry.
				nextTomb.set(row.id, { updatedAt: row.updated_at, dirty: false });
			}
		} else if (remoteTs > localTs) {
			// Live row newer than our copy — or than our tombstone
			// (resurrected elsewhere) — or never seen locally: take the
			// server version, clean.
			next.set(row.id, {
				id: row.id,
				name: row.name,
				recType: row.rec_type,
				tags: [...row.tags]
			});
			nextTomb.delete(row.id);
			nextMeta.set(row.id, { updatedAt: row.updated_at, dirty: false });
		}
		// Local newer or equal → keep local (it was pushed in step 1).
	}
	presetState.presets = [...next.values()];
	presetMeta = nextMeta;
	tombstoneMeta = nextTomb;
	if (
		presetState.presets.length > 0 &&
		!presetState.presets.some((p) => p.id === presetState.activeId)
	) {
		presetState.activeId = presetState.presets[0].id;
	}
	persistPresets();
}
