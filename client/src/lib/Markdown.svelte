<script lang="ts">
	import { marked } from 'marked';
	import DOMPurify from 'dompurify';

	let { text }: { text: string } = $props();

	// Artifact content is machine-generated (STT / LLM summary) but can still
	// carry raw HTML — sanitize before it reaches {@html}. An explicit allowlist
	// (not a deny-list) keeps only the tags our artifacts actually produce:
	// the webview has no opener plugin, so a clicked <a href> would navigate
	// the app window away, and any media/style vector (img, video, source,
	// poster, background…) would be a network egress channel for local-only
	// content. Stripped tags degrade to their inner text; image alt text and
	// link URLs are dropped by design.
	const html = $derived(
		DOMPurify.sanitize(marked.parse(text, { async: false }), {
			ALLOWED_TAGS: [
				'p', 'br', 'strong', 'em', 'b', 'i', 'u', 's', 'del',
				'ul', 'ol', 'li', 'code', 'pre', 'blockquote',
				'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr',
				'table', 'thead', 'tbody', 'tr', 'th', 'td',
				'span', 'sup', 'sub'
			],
			ALLOWED_ATTR: [],
			// data-*/aria-* bypass ALLOWED_ATTR by default — disable explicitly.
			ALLOW_DATA_ATTR: false,
			ALLOW_ARIA_ATTR: false
		})
	);
</script>

<div class="markdown-body">{@html html}</div>

<style>
	.markdown-body {
		flex: 1;
		min-height: 0;
		margin: 0;
		padding: 12px;
		overflow: auto;
		overflow-wrap: anywhere;
		color: #c7bbad;
		font-size: 12px;
		line-height: 1.6;
		scrollbar-width: thin;
		scrollbar-color: var(--red-dark) transparent;
	}
	.markdown-body > :global(:first-child) { margin-top: 0; }
	.markdown-body > :global(:last-child) { margin-bottom: 0; }
	.markdown-body :global(h1),
	.markdown-body :global(h2),
	.markdown-body :global(h3),
	.markdown-body :global(h4) {
		margin: 14px 0 6px;
		color: var(--bone);
		font-weight: 700;
		line-height: 1.35;
	}
	.markdown-body :global(h1) { font-size: 14px; padding-bottom: 5px; border-bottom: 1px solid var(--line); }
	.markdown-body :global(h2) { font-size: 13px; }
	.markdown-body :global(h3),
	.markdown-body :global(h4) { font-size: 12px; }
	.markdown-body :global(p) { margin: 7px 0; }
	.markdown-body :global(strong) { color: var(--bone); font-weight: 650; }
	.markdown-body :global(em) { color: #d9cfbe; }
	.markdown-body :global(ul),
	.markdown-body :global(ol) { margin: 7px 0; padding-left: 20px; }
	.markdown-body :global(li) { margin: 3px 0; }
	.markdown-body :global(li::marker) { color: var(--brass); }
	.markdown-body :global(code) {
		padding: 1px 4px;
		border: 1px solid var(--line);
		border-radius: 2px;
		background: rgba(0, 0, 0, 0.22);
		color: #d9cfbe;
		font: 11px/1.5 "SFMono-Regular", Consolas, monospace;
	}
	.markdown-body :global(pre) {
		margin: 8px 0;
		padding: 9px 10px;
		overflow-x: auto;
		border: 1px solid var(--line);
		border-radius: 2px;
		background: rgba(0, 0, 0, 0.22);
		scrollbar-width: thin;
		scrollbar-color: var(--red-dark) transparent;
	}
	.markdown-body :global(pre code) { padding: 0; border: 0; background: transparent; }
	.markdown-body :global(blockquote) {
		margin: 8px 0;
		padding: 2px 10px;
		border-left: 2px solid rgba(215, 167, 71, 0.4);
		color: var(--ash);
	}
	.markdown-body :global(hr) { margin: 12px 0; border: 0; border-top: 1px solid var(--line); }
	.markdown-body :global(table) { margin: 8px 0; border-collapse: collapse; font-size: 11px; }
	.markdown-body :global(th),
	.markdown-body :global(td) { padding: 4px 8px; border: 1px solid var(--line); text-align: left; }
	.markdown-body :global(th) { color: var(--bone); font-weight: 650; background: rgba(0, 0, 0, 0.18); }
</style>
