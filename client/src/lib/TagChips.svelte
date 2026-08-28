<script lang="ts">
	import Icon from './Icon.svelte';
	import { mergeDraftTags } from './tags';

	let {
		tags,
		onChange,
		disabled = false,
		placeholder = 'Add tag',
		dense = false,
		draft = $bindable('')
	}: {
		tags: string[];
		onChange: (next: string[]) => void;
		disabled?: boolean;
		placeholder?: string;
		/** Compact variant for meta rows (recording detail) vs the record
		 * form's full-size field. */
		dense?: boolean;
		/** In-progress input text; bindable so the parent can flush it
		 * before an action (see the record page's beginRecording). */
		draft?: string;
	} = $props();

	function commitDraft(): void {
		if (draft.trim().length === 0) return;
		const next = mergeDraftTags(tags, draft);
		draft = '';
		if (next.length !== tags.length) onChange(next);
	}

	function removeAt(index: number): void {
		onChange(tags.filter((_, i) => i !== index));
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' || event.key === ',') {
			event.preventDefault();
			commitDraft();
			return;
		}
		// Backspace on an empty draft pops the last chip — the standard
		// chips-input ergonomics so the user is not trapped behind a chip.
		if (event.key === 'Backspace' && draft === '' && tags.length > 0) {
			event.preventDefault();
			removeAt(tags.length - 1);
		}
	}
</script>

<div class="tags-input" class:dense role="group" aria-label="Recording tags">
	{#each tags as tag, index (tag)}
		<span class="tag-chip">
			<span class="tag-chip-text">{tag}</span>
			<button
				type="button"
				class="tag-chip-remove"
				aria-label={`Remove tag ${tag}`}
				{disabled}
				onclick={() => removeAt(index)}
			>
				<Icon name="close" size={10} />
			</button>
		</span>
	{/each}
	<input
		type="text"
		class="tags-draft"
		placeholder={tags.length === 0 ? placeholder : ''}
		bind:value={draft}
		onkeydown={onKeydown}
		onblur={commitDraft}
		{disabled}
		aria-label="Add tag"
	/>
</div>

<style>
	.tags-input {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
		min-height: 42px;
		padding: 6px 7px;
		border: 1px solid rgba(231, 214, 190, 0.18);
		border-radius: 3px;
		background: rgba(7, 6, 5, 0.58);
		transition: border-color 120ms ease, background 120ms ease;
	}
	.tags-input.dense {
		min-height: 32px;
		padding: 4px 6px;
		border-radius: 2px;
	}
	.tags-input:focus-within { border-color: var(--brass); background: rgba(7, 6, 5, 0.82); }
	.tag-chip {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 3px 4px 3px 8px;
		border: 1px solid rgba(215, 167, 71, 0.32);
		border-radius: 2px;
		background: rgba(215, 167, 71, 0.08);
		color: var(--brass);
		font-size: 11px;
		font-weight: 650;
		line-height: 1;
	}
	.dense .tag-chip { gap: 3px; padding: 2px 3px 2px 7px; font-size: 10px; }
	.tag-chip-text { white-space: nowrap; }
	.tag-chip-remove {
		display: grid;
		place-items: center;
		width: 16px;
		height: 16px;
		padding: 0;
		border: 0;
		border-radius: 1px;
		background: transparent;
		color: var(--brass);
		cursor: pointer;
		line-height: 0;
		opacity: 0.7;
	}
	.dense .tag-chip-remove { width: 14px; height: 14px; }
	.tag-chip-remove:hover:not(:disabled) { opacity: 1; background: rgba(213, 45, 36, 0.18); color: #ff8b7c; }
	.tag-chip-remove:disabled { cursor: default; }
	.tags-draft {
		flex: 1 1 80px;
		min-width: 80px;
		min-height: 26px;
		padding: 2px 4px;
		border: 0;
		background: transparent;
		color: var(--bone);
		font-size: 12px;
	}
	.dense .tags-draft { flex-basis: 60px; min-width: 60px; min-height: 22px; font-size: 11px; }
	.tags-draft::placeholder { color: #665f58; }
	.tags-draft:focus { outline: none; }
</style>
