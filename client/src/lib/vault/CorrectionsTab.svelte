<script lang="ts">
	import { onMount } from 'svelte';
	import Icon from '$lib/Icon.svelte';
	import EmptyState from '$lib/EmptyState.svelte';
	import Skeleton from '$lib/Skeleton.svelte';
	import {
		fetchGraphEdits,
		loadApiConfig,
		retireGraphEdit,
		startFixPreview,
		pollFixPreview,
		startFixApply,
		pollFixApply,
		type GraphEditRow,
		type FixProposal,
		type FixOp
	} from '$lib/api.svelte';

	let {
		tag,
		onchanged
	}: {
		tag: string;
		onchanged: () => void;
	} = $props();

	let edits = $state<GraphEditRow[]>([]);
	let auditLoading = $state(true);
	let auditError = $state('');
	let retiringId = $state<number | null>(null);

	let instruction = $state('');
	let previewing = $state(false);
	let previewNote = $state('');
	let previewError = $state('');
	let proposal = $state<FixProposal | null>(null);
	let previewId = $state<string | null>(null);
	let applying = $state(false);
	let applyNote = $state('');
	let applyError = $state('');
	let applyingId = $state<string | null>(null);

	function _opDescription(op: FixOp): string {
		switch (op.op) {
			case 'update_event': return 'update event';
			case 'delete_event': return 'delete event';
			case 'update_entity': return 'update entity';
			case 'delete_entity': return 'delete entity';
			case 'create_relation': return 'create relation';
			case 'delete_relation': return 'delete relation';
			default: return String(op.op);
		}
	}

	function _opDetail(op: FixOp): string {
		if (op.op === 'update_event' || op.op === 'delete_event') return `${op.event_key ?? ''}`;
		if (op.op === 'update_entity' || op.op === 'delete_entity') return `${op.slug ?? ''}`;
		return `${op.from_slug ?? ''} → ${op.type ?? ''} → ${op.to_slug ?? ''}`;
	}

	async function refreshAudit(): Promise<void> {
		try {
			edits = await fetchGraphEdits(loadApiConfig(), tag);
			auditError = '';
		} catch (caught) {
			auditError = `Audit unavailable: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			auditLoading = false;
		}
	}

	async function retireRule(id: number): Promise<void> {
		if (retiringId !== null) return;
		retiringId = id;
		try {
			await retireGraphEdit(loadApiConfig(), tag, id);
			edits = edits.map((e) => (e.id === id ? { ...e, status: 'retired' as const } : e));
		} catch (caught) {
			// keep row state; retire failed — leave it as-is so the operator can retry
		} finally {
			retiringId = null;
		}
	}

	async function requestPreview(): Promise<void> {
		const text = instruction.trim();
		if (text.length < 3 || previewing || proposal) return;
		previewing = true;
		previewError = '';
		previewNote = '';
		try {
			const { workflow_id } = await startFixPreview(loadApiConfig(), tag, instruction.trim());
			previewId = workflow_id;
			const startedAt = Date.now();
			let settled = false;
			while (!settled) {
				await new Promise((r) => globalThis.setTimeout(r, 3_000));
				if (!previewId) return;
				const result = await pollFixPreview(loadApiConfig(), tag, previewId);
				if (result.state === 'running') {
					if (Date.now() - startedAt >= 150_000) {
						previewError = 'Preview timed out — the server may be slow. Retry in a bit.';
						settled = true;
					}
					continue;
				}
				settled = true;
				if (result.state === 'busy') {
					previewNote = 'Summarizer busy — retry shortly';
				} else if (result.state === 'ready') {
					proposal = result.proposal;
				} else if (result.state === 'failed' || result.state === 'unparseable' || result.state === 'invalid' || result.state === 'unknown') {
					previewError = `Preview failed: ${'detail' in result ? (result.detail ?? String(result.state)) : 'unavailable'}`;
				} else {
					previewError = `Preview failed: ${String(result.state)}`;
				}
			}
		} catch (caught) {
			previewError = `Preview failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			previewing = false;
		}
	}

	async function confirmApply(): Promise<void> {
		if (!proposal || applying) return;
		applying = true;
		applyNote = '';
		applyError = '';
		try {
			const { workflow_id } = await startFixApply(loadApiConfig(), tag, proposal, instruction.trim());
			applyingId = workflow_id;
			const startedAt = Date.now();
			let settled = false;
			while (!settled) {
				await new Promise((r) => globalThis.setTimeout(r, 2_000));
				if (!applyingId) return;
				const result = await pollFixApply(loadApiConfig(), tag, applyingId);
				if (result.state === 'running') {
					if (Date.now() - startedAt >= 120_000) {
						applyError = 'Apply timed out — nothing landed. Retry in a bit.';
						settled = true;
					}
					continue;
				}
				settled = true;
				if (result.state === 'ok') {
					applyNote = `Fix applied: ${result.applied} edit${result.applied === 1 ? '' : 's'} applied. Digest renewal queued.`;
					proposal = null;
					applyingId = null;
					onchanged();
				} else if (result.state === 'stale') {
					applyError = `The graph changed while your fix was being applied — nothing landed. ${result.rejections.map((r) => `#${r.op_index + 1}: ${r.reason}`).join('; ')}`;
				} else {
					applyError = `Apply failed: ${'detail' in result ? (result.detail ?? String(result.state)) : 'unavailable'}`;
				}
			}
		} catch (caught) {
			applyError = `Apply failed: ${caught instanceof Error ? caught.message : String(caught)}`;
		} finally {
			applying = false;
		}
	}

	onMount(() => {
		void refreshAudit();
	});
</script>

<section class="corrections-tab">
	<header class="corr-header">
		<span class="corr-title"><Icon name="shield" size={11} />Correct the record</span>
	</header>
	<form
		class="corr-form"
		onsubmit={(e) => {
			e.preventDefault();
			void requestPreview();
		}}
	>
		<textarea
			class="corr-input"
			aria-label="Describe what is wrong"
			placeholder="Describe what is wrong — e.g. Glennis didn't create the agent network, I did"
			bind:value={instruction}
			maxlength="500"
			rows="2"
			disabled={previewing || !!proposal}
		></textarea>
		<div class="corr-actions">
			<button
				class="corr-preview"
				type="submit"
				disabled={previewing || !!proposal || instruction.trim().length < 3}
			>
				{#if previewing}
					<Icon name="refresh" size={11} strokeWidth={1.6} /> Translating…
				{:else}
					Translate
				{/if}
			</button>
		</div>
	</form>
	{#if previewNote}
		<p class="corr-note">{previewNote}</p>
	{/if}
	{#if previewError}
		<p class="corr-error" role="alert">{previewError}</p>
	{/if}
	{#if proposal}
		<div class="corr-proposal">
			<strong class="corr-proposal-head">Proposal — confirm to apply</strong>
			<ul class="corr-ops">
				{#each proposal.ops as op, i (i)}
					<li class="corr-op">
						<strong>{_opDescription(op)}</strong>
						<span class="corr-op-detail">{_opDetail(op)}</span>
					</li>
				{/each}
			</ul>
			<div class="corr-proposal-actions">
				<button class="corr-apply" type="button" disabled={applying} onclick={() => void confirmApply()}>
					{#if applying}
						<Icon name="refresh" size={11} strokeWidth={1.6} /> Applying…
					{:else}
						Apply fix
					{/if}
				</button>
				<button
					class="corr-discard"
					type="button"
					onclick={() => {
						proposal = null;
						previewId = null;
						instruction = '';
					}}
				>
					Discard
				</button>
			</div>
		</div>
	{/if}
	{#if applyNote}
		<p class="corr-note">{applyNote}</p>
	{/if}
	{#if applyError}
		<p class="corr-error" role="alert">{applyError}</p>
	{/if}

	<header class="corr-header">
		<span class="corr-title"><Icon name="summary" size={11} />Corrections</span>
	</header>
	{#if auditLoading}
		<Skeleton variant="panel-tag" />
	{:else if auditError}
		<div class="corr-error">{auditError}</div>
	{:else if edits.length === 0}
		<EmptyState icon="summary" title="No corrections recorded yet" hint="The record is clean — edits appear here as they land." />
	{:else}
		<ul class="corr-list">
			{#each edits as edit (edit.id)}
				<li class="corr-row">
					<span class="corr-target">
						<strong>{edit.target}</strong>
						<em>{edit.op}</em>
						<span class="corr-obj">{edit.obj_key}</span>
					</span>
					<span class="corr-status" data-status={edit.status}>{edit.status}</span>
					{#if edit.feedback_text}
						<small class="corr-feedback">{edit.feedback_text}</small>
					{/if}
					{#if edit.status !== 'retired' && edit.status !== 'orphaned' && edit.feedback_text}
						<button class="corr-retire" type="button" disabled={retiringId === edit.id} onclick={() => void retireRule(edit.id)}>
							{retiringId === edit.id ? 'Retiring…' : 'Retire rule'}
						</button>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</section>

<style>
	.corrections-tab { display: grid; gap: 8px; padding: 8px 0 4px; }
	.corr-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 2px; }
	.corr-title { display: inline-flex; align-items: center; gap: 5px; color: var(--ash); font-size: 9px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
	.corr-form { display: grid; gap: 6px; }
	.corr-input { width: 100%; padding: 6px 9px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.3); color: var(--bone); font-size: 11px; font-family: inherit; resize: vertical; line-height: 1.45; min-height: 44px; }
	.corr-input:focus { outline: none; border-color: var(--brass); box-shadow: 0 0 0 1px var(--cyan); }
	.corr-actions { display: flex; gap: 6px; justify-content: flex-end; }
	.corr-preview { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.corr-preview:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.corr-preview:disabled { opacity: 0.6; cursor: default; }
	.corr-proposal { display: grid; gap: 6px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 2px; background: rgba(0,0,0,.22); box-shadow: inset 0 1px 3px rgba(0,0,0,.28); }
	.corr-proposal-head { color: var(--brass); font-size: 10px; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; }
	.corr-ops { margin: 0; padding-left: 12px; display: grid; gap: 4px; }
	.corr-op { color: #ded3c4; font-size: 11px; line-height: 1.4; }
	.corr-op-detail { color: var(--ash); font-size: 10px; }
	.corr-proposal-actions { display: flex; gap: 6px; margin-top: 6px; }
	.corr-apply { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; border: 1px solid var(--brass); border-radius: 2px; background: rgba(215,167,71,.12); color: var(--brass); font-size: 10px; font-weight: 700; cursor: pointer; }
	.corr-apply:hover:not(:disabled) { background: rgba(215,167,71,.2); }
	.corr-apply:disabled { opacity: 0.6; cursor: default; }
	.corr-discard { padding: 4px 10px; border: 1px solid var(--line); background: transparent; color: var(--ash); cursor: pointer; }
	.corr-note { margin: 0; padding: 0 2px; color: var(--ash); font-size: 10px; }
	.corr-error { margin: 0; padding: 0 2px; color: #f36b60; font-size: 10px; }
	.corr-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
	.corr-row { display: flex; align-items: baseline; gap: 7px; padding: 7px 2px; border-bottom: 1px solid var(--line); font-size: 11px; }
	.corr-target { flex: 1; min-width: 0; display: flex; gap: 6px; align-items: baseline; color: #ded3c4; }
	.corr-target strong { color: var(--bone); }
	.corr-target em { color: var(--ash); font-size: 9px; text-transform: uppercase; letter-spacing: 0.03em; }
	.corr-obj { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.corr-status { font-size: 9px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ash); }
	.corr-status[data-status='applied'] { color: var(--cyan); }
	.corr-status[data-status='orphaned'] { color: #f36b60; }
	.corr-status[data-status='retired'] { color: #8b8278; }
	.corr-feedback { margin: 0; font-size: 10px; color: #9a8f82; overflow-wrap: anywhere; }
	.corr-feedback::before { content: '“'; }
	.corr-feedback::after { content: '”'; }
</style>
