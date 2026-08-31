<script lang="ts">
	import Icon from './Icon.svelte';
	import { mergeDraftTags, normalizeTag, type TagSuggestion } from './tags';

	/** Chip-only tag editor for the recording detail meta row: tags render as
	 * buttons, "+" opens a draft input with the same server-known suggestions
	 * dropdown TagChips uses, and clicking a chip opens a small Edit/Remove
	 * menu. The record/import forms keep the always-visible TagChips input. */
	let {
		tags,
		onChange,
		suggestions = []
	}: {
		tags: string[];
		onChange: (next: string[]) => void;
		suggestions?: TagSuggestion[];
	} = $props();

	let adding = $state(false);
	let draft = $state('');
	let editing = $state<number | null>(null);
	let editDraft = $state('');
	let menuFor = $state<string | null>(null);
	let open = $state(false);
	let highlighted = $state(0);
	let inputEl = $state<HTMLInputElement>();
	let wrapEl = $state<HTMLDivElement>();
	/** Viewport coords for the portaled suggestions dropdown (position:fixed
	 * escapes overflow-clipping ancestors; recomputed on open/scroll/resize). */
	let popStyle = $state('');

	const MAX_ROWS = 8;

	let filtered = $derived.by(() => {
		const needle = draft.trim().toLowerCase();
		const pool = suggestions.filter((s) => !tags.includes(s.tag));
		if (needle.length === 0) return pool.slice(0, MAX_ROWS);
		return pool.filter((s) => s.tag.includes(needle)).slice(0, MAX_ROWS);
	});

	function autofocus(node: HTMLElement): void {
		node.focus();
	}

	function startAdd(): void {
		adding = true;
		draft = '';
		menuFor = null;
		editing = null;
	}

	function closeAdd(): void {
		adding = false;
		draft = '';
		closeList();
	}

	function commitDraft(): void {
		if (draft.trim().length === 0) return;
		const next = mergeDraftTags(tags, draft);
		draft = '';
		if (next.length !== tags.length) onChange(next);
	}

	function choose(suggestion: TagSuggestion): void {
		if (!tags.includes(suggestion.tag)) onChange([...tags, suggestion.tag]);
		draft = '';
		highlighted = 0;
		inputEl?.focus();
	}

	function startEdit(index: number): void {
		editing = index;
		editDraft = tags[index];
		menuFor = null;
		adding = false;
	}

	function cancelEdit(): void {
		editing = null;
		editDraft = '';
	}

	/** Enter/blur commits the rename; an empty draft removes the tag. The
	 * result is normalized and deduped exactly like mergeDraftTags does. */
	function commitEdit(): void {
		if (editing === null) return;
		const norm = normalizeTag(editDraft);
		const next = tags.map((tag, i) => (i === editing ? (norm ?? '') : tag)).filter((tag) => tag.length > 0);
		const deduped = next.filter((tag, i) => next.indexOf(tag) === i);
		editing = null;
		editDraft = '';
		if (deduped.length !== tags.length || deduped.some((tag, i) => tag !== tags[i])) onChange(deduped);
	}

	function removeAt(index: number): void {
		menuFor = null;
		onChange(tags.filter((_, i) => i !== index));
	}

	function updatePopPosition(): void {
		if (!inputEl) return;
		const rect = inputEl.getBoundingClientRect();
		popStyle = `left:${rect.left}px;top:${rect.bottom + 4}px;width:${Math.max(rect.width, 180)}px;`;
	}

	function openList(): void {
		if (filtered.length === 0) return;
		updatePopPosition();
		open = true;
	}

	function closeList(): void {
		open = false;
		highlighted = 0;
	}

	function onAddKeydown(event: KeyboardEvent): void {
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
		if (event.key === 'Escape') {
			event.stopPropagation();
			event.preventDefault();
			closeAdd();
		}
	}

	function onEditKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			commitEdit();
			return;
		}
		if (event.key === 'Escape') {
			event.stopPropagation();
			event.preventDefault();
			cancelEdit();
		}
	}

	/** Capture phase: an open chip menu swallows Escape before the layout's
	 * bubble-phase window handler can collapse the whole app shell. */
	function onWindowKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || menuFor === null) return;
		event.stopPropagation();
		menuFor = null;
	}

	function onWindowPointerDown(event: PointerEvent): void {
		if (menuFor === null || !wrapEl) return;
		if (event.target instanceof Node && wrapEl.contains(event.target)) return;
		menuFor = null;
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

<svelte:window onkeydowncapture={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<div class="tag-editor" role="group" aria-label="Recording tags" bind:this={wrapEl}>
	{#each tags as tag, index (tag)}
		{#if editing === index}
			<input
				class="tag-edit-input"
				type="text"
				bind:value={editDraft}
				aria-label="Edit tag"
				{@attach autofocus}
				onkeydown={onEditKeydown}
				onblur={commitEdit}
			/>
		{:else}
			<span class="tag-wrap">
				<button
					type="button"
					class="tag-chip"
					title={tag}
					aria-haspopup="menu"
					aria-expanded={menuFor === tag}
					onclick={() => (menuFor = menuFor === tag ? null : tag)}
				>
					{tag}
				</button>
				{#if menuFor === tag}
					<span class="chip-menu" role="menu" aria-label="Tag actions">
						<button type="button" role="menuitem" onclick={() => startEdit(index)}>Edit</button>
						<button type="button" role="menuitem" class="chip-menu-danger" onclick={() => removeAt(index)}>Remove</button>
					</span>
				{/if}
			</span>
		{/if}
	{/each}
	{#if adding}
		<input
			type="text"
			class="tag-add-input"
			placeholder="Add tag"
			aria-label="Add tag"
			role="combobox"
			aria-expanded={open}
			aria-controls="tag-editor-suggest"
			aria-activedescendant={open && filtered.length > 0 ? `tag-editor-suggest-${filtered[highlighted].tag}` : undefined}
			aria-autocomplete="list"
			bind:value={draft}
			bind:this={inputEl}
			{@attach autofocus}
			onkeydown={onAddKeydown}
			onfocus={openList}
			oninput={openList}
			onblur={() => {
				commitDraft();
				closeAdd();
			}}
		/>
	{:else}
		<button type="button" class="tag-add" aria-label="Add tag" title="Add tag" onclick={startAdd}>
			<Icon name="plus" size={11} />
		</button>
	{/if}
</div>

{#if open && filtered.length > 0}
	<div class="tag-suggest-pop" style={popStyle} use:portal>
		<ul id="tag-editor-suggest" role="listbox" aria-label="Recent freehand tags">
			{#each filtered as suggestion, i (suggestion.tag)}
				<li
					id={`tag-editor-suggest-${suggestion.tag}`}
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
					<span class="suggest-count">
						{suggestion.recent ? 'recent' : `×${suggestion.count}`}
					</span>
				</li>
			{/each}
		</ul>
	</div>
{/if}

<style>
	.tag-editor {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
		min-height: 26px;
	}
	.tag-wrap { position: relative; display: inline-flex; }
	.tag-chip {
		max-width: 180px;
		padding: 3px 8px;
		border: 0;
		border-radius: 2px;
		background: rgba(215, 167, 71, 0.08);
		color: var(--brass);
		font-size: 10px;
		font-weight: 650;
		line-height: 1.2;
		cursor: pointer;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		transition: background 120ms ease;
	}
	.tag-chip:hover, .tag-chip[aria-expanded='true'] { background: rgba(215, 167, 71, 0.16); }
	.chip-menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		z-index: 30;
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 92px;
		padding: 3px;
		background: #14100e;
		border: 1px solid rgba(215, 167, 71, 0.28);
		border-radius: 3px;
		box-shadow: 0 10px 24px rgba(0, 0, 0, 0.55);
	}
	.chip-menu button {
		width: 100%;
		min-height: 24px;
		padding: 0 8px;
		border: 0;
		border-radius: 2px;
		background: transparent;
		color: #c8bbaa;
		font-size: 10px;
		text-align: left;
		cursor: pointer;
		white-space: nowrap;
	}
	.chip-menu button:hover { background: rgba(215, 167, 71, 0.1); color: var(--bone); }
	.chip-menu button.chip-menu-danger { color: #f36b60; }
	.chip-menu button.chip-menu-danger:hover { background: rgba(213, 45, 36, 0.12); color: #f36b60; }
	.tag-add {
		width: 22px;
		height: 22px;
		display: grid;
		place-items: center;
		padding: 0;
		border: 1px dashed rgba(215, 167, 71, 0.35);
		border-radius: 2px;
		background: transparent;
		color: #968d83;
		cursor: pointer;
		line-height: 0;
	}
	.tag-add:hover { border-color: var(--brass); color: var(--brass); background: rgba(215, 167, 71, 0.08); }
	.tag-add-input, .tag-edit-input {
		width: 120px;
		min-height: 22px;
		padding: 2px 6px;
		border: 1px solid rgba(215, 167, 71, 0.4);
		border-radius: 2px;
		background: rgba(0, 0, 0, 0.25);
		color: var(--bone);
		font-size: 11px;
	}
	.tag-edit-input { width: 100px; }
	.tag-add-input:focus, .tag-edit-input:focus { outline: none; border-color: var(--brass); }
	.tag-add-input::placeholder { color: #665f58; }

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
	.tag-suggest-pop li.highlighted { background: rgba(215, 167, 71, 0.1); }
	.suggest-tag { color: var(--bone); font-size: 11px; }
	.suggest-count { color: #8e857c; font-size: 9px; }
</style>
