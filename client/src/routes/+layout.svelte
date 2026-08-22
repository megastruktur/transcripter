<script lang="ts">
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { LogicalSize } from '@tauri-apps/api/dpi';
	import { getCurrentWindow } from '@tauri-apps/api/window';
	import { checkServerConnection, connection, preflight, recorder, uploads } from '$lib/stores.svelte';
	import Icon from '$lib/Icon.svelte';

	let { children } = $props();
	let collapsed = $state(browser && localStorage.getItem('transcripter.window-collapsed') === 'true');
	let resizing = $state(false);
	let dragOrigin: { x: number; y: number } | null = null;
	let draggedCollapsedMark = $state(false);

	const navItems = [
		{ href: '/', label: 'Record', icon: 'record' },
		{ href: '/recordings', label: 'Library', icon: 'library' },
		{ href: '/settings', label: 'Settings', icon: 'settings' }
	] as const;

	const pendingUploads = $derived(Object.keys(uploads).length);
	const audioStatus = $derived(
		recorder.recording
			? 'Recording'
			: !preflight.current
				? 'Audio not checked'
				: preflight.current.error || !preflight.current.mic_device_present || preflight.current.mic_signal === false
					? 'Audio needs attention'
					: 'Audio ready'
	);
	const serverStatus = $derived(
		connection.phase === 'checking'
			? 'Checking server'
			: connection.phase === 'connected'
				? 'Server connected'
				: connection.phase === 'unavailable'
					? 'Server unavailable'
					: 'Server not configured'
	);
	const serverTone = $derived(
		connection.phase === 'connected' ? 'ready' : connection.phase === 'checking' ? 'issue' : connection.phase === 'unavailable' ? 'unavailable' : 'idle'
	);
	const collapsedStatus = $derived(
		`${audioStatus} · ${serverStatus}${pendingUploads ? ` · ${pendingUploads} pending ${pendingUploads === 1 ? 'upload' : 'uploads'}` : ''}`
	);
	const routeName = $derived(
		page.url.pathname === '/' ? 'Recorder' : page.url.pathname === '/recordings' ? 'Recordings' : 'Settings'
	);
	onMount(async () => {
		void checkServerConnection();
		if (!isTauri()) return;
		try {
			const appWindow = getCurrentWindow();
			const [physicalSize, scaleFactor] = await Promise.all([appWindow.innerSize(), appWindow.scaleFactor()]);
			const logicalSize = physicalSize.toLogical(scaleFactor);
			collapsed = logicalSize.width <= 100 && logicalSize.height <= 100;
			localStorage.setItem('transcripter.window-collapsed', String(collapsed));
		} catch {
			// The persisted value remains the fallback if native size inspection fails.
		}
	});


	function isTauri(): boolean {
		return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
	}

	async function resizeWindow(width: number, height: number): Promise<void> {
		if (!isTauri()) return;
		await getCurrentWindow().setSize(new LogicalSize(width, height));
	}

	async function toggleCollapsed(): Promise<void> {
		if (resizing) return;
		resizing = true;
		collapsed = !collapsed;
		localStorage.setItem('transcripter.window-collapsed', String(collapsed));
		try {
			await resizeWindow(collapsed ? 76 : 440, collapsed ? 76 : 720);
		} finally {
			resizing = false;
		}
	}

	async function minimizeWindow(): Promise<void> {
		if (isTauri()) await getCurrentWindow().minimize();
	}

	async function closeWindow(): Promise<void> {
		if (isTauri()) await getCurrentWindow().close();
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape' && !collapsed) toggleCollapsed();
	}

	function beginCollapsedDrag(event: PointerEvent): void {
		if (event.button !== 0) return;
		dragOrigin = { x: event.screenX, y: event.screenY };
		draggedCollapsedMark = false;
	}

	async function moveCollapsedDrag(event: PointerEvent): Promise<void> {
		if (!dragOrigin || draggedCollapsedMark) return;
		if (Math.hypot(event.screenX - dragOrigin.x, event.screenY - dragOrigin.y) < 5) return;
		draggedCollapsedMark = true;
		if (isTauri()) await getCurrentWindow().startDragging();
	}

	function endCollapsedDrag(): void {
		dragOrigin = null;
	}

	async function activateCollapsedMark(): Promise<void> {
		if (draggedCollapsedMark) {
			draggedCollapsedMark = false;
			return;
		}
		await toggleCollapsed();
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<svelte:head>
	<meta name="theme-color" content="#160f0d" />
</svelte:head>

{#if collapsed}
	<button
		class:recording={recorder.recording}
		class:dragging={draggedCollapsedMark}
		class="collapsed-mark"
		type="button"
		onpointerdown={beginCollapsedDrag}
		onpointermove={moveCollapsedDrag}
		onpointerup={endCollapsedDrag}
		onpointercancel={endCollapsedDrag}
		onclick={activateCollapsedMark}
		aria-label={`Expand Transcripter. ${collapsedStatus}`}
		title={collapsedStatus}
	>
		<span class="collapsed-icon" aria-hidden="true"><Icon name="mark" size={62} /></span>
		<span class="collapsed-state"></span>
	</button>
{:else}
	<div class="app-shell">
		<header class="titlebar" data-tauri-drag-region>
			<div class="identity" data-tauri-drag-region>
				<div class="mini-cog" aria-hidden="true"><Icon name="mark" size={25} /></div>
				<div data-tauri-drag-region>
					<strong data-tauri-drag-region>TRANSCRIPTER</strong>
					<small data-tauri-drag-region>Always on top</small>
				</div>
			</div>
			<div class="window-actions">
				<button type="button" onclick={toggleCollapsed} aria-label="Collapse to symbol" title="Collapse to symbol"><Icon name="collapse" size={16} /></button>
				<button type="button" onclick={minimizeWindow} aria-label="Minimize window" title="Minimize"><Icon name="minimize" size={16} /></button>
				<button class="close" type="button" onclick={closeWindow} aria-label="Close window" title="Close"><Icon name="close" size={16} /></button>
			</div>
		</header>

		<div class="hazard-rule" aria-hidden="true"></div>

		<div class="shell-body">
			<nav class="rail" aria-label="Primary navigation">
				{#each navItems as item (item.href)}

					<a href={item.href} aria-current={page.url.pathname === item.href ? 'page' : undefined} title={item.label}>
						<span class="nav-icon" aria-hidden="true"><Icon name={item.icon} size={20} /></span>
						<span>{item.label}</span>
					</a>
				{/each}
				<div class="rail-spacer"></div>
			</nav>

			<main class="workspace">
				<div class="context-bar">
					<span class="context-name">{routeName}</span>
					<span class:ready={serverTone === 'ready'} class:issue={serverTone === 'issue'} class:unavailable={serverTone === 'unavailable'} class="status-lamp" aria-hidden="true"></span>
				</div>
				<div class="page-scroll">
					{@render children()}
				</div>
			</main>
		</div>

		<footer class="status-strip">
			<span><i class:ready={serverTone === 'ready'} class:issue={serverTone === 'issue'} class:unavailable={serverTone === 'unavailable'}></i>{serverStatus}</span>
			<span>{pendingUploads ? `${pendingUploads} pending uploads` : 'No pending uploads'}</span>
		</footer>
	</div>
{/if}

<style>
	:global(*) { box-sizing: border-box; }
	:global(:root) {
		font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
		color: #e9dfcf;
		background: transparent;
		font-synthesis: none;
		--void: #0b0908;
		--iron: #171311;
		--iron-raised: #201a17;
		--bone: #e9dfcf;
		--ash: #9e9183;
		--red: #d52d24;
		--red-dark: #6f1715;
		--brass: #d7a747;
		--cyan: #70d7d0;
		--line: rgba(231, 214, 190, 0.14);
	}
	:global(html), :global(body) { margin: 0; min-width: 100%; min-height: 100%; background: rgba(0, 0, 0, 0) !important; overflow: hidden; }
	:global(body) { padding: 0; -webkit-font-smoothing: antialiased; }
	:global(button), :global(input), :global(select) { font: inherit; }
	:global(button), :global(a) { -webkit-tap-highlight-color: transparent; }
	:global(:focus-visible) { outline: 2px solid var(--cyan); outline-offset: 2px; }

	.app-shell {
		width: 100vw;
		height: 100vh;
		min-height: 560px;
		display: grid;
		grid-template-rows: 54px 4px 1fr 28px;
		background: radial-gradient(circle at 84% 5%, rgba(213, 45, 36, 0.14), transparent 28%), linear-gradient(145deg, rgba(255,255,255,0.025), transparent 24%), var(--iron);
		border: 1px solid rgba(215, 167, 71, 0.36);
		box-shadow: 0 26px 64px rgba(0, 0, 0, 0.58), inset 0 0 0 1px rgba(0, 0, 0, 0.8);
		position: relative;
		overflow: hidden;
	}
	.app-shell::after { content: ''; position: absolute; inset: 0; pointer-events: none; opacity: 0.22; background-image: repeating-linear-gradient(0deg, transparent 0 3px, rgba(255, 255, 255, 0.018) 3px 4px); mix-blend-mode: screen; }
	.titlebar { display: flex; align-items: center; justify-content: space-between; padding: 0 8px 0 14px; background: linear-gradient(90deg, #100d0b 0%, #221714 62%, #2d1311 100%); border-bottom: 1px solid rgba(215, 167, 71, 0.22); user-select: none; position: relative; z-index: 2; }
	.identity { display: flex; align-items: center; gap: 10px; min-width: 0; }
	.identity strong { display: block; font-size: 15px; font-weight: 750; letter-spacing: 0.1em; line-height: 1.05; }
	.identity small { display: block; margin-top: 3px; font-size: 10px; color: var(--ash); }
	.mini-cog { width: 29px; height: 29px; display: grid; place-items: center; filter: drop-shadow(0 2px 4px rgba(0, 0, 0, .45)); line-height: 0; }
	.window-actions { display: flex; gap: 3px; }
	.window-actions button { width: 27px; height: 27px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: rgba(0, 0, 0, 0.24); color: var(--ash); cursor: pointer; line-height: 0; transition: color 120ms ease, border-color 120ms ease, background 120ms ease; }
	.window-actions button:hover { color: var(--bone); border-color: rgba(215, 167, 71, 0.55); background: rgba(215, 167, 71, 0.08); }
	.window-actions .close:hover { color: white; border-color: var(--red); background: var(--red-dark); }
	.hazard-rule { background: repeating-linear-gradient(120deg, var(--brass) 0 8px, #17110b 8px 16px); opacity: 0.72; z-index: 2; }
	.shell-body { display: grid; grid-template-columns: 80px minmax(0, 1fr); min-height: 0; position: relative; z-index: 1; }
	.rail { display: flex; flex-direction: column; align-items: stretch; gap: 5px; padding: 10px 7px 8px; background: rgba(8, 7, 6, 0.65); border-right: 1px solid var(--line); }
	.rail a { display: grid; place-items: center; gap: 5px; min-height: 66px; color: #8e857c; text-decoration: none; font-size: 10px; font-weight: 650; letter-spacing: 0.02em; border: 1px solid transparent; border-radius: 3px; transition: color 130ms ease, background 130ms ease, border-color 130ms ease; }
	.rail a:hover { color: var(--bone); background: rgba(255, 255, 255, 0.025); }
	.rail a[aria-current='page'] { color: var(--brass); background: linear-gradient(90deg, rgba(213, 45, 36, 0.18), rgba(215, 167, 71, 0.04)); border-color: rgba(215, 167, 71, 0.28); box-shadow: inset 2px 0 var(--red); }
	.nav-icon { width: 28px; height: 28px; display: grid; place-items: center; line-height: 0; }
	.rail-spacer { flex: 1; }
	.workspace { display: grid; grid-template-rows: 42px minmax(0, 1fr); min-width: 0; min-height: 0; }
	.context-bar { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 10px; padding: 0 16px; border-bottom: 1px solid var(--line); background: rgba(0, 0, 0, 0.1); }
	.context-name { font-size: 14px; font-weight: 650; color: #c8bbaa; }
	.status-lamp, .status-strip i { width: 6px; height: 6px; border-radius: 50%; background: #6b655e; box-shadow: 0 0 0 2px rgba(107, 101, 94, 0.12); }
	.status-lamp.unavailable, .status-strip i.unavailable { background: var(--red); box-shadow: 0 0 0 3px rgba(213, 45, 36, 0.12), 0 0 12px rgba(213, 45, 36, 0.8); }
	.status-lamp.ready, .status-strip i.ready { background: var(--cyan); box-shadow: 0 0 0 3px rgba(112, 215, 208, 0.1), 0 0 10px rgba(112, 215, 208, 0.65); }
	.status-lamp.issue, .status-strip i.issue { background: var(--brass); box-shadow: 0 0 0 3px rgba(215, 167, 71, 0.1), 0 0 10px rgba(215, 167, 71, 0.55); }
	.page-scroll { min-height: 0; overflow: auto; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.status-strip { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 14px; padding: 0 12px; border-top: 1px solid rgba(215, 167, 71, 0.16); background: #0e0b0a; color: #8f857b; font-size: 10px; position: relative; z-index: 2; }
	.status-strip span:first-child { display: flex; align-items: center; gap: 7px; }

	.collapsed-mark { width: 76px; height: 76px; margin: 0; padding: 7px; border: 0; border-radius: 50%; background: transparent; box-shadow: none; cursor: grab; position: relative; overflow: hidden; transition: transform 180ms ease; touch-action: none; }
	.collapsed-icon { width: 62px; height: 62px; display: grid; place-items: center; transition: transform 180ms ease; line-height: 0; }
	.collapsed-state { position: absolute; right: 7px; bottom: 8px; width: 9px; height: 9px; border: 2px solid #100d0b; border-radius: 50%; background: #7c756d; }
	.collapsed-mark.recording .collapsed-state { background: var(--red); box-shadow: none; }
	.collapsed-mark:hover .collapsed-icon { transform: rotate(9deg); }
	.collapsed-mark.dragging { cursor: grabbing; }
	.collapsed-mark.dragging .collapsed-icon { transform: scale(0.96); }

	:global(.page) { padding: 18px 18px 24px; }
	:global(.page-title) { margin: 0; font-size: 30px; font-weight: 760; line-height: 1.05; letter-spacing: -0.035em; color: var(--bone); }
	:global(.panel) { background: linear-gradient(145deg, rgba(255,255,255,0.026), rgba(0,0,0,0.08)); border: 1px solid var(--line); border-radius: 4px; }
	:global(.field-label) { display: block; margin-bottom: 8px; font-size: 11px; font-weight: 650; color: #b8ac9d; }
	:global(input), :global(select) { width: 100%; min-height: 42px; padding: 0 12px; border: 1px solid rgba(231, 214, 190, 0.18); border-radius: 3px; background: rgba(7, 6, 5, 0.58); color: var(--bone); font-size: 13px; transition: border-color 120ms ease, background 120ms ease; }
	:global(input::placeholder) { color: #665f58; }
	:global(input:focus), :global(select:focus) { border-color: var(--brass); background: rgba(7, 6, 5, 0.82); outline: none; }
	:global(button:disabled) { cursor: not-allowed; opacity: 0.5; }

	@media (prefers-reduced-motion: reduce) {
		*, *::before, *::after { scroll-behavior: auto !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
	}
</style>
