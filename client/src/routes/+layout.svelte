<script lang="ts">
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { LogicalSize } from '@tauri-apps/api/dpi';
	import { getCurrentWindow } from '@tauri-apps/api/window';
	import { commands } from '$lib/tauri';
	import { artifactTab, checkServerConnection, connection, initUploadTracking, preflight, recorder, stageNames, stageRetry, stageShortNames, uploads } from '$lib/stores.svelte';
	import Icon from '$lib/Icon.svelte';
	import { isAndroidTauri } from '$lib/mobile-recorder';

	let { children } = $props();
	// Android: no desktop window chrome (collapse/minimize/close), no native
	// window sizing — the WebView is fullscreen and the OS owns the window.
	const android = isAndroidTauri();
	let collapsed = $state(browser && localStorage.getItem('transcripter.window-collapsed') === 'true');
	// Android-only navigation drawer: the rail is too wide for a phone screen,
	// so it slides in over the workspace instead of pinning a column.
	let navOpen = $state(false);
	let dragOrigin: { x: number; y: number } | null = null;
	let draggedCollapsedMark = $state(false);
	const navItems = [
		{ href: '/', label: 'Record', icon: 'record' },
		{ href: '/import', label: 'Import', icon: 'import' },
		{ href: '/recordings', label: 'Library', icon: 'library' },
		{ href: '/vault', label: 'Vault', icon: 'vault' },
		{ href: '/settings', label: 'Settings', icon: 'settings' }
	] as const;

	const artifactTabs = [
		{ key: 'transcript', label: 'Transcript', icon: 'transcript' },
		{ key: 'speakers', label: 'Speakers', icon: 'speakers' },
		{ key: 'events', label: 'Events', icon: 'events' },
		{ key: 'summary', label: 'Summary', icon: 'summary' },
		{ key: 'json', label: 'JSON', icon: 'json' }
	] as const;

	const onRecordingDetail = $derived(page.url.pathname.startsWith('/recordings/'));

	const uploadStates = $derived(Object.values(uploads));
	const uploadingCount = $derived(uploadStates.filter((u) => u.state === 'uploading' || u.state === 'queued').length);
	const failedCount = $derived(uploadStates.filter((u) => u.state === 'failed').length);
	const uploadPct = $derived.by(() => {
		const active = uploadStates.filter((u) => u.state === 'uploading' && u.total > 0);
		if (!active.length) return null;
		const committed = active.reduce((sum, u) => sum + u.committed, 0);
		const total = active.reduce((sum, u) => sum + u.total, 0);
		return Math.round((committed / total) * 100);
	});
	const uploadStatus = $derived.by(() => {
		if (failedCount > 0) return { tone: 'issue', text: `${failedCount} upload${failedCount === 1 ? '' : 's'} failed` };
		if (uploadingCount > 0) {
			const pct = uploadPct;
			return { tone: 'issue', text: pct !== null ? `Uploading… ${pct}%` : `Uploading… (${uploadingCount})` };
		}
		const pending = uploadStates.filter((u) => u.state !== 'done').length;
		return { tone: 'idle', text: pending > 0 ? `${pending} pending upload${pending === 1 ? '' : 's'}` : 'No pending uploads' };
	});
	const audioStatus = $derived(
		recorder.recording
			? 'Recording'
			: !preflight.current
				? 'Audio not checked'
				: preflight.current.error || ['silent', 'permission_denied', 'unavailable', 'failed'].includes(preflight.current.mic_state) || ['silent', 'permission_denied', 'unavailable', 'failed'].includes(preflight.current.system_state)
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
		`${audioStatus} · ${serverStatus}${uploadingCount ? ` · ${uploadStatus.text}` : ''}`
	);
	const routeName = $derived(
		page.url.pathname === '/'
			? 'Recorder'
			: page.url.pathname.startsWith('/import')
				? 'Import'
				: page.url.pathname.startsWith('/recordings')
					? 'Recordings'
					: page.url.pathname.startsWith('/vault')
						? 'Vault'
						: 'Settings'
	);
	onMount(async () => {
		void checkServerConnection();
		void initUploadTracking();
		if (!isTauri()) return;
		if (android) return;
		try {
			const appWindow = getCurrentWindow();
			const [physicalSize, scaleFactor] = await Promise.all([appWindow.innerSize(), appWindow.scaleFactor()]);
			const logicalSize = physicalSize.toLogical(scaleFactor);
			collapsed = logicalSize.width <= 100 && logicalSize.height <= 100;
			localStorage.setItem('transcripter.window-collapsed', String(collapsed));
		} catch {
			// The persisted value remains the fallback if native size inspection fails.
		}
		if (collapsed) {
			// Restore the pinned floating mode for a window that starts collapsed.
			void applyWindowMode(true);
		}
	});


	function isTauri(): boolean {
		return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
	}

	async function resizeWindow(width: number, height: number): Promise<void> {
		if (!isTauri()) return;
		await getCurrentWindow().setSize(new LogicalSize(width, height));
	}
	async function applyWindowMode(collapsed: boolean): Promise<void> {
		if (!isTauri()) {
			resizeWindow(collapsed ? 76 : 440, collapsed ? 76 : 720);
			return;
		}
		try {
			await commands.applyWindowMode(collapsed);
		} catch {
			resizeWindow(collapsed ? 76 : 440, collapsed ? 76 : 720);
		}
	}

	function toggleCollapsed(): void {
		if (android) return;
		collapsed = !collapsed;
		localStorage.setItem('transcripter.window-collapsed', String(collapsed));
		applyWindowMode(collapsed).catch((error) => console.warn('applyWindowMode failed', error));
	}

	async function minimizeWindow(): Promise<void> {
		if (isTauri()) await getCurrentWindow().minimize();
	}

	async function closeWindow(): Promise<void> {
		if (isTauri()) await getCurrentWindow().close();
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		// Drawer swallows Escape before the desktop collapse toggle sees it.
		if (android && navOpen) {
			navOpen = false;
			return;
		}
		if (!collapsed) toggleCollapsed();
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

	function activateCollapsedMark(): void {
		if (draggedCollapsedMark) {
			draggedCollapsedMark = false;
			return;
		}
		toggleCollapsed();
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
		aria-label={`Expand Transcriptor Maximus. ${collapsedStatus}`}
		title={collapsedStatus}
	>
		<span class="collapsed-icon" aria-hidden="true"><Icon name="mark" size={56} /></span>
		<span class="collapsed-state"></span>
	</button>
{:else}
	<div class="app-shell" class:shell--android={android}>
		{#if !android}
		<header class="titlebar" data-tauri-drag-region>
			<div class="titlebar-identity">
				<span class="titlebar-sigil"><Icon name="mark" size={18} /></span>
				<span class="wordmark">Transcriptor Maximus</span>
			</div>
			<div class="window-actions">
				<button type="button" onclick={toggleCollapsed} aria-label="Collapse to symbol" title="Collapse to symbol"><Icon name="collapse" size={16} /></button>
				<button type="button" onclick={minimizeWindow} aria-label="Minimize window" title="Minimize"><Icon name="minimize" size={16} /></button>
				<button class="close" type="button" onclick={closeWindow} aria-label="Close window" title="Close"><Icon name="close" size={16} /></button>
			</div>
		</header>
		{/if}

		<div class="hazard-rule" aria-hidden="true"></div>
		<div class="shell-body">
			{#if android}
				<button class="nav-scrim" class:open={navOpen} type="button" tabindex={navOpen ? 0 : -1} aria-label="Close navigation" onclick={() => (navOpen = false)}></button>
			{/if}
			<nav id="primary-nav" class="rail" class:open={navOpen} aria-label="Primary navigation">
				{#each navItems as item (item.href)}

					<a href={item.href} onclick={() => (navOpen = false)} aria-current={(item.href === '/recordings' || item.href === '/vault' ? page.url.pathname.startsWith(item.href) : page.url.pathname === item.href) ? 'page' : undefined} title={item.label}>
						<span class="nav-icon" aria-hidden="true"><Icon name={item.icon} size={20} /></span>
						<span>{item.label}</span>
					</a>
				{/each}
				{#if onRecordingDetail}
					<div class="rail-divider" role="separator"></div>
					<div class="rail-tabs" role="tablist" aria-label="Artifacts">
						{#each artifactTabs as tab (tab.key)}
							<button
								class="rail-tab"
								type="button"
								role="tab"
								aria-selected={artifactTab.active === tab.key}
								class:active={artifactTab.active === tab.key}
								title={tab.label}
								onclick={() => { artifactTab.active = tab.key; navOpen = false; }}
							>
								<span class="nav-icon" aria-hidden="true"><Icon name={tab.icon} size={20} /></span>
								<span>{tab.label}</span>
							</button>
						{/each}
					</div>
				{/if}
				<div class="rail-spacer"></div>
			</nav>

			<main class="workspace">
				<div class="context-bar">
					{#if android}
					<button class="cog-toggle" type="button" onclick={() => (navOpen = !navOpen)} aria-label={navOpen ? 'Close navigation' : 'Open navigation'} aria-expanded={navOpen} aria-controls="primary-nav"><span class="mini-cog" aria-hidden="true"><Icon name="mark" size={56} /></span></button>
					{/if}
					<span class="context-name">{routeName}</span>
					{#if onRecordingDetail && stageRetry.rerun && stageRetry.enabled && stageRetry.stages.length}
						<div class="stage-retry" role="group" aria-label="Re-run pipeline stages">
							{#each stageRetry.stages as stage (stage.kind)}
								<button
									type="button"
									class="stage-retry-button {stage.status}"
									title="Re-run {stageNames[stage.kind]} ({stage.status})"
									aria-label="Re-run {stageNames[stage.kind]} ({stage.status})"
									onclick={() => stageRetry.rerun?.(stage.kind)}
								>
									{stageShortNames[stage.kind]}
								</button>
							{/each}
						</div>
					{/if}
					<span class:ready={serverTone === 'ready'} class:issue={serverTone === 'issue'} class:unavailable={serverTone === 'unavailable'} class="status-lamp" aria-hidden="true"></span>
				</div>
				<div class="page-scroll">
					{@render children()}
				</div>
			</main>
		</div>

	<footer class="status-strip">
		<span><i class:ready={serverTone === 'ready'} class:issue={serverTone === 'issue'} class:unavailable={serverTone === 'unavailable'}></i>{serverStatus}</span>
		<span>{uploadStatus.text}</span>
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
	/* Desktop titlebar is a slim drag strip: identity cluster (sigil +
	   wordmark) on the left, window buttons on the right; the context bar
	   below still names the current page. The identity cluster is
	   pointer-events: none so clicks on it fall through to the header's
	   data-tauri-drag-region. */
	.titlebar { display: flex; align-items: center; justify-content: space-between; padding: 3px 6px; background: linear-gradient(90deg, #100d0b 0%, #221714 62%, #2d1311 100%); border-bottom: 1px solid rgba(215, 167, 71, 0.22); user-select: none; position: relative; z-index: 2; }
	.titlebar-identity { display: flex; align-items: center; gap: 7px; pointer-events: none; }
	.titlebar-sigil { width: 18px; height: 18px; display: grid; place-items: center; line-height: 0; color: var(--brass); }
	.wordmark { color: var(--bone); font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; white-space: nowrap; }
	.mini-cog { width: 29px; height: 29px; display: grid; place-items: center; filter: drop-shadow(0 2px 4px rgba(0, 0, 0, .45)); line-height: 0; }
	.window-actions { display: flex; gap: 3px; }
	.window-actions button { width: 27px; height: 27px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: rgba(0, 0, 0, 0.24); color: var(--ash); cursor: pointer; line-height: 0; transition: color 120ms ease, border-color 120ms ease, background 120ms ease; }
	.window-actions button:hover { color: var(--bone); border-color: rgba(215, 167, 71, 0.55); background: rgba(215, 167, 71, 0.08); }
	.window-actions .close:hover { color: white; border-color: var(--red); background: var(--red-dark); }
	.hazard-rule { background: repeating-linear-gradient(120deg, var(--brass) 0 8px, #17110b 8px 16px); opacity: 0.72; z-index: 0; }
	.shell-body { display: grid; grid-template-columns: 80px minmax(0, 1fr); min-height: 0; position: relative; z-index: 1; }
	.rail { display: flex; flex-direction: column; align-items: stretch; gap: 5px; padding: 10px 7px 8px; background: rgba(8, 7, 6, 0.65); border-right: 1px solid var(--line); }
	.rail a { display: grid; place-items: center; gap: 5px; min-height: 66px; color: #8e857c; text-decoration: none; font-size: 10px; font-weight: 650; letter-spacing: 0.02em; border: 1px solid transparent; border-radius: 3px; transition: color 130ms ease, background 130ms ease, border-color 130ms ease; }
	.rail a:hover { color: var(--bone); background: rgba(255, 255, 255, 0.025); }
	.rail a[aria-current='page'] { color: var(--brass); background: linear-gradient(90deg, rgba(213, 45, 36, 0.18), rgba(215, 167, 71, 0.04)); box-shadow: inset 2px 0 var(--red); }
	.rail-divider { height: 1px; margin: 3px 4px; background: var(--line); }
	.rail-tabs { display: flex; flex-direction: column; gap: 5px; }
	.rail-tab { display: grid; place-items: center; gap: 5px; min-height: 56px; padding: 0; border: 1px solid transparent; border-radius: 3px; background: transparent; color: #8e857c; font-size: 10px; font-weight: 650; letter-spacing: 0.02em; cursor: pointer; transition: color 130ms ease, background 130ms ease, border-color 130ms ease; }
	.rail-tab:hover { color: var(--bone); background: rgba(255, 255, 255, 0.025); }
	.rail-tab.active { color: var(--brass); background: linear-gradient(90deg, rgba(213, 45, 36, 0.18), rgba(215, 167, 71, 0.04)); box-shadow: inset 2px 0 var(--red); }
	.nav-icon { width: 28px; height: 28px; display: grid; place-items: center; line-height: 0; }
	.rail-spacer { flex: 1; }
	.workspace { display: grid; grid-template-rows: 42px minmax(0, 1fr); min-width: 0; min-height: 0; }
	.context-bar { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 10px; padding: 0 16px; border-bottom: 1px solid var(--line); background: rgba(0, 0, 0, 0.1); }
	.stage-retry { display: flex; gap: 5px; }
	.stage-retry-button { display: flex; align-items: center; min-height: 22px; padding: 0 5px; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #968d83; font-size: 9px; font-weight: 700; cursor: pointer; }
	.stage-retry-button.done { color: var(--cyan); border-color: rgba(112, 215, 208, 0.25); }
	.stage-retry-button.running { color: var(--brass); border-color: rgba(215, 167, 71, 0.25); }
	.stage-retry-button.failed { color: #f36b60; border-color: rgba(213, 45, 36, 0.35); }
	.stage-retry-button.skipped, .stage-retry-button.pending { color: #6f685f; }
	.stage-retry-button:hover { border-color: var(--brass); background: rgba(215, 167, 71, 0.12); }
	.context-name { font-size: 14px; font-weight: 650; color: #c8bbaa; }
	.status-lamp, .status-strip i { width: 6px; height: 6px; border-radius: 50%; background: #6b655e; box-shadow: 0 0 0 2px rgba(107, 101, 94, 0.12); }
	.status-lamp.unavailable, .status-strip i.unavailable { background: var(--red); box-shadow: 0 0 0 3px rgba(213, 45, 36, 0.12), 0 0 12px rgba(213, 45, 36, 0.8); }
	.status-lamp.ready, .status-strip i.ready { background: var(--cyan); box-shadow: 0 0 0 3px rgba(112, 215, 208, 0.1), 0 0 10px rgba(112, 215, 208, 0.65); }
	.status-lamp.issue, .status-strip i.issue { background: var(--brass); box-shadow: 0 0 0 3px rgba(215, 167, 71, 0.1), 0 0 10px rgba(215, 167, 71, 0.55); }
	.page-scroll { min-height: 0; overflow: auto; scrollbar-width: thin; scrollbar-color: var(--red-dark) transparent; }
	.status-strip { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 14px; padding: 0 12px; border-top: 1px solid rgba(215, 167, 71, 0.16); background: #0e0b0a; color: #8f857b; font-size: 10px; position: relative; z-index: 2; }
	.status-strip span:first-child { display: flex; align-items: center; gap: 7px; }
	/* Android: the app sigil lives in the context bar and doubles as the
	   drawer toggle. Glyph fills the 56px hit target so its ink actually
	   rides OVER the hazard rule above and the bar seam below (z above
	   both), while its bottom edge stays ~10px clear of the page title. */
	.cog-toggle { width: 56px; height: 56px; display: grid; place-items: center; padding: 0; border: 0; background: transparent; cursor: pointer; line-height: 0; position: relative; z-index: 3; }
	.shell--android .cog-toggle { align-self: center; margin-left: -6px; }
	/* Pin the bar's internal row to the bar box: without this the 56px
	   toggle grows the row to 56px and drags the page title off-center. */
	.shell--android .context-bar { grid-template-rows: 100%; }
	.shell--android .mini-cog { width: 56px; height: 56px; }
	.cog-toggle:active .mini-cog { transform: scale(0.94); transition: transform 100ms ease; }

	/* Android: the WebView draws edge-to-edge under the system status bar, so
	   the shell absorbs the top inset (env() = 0 on desktop, where these rules
	   never apply anyway) and the footer absorbs the bottom one. There is no
	   titlebar at all; the rail becomes an overlay drawer. */
	.shell--android { grid-template-rows: 4px minmax(0, 1fr) auto; padding-top: env(safe-area-inset-top, 0px); }
	/* The context bar becomes the top chrome: sigil toggle + page title +
	   stage re-runs + connection lamp. */
	.shell--android .context-bar { grid-template-columns: auto 1fr auto auto; gap: 8px; padding: 0 10px; }

	/* Android is edge-to-edge fullscreen: the desktop window frame (brass
	   border + drop shadow) has no window to frame and reads as a stray
	   outline around the whole screen. */
	.shell--android { border: 0; box-shadow: none; }

	.shell--android .shell-body { grid-template-columns: minmax(0, 1fr); }
	.nav-scrim { position: absolute; inset: 0; z-index: 5; padding: 0; border: 0; border-radius: 0; background: rgba(5, 4, 3, 0.55); opacity: 0; pointer-events: none; transition: opacity 140ms ease; }
	.nav-scrim.open { opacity: 1; pointer-events: auto; }
	.shell--android .rail { position: absolute; top: 0; bottom: 0; left: 0; z-index: 6; width: 192px; background: #14100e; border-right: 1px solid rgba(215, 167, 71, 0.28); box-shadow: 14px 0 34px rgba(0, 0, 0, 0.5); transform: translateX(-105%); transition: transform 160ms ease; }
	.shell--android .rail.open { transform: translateX(0); }
	.shell--android .status-strip { min-height: 28px; padding-bottom: env(safe-area-inset-bottom, 0px); }

	.collapsed-mark { width: 76px; height: 76px; margin: 0; padding: 7px; border: 0; border-radius: 50%; background: transparent; box-shadow: none; cursor: grab; position: relative; transition: transform 180ms ease; touch-action: none; }
	.collapsed-icon { width: 56px; height: 56px; display: grid; place-items: center; transition: transform 180ms ease; line-height: 0; }
	.collapsed-state { position: absolute; right: 12px; bottom: 12px; width: 9px; height: 9px; border: 2px solid #100d0b; border-radius: 50%; background: #7c756d; }
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
	:global(.notice) { display: grid; gap: 4px; margin: 0 0 10px; padding: 11px 12px; border-left: 2px solid var(--brass); background: rgba(215, 167, 71, 0.07); font-size: 12px; line-height: 1.4; }
	:global(.notice.error) { border-color: var(--red); background: rgba(213, 45, 36, 0.08); }
	:global(.notice strong) { font-size: 10px; font-weight: 700; color: var(--brass); }
	:global(.notice.error strong) { color: var(--red); }
	:global(.notice span) { color: #b5aa9c; font-size: 11px; }
	/* Canonical list-row button (DESIGN_GUIDELINES "Seam": a list is a ruled
	   manifest directly on the plate — rows separated by seams, never boxed
	   cards). Use as <button class="list-row <page-class>">; the page adds only
	   grid-template-columns and row-specific rules. The class carries the UA
	   button reset — skipping it renders the native white button chrome. */
	:global(.list-row) { width: 100%; display: grid; align-items: center; gap: 9px; padding: 11px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; transition: background 120ms ease; }
	:global(.list-row:hover) { background: rgba(255, 255, 255, 0.02); }

	@media (prefers-reduced-motion: reduce) {
		*, *::before, *::after { scroll-behavior: auto !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
	}
</style>
