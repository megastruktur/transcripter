<script lang="ts">
	import Icon from './Icon.svelte';
	import { mergeDraftTags, type TagSuggestion } from './tags';

	let {
		tags,
		onChange,
		disabled = false,
		placeholder = 'Add tag',
		dense = false,
		draft = $bindable(''),
		suggestions = []
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
		/** Server-known tags (from profiles). Selecting one adds the chip;
		 * free-form entry stays legal for tags no profile claims. */
		suggestions?: TagSuggestion[];
	} = $props();

	let open = $state(false);
	let highlighted = $state(0);
	let inputEl: HTMLInputElement | null = null;
	/** Viewport coords for the portaled dropdown (position:fixed escapes
	 * overflow-clipping ancestors; recomputed on open/scroll/resize). */
	let popStyle = $state('');

	const MAX_ROWS = 8;

	let filtered = $derived.by(() => {
		const needle = draft.trim().toLowerCase();
		const pool = suggestions.filter((s) => !tags.includes(s.tag));
		if (needle.length === 0) return pool.slice(0, MAX_ROWS);
		return pool
			.filter(
				(s) =>
					s.tag.includes(needle) ||
					s.profiles.some((p) => p.toLowerCase().includes(needle)) ||
					s.description.toLowerCase().includes(needle)
			)
			.slice(0, MAX_ROWS);
	});

	function commitDraft(): void {
		if (draft.trim().length === 0) return;
		const next = mergeDraftTags(tags, draft);
		draft = '';
		if (next.length !== tags.length) onChange(next);
	}

	function removeAt(index: number): void {
		onChange(tags.filter((_, i) => i !== index));
	}

	function choose(suggestion: TagSuggestion): void {
		if (!tags.includes(suggestion.tag)) onChange([...tags, suggestion.tag]);
		draft = '';
		highlighted = 0;
		inputEl?.focus();
	}

	function updatePopPosition(): void {
		if (!inputEl) return;
		const rect = inputEl.getBoundingClientRect();
		// 4px gap below the field; width follows the chips container, not
		// the (flex-shrunk) input alone.
		const parent = inputEl.closest('.tags-input');
		const box = (parent ?? inputEl).getBoundingClientRect();
		popStyle = `left:${box.left}px;top:${rect.bottom + 4}px;width:${box.width}px;`;
	}

	function openList(): void {
		if (disabled || filtered.length === 0) return;
		updatePopPosition();
		open = true;
	}

	function closeList(): void {
		open = false;
		highlighted = 0;
	}

	function onKeydown(event: KeyboardEvent): void {
		if (open && filtered.length > 0) {
			if (event.key === 'ArrowDown') {
				event.preventDefault();
				highlighted = (highlighted + 1) % filtered.length;
				return;
			}
			if (event.key === 'ArrowUp') {
				event.preventDefault();
				highlighted = (highlighted - 1 + filtered.length) % filtered.length;
				return;
			}
			if (event.key === 'Escape') {
				event.preventDefault();
				closeList();
				return;
			}
			if (event.key === 'Enter') {
				event.preventDefault();
				choose(filtered[highlighted]);
				return;
			}
		}
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

	$effect(() => {
		// The list changed under the highlight (typing narrows it, choosing
		// removes a row) — clamp instead of pointing past the end.
		if (highlighted >= filtered.length) highlighted = 0;
	});

	$effect(() => {
		if (!open) return;
		const reposition = () => updatePopPosition();
		window.addEventListener('scroll', reposition, true);
		window.addEventListener('resize', reposition);
		return () => {
			window.removeEventListener('scroll', reposition, true);
			window.removeEventListener('resize', reposition);
		};
	});

	/** Moves the dropdown node to <body> so no ancestor's overflow (or
	 * stacking context) can clip it. */
	function portal(node: HTMLElement): { destroy(): void } {
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
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
		bind:this={inputEl}
		onkeydown={onKeydown}
		onfocus={openList}
		oninput={openList}
		onblur={() => {
			commitDraft();
			closeList();
		}}
		{disabled}
		aria-label="Add tag"
		role="combobox"
		aria-expanded={open}
		aria-controls="tag-suggest-list"
		aria-activedescendant={open && filtered.length > 0 ? `tag-suggest-${filtered[highlighted].tag}` : undefined}
		aria-autocomplete="list"
	/>
</div>

{#if open && filtered.length > 0}
	<div class="tag-suggest-pop" style={popStyle} use:portal>
		<ul id="tag-suggest-list" role="listbox" aria-label="Available tag logics">
			{#each filtered as suggestion, i (suggestion.tag)}
				<li
					id={`tag-suggest-${suggestion.tag}`}
					role="option"
					aria-selected={i === highlighted}
					class:highlighted={i === highlighted}
					onmousedown={(event) => {
						// mousedown + preventDefault: fires before the input's
						// blur, so the click survives the list closing.
						event.preventDefault();
						choose(suggestion);
					}}
				>
					<span class="suggest-tag">{suggestion.tag}</span>
					<span class="suggest-profile">
						{suggestion.profiles.join(', ')}
						{#if suggestion.hasEnrich}<span class="suggest-kg">graph</span>{/if}
					</span>
					{#if suggestion.description}
						<span class="suggest-desc">{suggestion.description}</span>
					{/if}
				</li>
			{/each}
		</ul>
	</div>
{/if}

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

	.tag-suggest-pop {
		position: fixed;
		z-index: 1000;
		max-height: 240px;
		overflow-y: auto;
		border: 1px solid rgba(231, 214, 190, 0.22);
		border-radius: 3px;
		background: #141210;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}
	.tag-suggest-pop ul {
		margin: 0;
		padding: 3px;
		list-style: none;
	}
	.tag-suggest-pop li {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 6px 8px;
		border-radius: 2px;
		cursor: pointer;
	}
	.tag-suggest-pop li.highlighted { background: rgba(215, 167, 71, 0.12); }
	.suggest-tag {
		color: var(--brass);
		font-size: 12px;
		font-weight: 650;
		line-height: 1.2;
	}
	.suggest-profile {
		color: var(--bone);
		font-size: 10px;
		opacity: 0.75;
		line-height: 1.2;
	}
	.suggest-kg {
		margin-left: 6px;
		padding: 1px 5px;
		border: 1px solid rgba(231, 214, 190, 0.25);
		border-radius: 2px;
		font-size: 9px;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		opacity: 0.8;
	}
	.suggest-desc {
		color: #8f867c;
		font-size: 10px;
		line-height: 1.3;
		display: -webkit-box;
		line-clamp: 2;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
</style>
