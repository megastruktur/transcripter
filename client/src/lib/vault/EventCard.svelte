<script lang="ts">
	import Icon from '$lib/Icon.svelte';
	import type { TimelineEvent } from '$lib/api.svelte';

	/** One timeline event with phase-A edit/delete affordances. The card
	 * stays read-only until an action opens the inline recess (the same
	 * one-editor-open-at-a-time idiom as the entity rename). Deletes are
	 * two-step (arm → confirm) inside the card — never a native dialog. */

	let {
		event,
		eventKey,
		accent,
		saving,
		onedit,
		ondelete
	}: {
		event: TimelineEvent;
		eventKey: string | null;
		accent: string;
		saving: boolean;
		/** (fields, feedback) — fields = any of ts/kind/summary/mentions. */
		onedit: (fields: { ts?: string; kind?: string; summary?: string; mentions?: string[] }, feedback: string) => Promise<boolean>;
		ondelete: () => Promise<boolean>;
	} = $props();

	let editing = $state(false);
	let deleting = $state(false);
	let armed = $state(false);
	let ts = $state('');
	let kind = $state('');
	let summary = $state('');
	let mentions = $state('');
	let feedback = $state('');
	let error = $state('');

	function startEdit(): void {
		editing = true;
		armed = false;
		deleting = false;
		error = '';
		ts = event.ts;
		kind = event.kind;
		summary = event.summary;
		mentions = event.mentions.join(', ');
		feedback = '';
	}

	function cancelEdit(): void {
		if (saving) return;
		editing = false;
		error = '';
	}

	async function save(): Promise<boolean> {
		const fields: { ts?: string; kind?: string; summary?: string; mentions?: string[] } = {};
		if (ts.trim() !== event.ts) fields.ts = ts.trim();
		if (kind.trim() !== event.kind) fields.kind = kind.trim();
		if (summary !== event.summary) fields.summary = summary.trim();
		const nextMentions = mentions.split(',').map((m) => m.trim()).filter(Boolean);
		if (nextMentions.join(',') !== event.mentions.join(',')) fields.mentions = nextMentions;
		if (Object.keys(fields).length === 0) {
			editing = false;
			return true;
		}
		const ok = await onedit(fields, feedback.trim());
		if (ok) editing = false;
		else error = 'The server rejected the edit — nothing changed.';
		return ok;
	}

	async function confirmDelete(): Promise<boolean> {
		const ok = await ondelete();
		if (!ok) {
			armed = false;
			error = 'The server refused the delete — nothing changed.';
		}
		return ok;
	}
</script>

{#if editing}
	<div class="event-edit" style="--event-accent: {accent}">
		<form
			class="event-edit-form"
			onsubmit={(e) => {
				e.preventDefault();
				void save();
			}}
		>
			<input class="edit-ts" type="text" aria-label="Timestamp" bind:value={ts} disabled={saving} maxlength="32" placeholder="00:00" />
			<input class="edit-kind" type="text" aria-label="Kind" bind:value={kind} disabled={saving} maxlength="100" placeholder="kind" />
			<textarea class="edit-summary" aria-label="Summary" bind:value={summary} disabled={saving} maxlength="2000" rows="2" placeholder="What happened"></textarea>
			<input class="edit-mentions" type="text" aria-label="Mentions, comma-separated" bind:value={mentions} disabled={saving} placeholder="mentions, comma-separated" />
			<input class="edit-feedback" type="text" aria-label="Correction rule (optional)" bind:value={feedback} disabled={saving} maxlength="500" placeholder="correction rule — teaches future extractions" />
			<div class="edit-actions">
				<button class="edit-save" type="submit" disabled={saving || !summary.trim()}>
					<Icon name="refresh" size={11} strokeWidth={1.6} />
					{saving ? 'Saving…' : 'Save'}
				</button>
				<button class="edit-cancel" type="button" disabled={saving} onclick={cancelEdit}>
					<Icon name="close" size={11} strokeWidth={1.6} />
					Cancel
				</button>
			</div>
		</form>
		{#if error}
			<p class="edit-error" role="alert">{error}</p>
		{/if}
	</div>
{:else}
	<div class="event-card" style="--event-accent: {accent}">
		<div class="event-head">
			<span class="event-ts">{event.ts}</span>
			<span class="event-kind" title={event.kind}>{event.kind}</span>
			<span class="event-actions">
				<button class="event-act" type="button" disabled={saving} onclick={startEdit} title="Edit event" aria-label={`Edit event at ${event.ts}`}>
					<Icon name="pencil" size={11} strokeWidth={1.6} />
				</button>
				{#if armed}
					<button class="event-act event-act-danger" type="button" disabled={saving} onclick={() => void confirmDelete()} title="Confirm delete">Delete?</button>
					<button class="event-act" type="button" disabled={saving} onclick={() => (armed = false)} title="Cancel delete">No</button>
				{:else}
					<button class="event-act event-act-danger" type="button" disabled={saving} onclick={() => (armed = true)} title="Delete event" aria-label={`Delete event at ${event.ts}`}>
						<Icon name="trash" size={11} strokeWidth={1.6} />
					</button>
				{/if}
			</span>
		</div>
		<p class="event-summary">{event.summary}</p>
		{#if event.mentions.length > 0}
			<div class="event-mentions">
				{#each event.mentions as mention (mention)}
					<span class="event-mention">{mention}</span>
				{/each}
			</div>
		{/if}
		{#if error}
			<p class="edit-error" role="alert">{error}</p>
		{/if}
	</div>
{/if}

<style>
	.event-card { display: grid; gap: 5px; padding: 8px 10px 8px 9px; border-left: 2px solid var(--event-accent, var(--brass)); background: rgba(0,0,0,.18); border-radius: 0 3px 3px 0; box-shadow: inset 0 1px 3px rgba(0,0,0,.32); }
	.event-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
	.event-ts { font: 10px/1.4 "SFMono-Regular", Consolas, monospace; color: var(--brass); font-variant-numeric: tabular-nums; flex: 0 0 auto; }
	.event-kind { color: var(--ash); font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
	.event-actions { display: inline-flex; gap: 4px; flex: 0 0 auto; }
	.event-act { display: inline-flex; align-items: center; gap: 4px; min-height: 20px; padding: 0 5px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.event-act:hover:not(:disabled) { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.event-act:disabled { opacity: 0.5; cursor: default; }
	.event-act-danger { color: #f36b60; }
	.event-act-danger:hover:not(:disabled) { color: #f36b60; border-color: var(--red); background: rgba(213,45,36,.1); }
	.event-summary { margin: 0; color: #c7bbad; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
	.event-mentions { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 1px; }
	.event-mention { padding: 1px 6px; border-radius: 2px; background: rgba(215,167,71,.08); color: var(--brass); font-size: 9px; font-weight: 650; line-height: 1.4; }
	.event-edit { display: grid; gap: 5px; padding: 8px 10px 8px 9px; border-left: 2px solid var(--event-accent, var(--brass)); background: rgba(0,0,0,.28); border-radius: 0 3px 3px 0; box-shadow: inset 0 1px 3px rgba(0,0,0,.32); }
	.event-edit-form { display: grid; gap: 6px; }
	.event-edit-form input, .event-edit-form textarea { min-width: 0; padding: 4px 8px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 11px; font-family: inherit; }
	.edit-ts { width: 64px; font: 10px/1.4 "SFMono-Regular", Consolas, monospace; font-variant-numeric: tabular-nums; }
	.edit-kind { width: 90px; }
	.edit-summary { resize: vertical; line-height: 1.45; }
	.edit-feedback { font-size: 10px; color: #ded3c4; }
	.edit-feedback::placeholder, .edit-mentions::placeholder { color: var(--ash); }
	.event-edit-form input:focus, .event-edit-form textarea:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }
	.event-edit-form input:disabled, .event-edit-form textarea:disabled { opacity: 0.6; }
	.edit-actions { display: flex; gap: 6px; }
	.edit-save { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; min-height: 24px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.edit-save:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.edit-save:disabled { opacity: 0.6; cursor: default; }
	.edit-cancel { display: inline-flex; align-items: center; gap: 5px; padding: 0 10px; min-height: 24px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: var(--ash); font-size: 10px; font-weight: 700; cursor: pointer; }
	.edit-cancel:hover:not(:disabled) { color: var(--bone); border-color: rgba(215,167,71,.4); }
	.edit-cancel:disabled { opacity: 0.6; cursor: default; }
	.edit-error { margin: 0; padding: 0 2px; color: #f36b60; font-size: 10px; }
</style>
