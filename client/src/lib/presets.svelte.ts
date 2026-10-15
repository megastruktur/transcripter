/** Named capture presets: a 1-tap identity for a recording — title stem,
 * type profile and starter tags. Persisted in localStorage so the Record
 * page, the quick-record popup (a separate webview window on desktop) and
 * the mascot menu all seed the same set; `selectPreset` mirrors the choice
 * into the shared `captureDraft` so every start path applies it. */

import { captureDraft } from '$lib/stores.svelte';
import { normalizeTag } from '$lib/tags';

export interface CapturePreset {
	id: string;
	name: string;
	/** '' = default pipeline; otherwise a server profile type ('dnd', …). */
	recType: string;
	tags: string[];
}

const PRESETS_KEY = 'transcripter.presets.v1';
const ACTIVE_KEY = 'transcripter.activePresetId.v1';

const DEFAULT_PRESETS: CapturePreset[] = [
	{ id: 'default-note', name: 'Quick Note', recType: '', tags: [] },
	{ id: 'session-dnd', name: 'Game Session', recType: '', tags: ['dnd'] }
];

export const presetState = $state<{ presets: CapturePreset[]; activeId: string }>({
	presets: [],
	activeId: ''
});

function persistPresets(): void {
	if (typeof localStorage === 'undefined') return;
	localStorage.setItem(PRESETS_KEY, JSON.stringify(presetState.presets));
	localStorage.setItem(ACTIVE_KEY, presetState.activeId);
}

/** Load (or seed) presets from localStorage. No-op without storage (SSR,
 * pre-mount): getActivePreset() still answers with the module default so
 * callers never see undefined. */
export function loadPresets(): void {
	if (typeof localStorage === 'undefined') return;
	let stored: CapturePreset[] = [];
	try {
		const parsed: unknown = JSON.parse(localStorage.getItem(PRESETS_KEY) ?? '[]');
		if (Array.isArray(parsed)) stored = parsed as CapturePreset[];
	} catch {
		stored = [];
	}
	if (stored.length === 0) {
		stored = DEFAULT_PRESETS.map((p) => ({ ...p, tags: [...p.tags] }));
	}
	presetState.presets = stored;
	const active = localStorage.getItem(ACTIVE_KEY) ?? '';
	presetState.activeId = stored.some((p) => p.id === active) ? active : stored[0].id;
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
	persistPresets();
	selectPreset(preset.id);
	return preset;
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
