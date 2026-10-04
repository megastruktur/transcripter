<script lang="ts">
	import { browser } from '$app/environment';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { onMount, onDestroy } from 'svelte';
	import { getCurrentWindow } from '@tauri-apps/api/window';
	import { commands } from '$lib/tauri';
	import { checkServerConnection, connection, initUploadTracking, preflight, recorder, recordActions, stageIcons, stageNames, stageRetry, uploads } from '$lib/stores.svelte';
	import Icon from '$lib/Icon.svelte';
	import { isAndroidTauri } from '$lib/mobile-recorder';
	import MascotOverlayView from '$lib/orbitkit/MascotOverlayView.svelte';
	import QuickRecordPopup from '$lib/orbitkit/QuickRecordPopup.svelte';
	import { PopupSheet, showOverlay, onMenuAction } from '@orbitkit/ui';
	import orbitkitConfig from '../orbitkit.config';

	// ── Multi-window router ─────────────────────────────────────────────────────
	// Classifies this webview window into one of three slots so the layout can
	// render exclusively the correct view instead of the full app shell.
	//
	// URL forms (config reconciliation — mandate 3 of BRIEF):
	//   Config popup URL  → ?orbitkit=popup&view=quick_record
	//   Spec router keys  → ?popup=quick_record
	//                         label starts with "orbitkit-popup-quick_record"
	// Both forms are handled so the declared popup in orbitkit.config.json routes.
	type WindowSlot = 'mascot' | 'quick_record' | 'default';
	function classifyWindow(): WindowSlot {
		const url = typeof window !== 'undefined' ? window.location.href : '';
		const params = new URL(url).searchParams;
		// Mascot: URL param or Tauri window label
		if (params.get('orbitkit') === 'mascot') return 'mascot';
		// Popup: spec keys (?popup=quick_record) AND config form
		// (?orbitkit=popup&view=quick_record).  Label prefix is checked by
		// the tauri config on the Rust side — we only classify the URL here.
		if (params.get('popup') === 'quick_record') return 'quick_record';
		if (params.get('orbitkit') === 'popup' && params.get('view') === 'quick_record') return 'quick_record';
		return 'default';
	}
	// AC2 requires window label checks too. Guard with isTauri() because
	// getCurrentWindow() throws in plain browser (dev fallback path).
	function getWindowLabel(): string {
		if (!isTauri()) return '';
		try {
			return getCurrentWindow().label;
		} catch {
			return '';
		}
	}
	function classifyByLabel(slot: WindowSlot): WindowSlot {
		const label = getWindowLabel();
		if (label === 'orbitkit-mascot') return 'mascot';
		if (label.startsWith('orbitkit-popup-quick_record')) return 'quick_record';
		return slot;
	}
	const windowSlot: WindowSlot = classifyByLabel(classifyWindow());

	let { children } = $props();
	// Android: no desktop window chrome (collapse/minimize/close), no native
	// window sizing — the WebView is fullscreen and the OS owns the window.
	const android = isAndroidTauri();
	// Android-only navigation drawer: the rail is too wide for a phone screen,
	// so it slides in over the workspace instead of pinning a column.
	let navOpen = $state(false);
	// Recording-detail actions menu (ellipsis at the right edge of the context
	// bar). The detail page publishes its actions through recordActions.
	let menuOpen = $state(false);
	let menuDeleteArmed = $state(false);
	let menuWrap = $state<HTMLDivElement>();
	const navItems = [
		{ href: '/', label: 'Record', icon: 'record' },
		{ href: '/import', label: 'Import', icon: 'import' },
		{ href: '/recordings', label: 'Library', icon: 'library' },
		{ href: '/vault', label: 'Vault', icon: 'vault' },
		{ href: '/settings', label: 'Settings', icon: 'settings' }
	] as const;
	const onRecordingDetail = $derived(page.url.pathname.startsWith('/recordings/'));
	// Route changes close the actions menu: the detail page unpublishes its
	// actions on unmount, and the menu must not linger over another page.
	$effect(() => {
		void page.url.pathname;
		closeMenu();
	});

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
		// AC1/AC2: show mascot overlay on desktop boot (mascot window manages itself)
		if (!android && windowSlot === 'default' && isTauri()) {
			void showOverlay({ menu: orbitkitConfig.menu, mascot: { size: orbitkitConfig.mascot.size ?? 96 } });
		}
	});
	onDestroy(() => { _unlistenMenu?.(); });

	// AC3: wire mascot menu actions (only in the main default window, not mascot/quick_record slots)
	let _unlistenMenu: (() => void) | undefined;
	if (windowSlot === 'default') {
		onMenuAction((action) => {
			if (action.id === 'quick_record') {
				// Toggle capture using last-used configuration from localStorage
				void import('$lib/stores.svelte').then(({ startRecording, stopRecording, recorder, checkAudio, SYSTEM_AUDIO_OFF }) => {
					if (recorder.recording) {
						void stopRecording();
					} else {
						const savedMicrophone = localStorage.getItem('transcripter.microphone') ?? '';
						const savedSystemOutput = localStorage.getItem('transcripter.system-output') ?? SYSTEM_AUDIO_OFF;
						const savedCaptureSystem = savedSystemOutput !== SYSTEM_AUDIO_OFF;
						const systemOutputForRecord = savedSystemOutput === SYSTEM_AUDIO_OFF ? null : savedSystemOutput;
						void checkAudio(savedMicrophone, savedSystemOutput || null, savedCaptureSystem).then((report) => {
							if (!report.error && report.mic_state !== 'permission_denied' && report.mic_state !== 'unavailable' && report.mic_state !== 'failed') {
								void startRecording('Quick Record', [], savedMicrophone || null, systemOutputForRecord, savedCaptureSystem);
							}
						});
					}
				});
			} else if (['recordings', 'import', 'vault', 'settings'].includes(action.id)) {
				void goto(`/${action.id}`);
				void getCurrentWindow().show();
			}
		}).then((fn) => { _unlistenMenu = fn; });
	}


	function isTauri(): boolean {
		return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
	}

	async function minimizeWindow(): Promise<void> {
		if (isTauri()) await getCurrentWindow().minimize();
	}

	async function closeWindow(): Promise<void> {
		if (isTauri()) await getCurrentWindow().close();
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		// An open actions menu swallows Escape before anything else sees it.
		if (menuOpen) {
			closeMenu();
			return;
		}
		// Drawer swallows Escape before the desktop collapse toggle sees it.
		if (android && navOpen) {
			navOpen = false;
			return;
		}
	}

	function closeMenu(): void {
		menuOpen = false;
		menuDeleteArmed = false;
	}

	function toggleMenu(): void {
		if (menuOpen) closeMenu();
		else menuOpen = true;
	}

	function runMenuAction(action: (() => void) | null): void {
		closeMenu();
		action?.();
	}

	async function confirmMenuDelete(): Promise<void> {
		const remove = recordActions.remove;
		closeMenu();
		await remove?.();
	}

	function handleMenuPointerDown(event: PointerEvent): void {
		if (!menuOpen || !menuWrap) return;
		if (event.target instanceof Node && menuWrap.contains(event.target)) return;
		closeMenu();
	}
</script>

<svelte:window onkeydown={handleKeydown} onpointerdown={handleMenuPointerDown} />

<svelte:head>
	<meta name="theme-color" content="#160f0d" />
</svelte:head>

{#if windowSlot === 'mascot'}
	<MascotOverlayView config={orbitkitConfig} />
{:else if windowSlot === 'quick_record'}
	<QuickRecordPopup />
{:else}
	<div class="app-shell" class:shell--android={android}>
		{#if !android}
		<header class="titlebar" data-tauri-drag-region>
			<span class="titlebar-sigil"><Icon name="mark" size={40} /></span>
			<span class="wordmark">Transcriptor Maximus</span>
			<div class="window-actions">
				<button type="button" onclick={() => getCurrentWindow().hide()} aria-label="Dock to Companion" title="Dock to Companion"><Icon name="collapse" size={16} /></button>
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
				<div class="rail-spacer"></div>
			</nav>

			<main class="workspace">
				<div class="context-bar">
					{#if android}
					<button class="cog-toggle" type="button" onclick={() => (navOpen = !navOpen)} aria-label={navOpen ? 'Close navigation' : 'Open navigation'} aria-expanded={navOpen} aria-controls="primary-nav"><span class="mini-cog" aria-hidden="true"><Icon name="mark" size={56} /></span></button>
					{/if}
					<span class="context-name">{routeName}</span>
					{#if onRecordingDetail && stageRetry.stages.length}
						<span class="context-stages" role="group" aria-label="Pipeline stages">
							{#each stageRetry.stages as stage (stage.kind)}
								<span
									class="ctx-stage {stage.status}"
									role="img"
									title="{stageNames[stage.kind]} · {stage.status}"
									aria-label="{stageNames[stage.kind]}: {stage.status}"
								>
									<Icon name={stageIcons[stage.kind]} size={13} strokeWidth={1.5} />
								</span>
							{/each}
						</span>
					{/if}
					<span class:ready={serverTone === 'ready'} class:issue={serverTone === 'issue'} class:unavailable={serverTone === 'unavailable'} class="status-lamp" aria-hidden="true"></span>
					{#if onRecordingDetail && recordActions.loaded}
						<div class="menu-wrap" bind:this={menuWrap}>
							<button class="menu-toggle" type="button" aria-label="Recording actions" aria-haspopup="menu" aria-expanded={menuOpen} onclick={toggleMenu}><Icon name="dots" size={15} /></button>
							{#if menuOpen}
								<div class="context-menu" role="menu" aria-label="Recording actions">
									{#if stageRetry.enabled && stageRetry.rerun}
										{#each stageRetry.stages as stage (stage.kind)}
											<button type="button" role="menuitem" class="menu-item" onclick={() => { stageRetry.rerun?.(stage.kind); closeMenu(); }}>
												<span>Re-run {stageNames[stage.kind]}</span>
												<span class="menu-status {stage.status}">{stage.status}</span>
											</button>
										{/each}
										<div class="menu-sep" role="separator"></div>
									{/if}
									<button type="button" role="menuitem" class="menu-item" onclick={() => runMenuAction(recordActions.rename)}>Rename…</button>
									<button type="button" role="menuitem" class="menu-item" onclick={() => runMenuAction(recordActions.editDate)}>Edit date…</button>
									<button type="button" role="menuitem" class="menu-item" onclick={() => runMenuAction(recordActions.editType)}>Change type…</button>
									{#if recordActions.deletable}
										<div class="menu-sep" role="separator"></div>
										{#if menuDeleteArmed}
											<div class="menu-confirm">
												<span>Delete permanently?</span>
												<button class="menu-confirm-yes" type="button" onclick={() => void confirmMenuDelete()}>Confirm</button>
												<button class="menu-confirm-no" type="button" onclick={() => (menuDeleteArmed = false)}>Cancel</button>
											</div>
										{:else}
											<button type="button" role="menuitem" class="menu-item menu-danger" onclick={() => (menuDeleteArmed = true)}>Delete…</button>
										{/if}
									{/if}
								</div>
							{/if}
						</div>
					{/if}
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
		<PopupSheet components={{ quick_record: QuickRecordPopup }} />
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
		/* Structural grid: rail width and horizontal content inset. Both the
		   titlebar (wordmark) and the workspace content (context-bar title,
		   .page padding) read these so all left edges line up on one ruler. */
		--rail-w: 72px;
		--pad-x: 16px;
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
	/* Desktop titlebar is a slim drag strip on the rail's structural grid:
	   column 1 = rail width (sigil centered over the rail), column 2 = wordmark
	   starting exactly at the workspace edge + --pad-x (same inset as the
	   context-bar title), column 3 = window buttons. Text nodes are
	   pointer-events: none so clicks fall through to the drag region. */
	.titlebar { display: grid; grid-template-columns: var(--rail-w) minmax(0, 1fr) auto; align-items: center; padding: 3px 6px 3px 0; background: linear-gradient(90deg, #100d0b 0%, #221714 62%, #2d1311 100%); border-bottom: 1px solid rgba(215, 167, 71, 0.22); user-select: none; position: relative; z-index: 2; }
	.titlebar-sigil { display: grid; place-items: center; width: 100%; height: 40px; color: var(--brass); line-height: 0; pointer-events: none; }
	.wordmark { padding-left: var(--pad-x); color: var(--bone); font-size: 16px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none; }
	.mini-cog { width: 29px; height: 29px; display: grid; place-items: center; filter: drop-shadow(0 2px 4px rgba(0, 0, 0, .45)); line-height: 0; }
	.window-actions { display: flex; gap: 3px; }
	.window-actions button { width: 27px; height: 27px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: rgba(0, 0, 0, 0.24); color: var(--ash); cursor: pointer; line-height: 0; transition: color 120ms ease, border-color 120ms ease, background 120ms ease; }
	.window-actions button:hover { color: var(--bone); border-color: rgba(215, 167, 71, 0.55); background: rgba(215, 167, 71, 0.08); }
	.window-actions .close:hover { color: white; border-color: var(--red); background: var(--red-dark); }
	.shell-body { display: grid; grid-template-columns: var(--rail-w) minmax(0, 1fr); min-height: 0; position: relative; z-index: 1; }
	.hazard-rule { background: repeating-linear-gradient(120deg, var(--brass) 0 8px, #17110b 8px 16px); opacity: 0.72; z-index: 0; }
	.rail { display: flex; flex-direction: column; align-items: stretch; gap: 4px; min-height: 0; padding: 10px 5px 8px; background: rgba(8, 7, 6, 0.65); border-right: 1px solid var(--line); }
	.rail a { display: grid; place-items: center; gap: 5px; min-height: 66px; color: #8e857c; text-decoration: none; font-size: 10px; font-weight: 650; letter-spacing: 0.02em; border: 1px solid transparent; border-radius: 3px; transition: color 130ms ease, background 130ms ease, border-color 130ms ease; }
	.rail a:hover { color: var(--bone); background: rgba(255, 255, 255, 0.025); }
	.nav-icon { width: 28px; height: 28px; display: grid; place-items: center; line-height: 0; }
	.rail-spacer { flex: 1; }
	.workspace { display: grid; grid-template-rows: 42px minmax(0, 1fr); min-width: 0; min-height: 0; }
	.context-bar { display: grid; grid-template-columns: minmax(0, 1fr) auto auto auto; align-items: center; gap: 10px; padding: 0 var(--pad-x); min-width: 0; border-bottom: 1px solid var(--line); background: rgba(0, 0, 0, 0.1); }
	/* Pipeline status icons ride next to the page title; every cluster in the
	   bar must stay shrinkable so a 360px phone never clips the right edge. */
	.context-stages { display: flex; gap: 3px; min-width: 0; }
	.ctx-stage { width: 18px; height: 18px; display: grid; place-items: center; border-radius: 2px; background: rgba(255, 255, 255, 0.03); color: #6f685f; line-height: 0; }
	.ctx-stage.done { color: var(--cyan); }
	.ctx-stage.running { color: var(--brass); }
	.ctx-stage.failed { color: #f36b60; }
	.menu-wrap { position: relative; display: flex; min-width: 0; }
	.menu-toggle { width: 26px; height: 26px; display: grid; place-items: center; padding: 0; border: 1px solid var(--line); border-radius: 2px; background: transparent; color: #968d83; cursor: pointer; line-height: 0; }
	.menu-toggle:hover, .menu-toggle[aria-expanded='true'] { color: var(--bone); border-color: rgba(215, 167, 71, 0.4); background: rgba(215, 167, 71, 0.08); }
	.context-menu { position: absolute; top: calc(100% + 6px); right: 0; z-index: 40; min-width: 196px; padding: 4px; display: flex; flex-direction: column; gap: 1px; background: #14100e; border: 1px solid rgba(215, 167, 71, 0.28); border-radius: 3px; box-shadow: 0 14px 34px rgba(0, 0, 0, 0.55); }
	.menu-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; min-height: 28px; padding: 0 9px; border: 0; border-radius: 2px; background: transparent; color: #c8bbaa; font-size: 11px; text-align: left; cursor: pointer; white-space: nowrap; }
	.menu-item:hover { background: rgba(215, 167, 71, 0.1); color: var(--bone); }
	.menu-status { font-size: 9px; font-weight: 700; color: #6f685f; text-transform: uppercase; letter-spacing: 0.04em; }
	.menu-status.done { color: var(--cyan); }
	.menu-status.running { color: var(--brass); }
	.menu-status.failed { color: #f36b60; }
	.menu-sep { height: 1px; margin: 3px 5px; background: var(--line); }
	.menu-danger { color: #f36b60; }
	.menu-danger:hover { background: rgba(213, 45, 36, 0.12); color: #f36b60; }
	.menu-confirm { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 5px; padding: 4px 5px; }
	.menu-confirm > span { font-size: 10px; font-weight: 650; color: #f36b60; white-space: nowrap; }
	.menu-confirm button { min-height: 24px; padding: 0 8px; border-radius: 2px; font-size: 9px; font-weight: 700; cursor: pointer; line-height: 0; }
	.menu-confirm-yes { border: 1px solid var(--red); background: var(--red); color: var(--bone); }
	.menu-confirm-yes:hover { background: #b3251d; }
	.menu-confirm-no { border: 1px solid rgba(215, 167, 71, 0.26); background: rgba(215, 167, 71, 0.06); color: var(--brass); }
	.menu-confirm-no:hover { border-color: var(--brass); background: rgba(215, 167, 71, 0.12); }
	.context-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 650; color: #c8bbaa; }
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
	   stage icons + connection lamp + actions menu. */
	.shell--android .context-bar { grid-template-columns: auto minmax(0, 1fr) auto auto auto; gap: 8px; padding: 0 10px; }

	/* Android is edge-to-edge fullscreen: the desktop window frame (brass
	   border + drop shadow) has no window to frame and reads as a stray
	   outline around the whole screen. */
	.shell--android { border: 0; box-shadow: none; }

	.shell--android .shell-body { grid-template-columns: minmax(0, 1fr); }
	.nav-scrim { position: absolute; inset: 0; z-index: 5; padding: 0; border: 0; border-radius: 0; background: rgba(5, 4, 3, 0.55); opacity: 0; pointer-events: none; transition: opacity 140ms ease; }
	.nav-scrim.open { opacity: 1; pointer-events: auto; }
	.shell--android .rail { position: absolute; top: 0; bottom: 0; left: 0; z-index: 6; width: 168px; background: #14100e; border-right: 1px solid rgba(215, 167, 71, 0.28); box-shadow: 14px 0 34px rgba(0, 0, 0, 0.5); transform: translateX(-105%); transition: transform 160ms ease; }
	.shell--android .rail.open { transform: translateX(0); }
	.shell--android .status-strip { min-height: 28px; padding-bottom: env(safe-area-inset-bottom, 0px); }

	:global(.page) { padding: 18px var(--pad-x) 24px; }
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
