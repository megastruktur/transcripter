<script lang="ts">
  import { Mascot, RadialMenu, createDragGesture, startMascotDrag, startPassthrough } from "@orbitkit/ui";
  import { getCurrentWindow } from "@tauri-apps/api/window";
  import { onMount } from "svelte";
  import type { OrbitKitConfig } from "@orbitkit/ui";

  let { config, mascotState = $bindable("idle") }: {
    config: OrbitKitConfig;
    mascotState?: "idle" | "recording" | "issue";
  } = $props();

  let menuOpen = $state(false);

  // Passthrough controller, started on mount
  let passthroughController: ReturnType<typeof startPassthrough> | null = null;

  // Hit region: the mascot button root
  let mascotEl: HTMLElement | null = $state(null);

  // Drag vs click disambiguation
  const dragGesture = createDragGesture({
    openButton: "right",
    onDragStart: () => startMascotDrag(),
    onToggle: () => {
      menuOpen = !menuOpen;
    },
    isMenuOpen: () => menuOpen,
    closeMenuInstant: () => {
      menuOpen = false;
    },
  });

  onMount(() => {
    if (!mascotEl) return;
    passthroughController = startPassthrough({
      getWindow: () => getCurrentWindow(),
      intervalMs: 150,
      isDragging: () => dragGesture.isDragged,
    });
    passthroughController.registerHitRegion(() => {
      if (!mascotEl) return [];
      const rect = mascotEl.getBoundingClientRect();
      return [{ left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }];
    });
    // Menu-open region: while the radial menu is visible the entire overlay
    // window stays interactive, so clicks on the arc items do not fall
    // through to the apps underneath. Closed menu → no rects, leaving only
    // the mascot region above.
    passthroughController.registerHitRegion(() => {
      if (!menuOpen) return [];
      return [
        {
          left: 0,
          top: 0,
          right: window.innerWidth,
          bottom: window.innerHeight,
        },
      ];
    });

    return () => {
      passthroughController?.stop();
    };
  });

  function handleMenuSelect(_id: string) {
    menuOpen = false;
  }

  function handleMenuClose() {
    menuOpen = false;
  }
</script>

<div class="mascot-overlay" role="presentation">
  <!-- Mascot with equalizer wave snippet -->
  <div
    bind:this={mascotEl}
    class="mascot-root"
    {...dragGesture}
  >
    <Mascot config={config.mascot} state={mascotState}>
      {#snippet children()}
        <span class="collapsed-wave" class:recording={mascotState === "recording"}>
          {#each [10, 17, 21, 17, 10] as barHeight, index (index)}
            <i style="--bar-height: {barHeight}px; --delay: {index * -74}ms"></i>
          {/each}
        </span>
      {/snippet}
    </Mascot>
  </div>

  <!-- Radial menu: 0×0 origin anchored at the window centre; the arc
       (layout "arc", position "left") is laid out around this point. -->
  {#if menuOpen}
    <div class="menu-origin">
      <RadialMenu
        config={config.menu}
        open={menuOpen}
        onselect={handleMenuSelect}
        onclose={handleMenuClose}
      />
    </div>
  {/if}
</div>

<style>
  /* Overlay fills the 288×288 mascot window so the RadialMenu origin can sit
     at the window centre: an arc (radius 92 + item 44) from the old corner
     origin (0,76) fell outside the window (x < 0). */
  .mascot-overlay {
    position: fixed;
    inset: 0;
  }

  .mascot-root {
    /* Mascot centred on the window centre — the same point the arc origin
       uses, so the menu rings the mascot instead of hanging off a corner. */
    position: absolute;
    left: 50%;
    top: 50%;
    translate: -50% -50%;
    line-height: 0;
  }

  .menu-origin {
    /* Zero-size anchor at the window centre: the RadialMenu container is
       0×0 and positions its items relative to it, so this point is the
       arc origin (144,144 in the 288×288 overlay window). */
    position: absolute;
    left: 50%;
    top: 50%;
    width: 0;
    height: 0;
  }

  .collapsed-wave {
    position: absolute;
    inset: 0;
    display: none;
    align-items: center;
    justify-content: center;
    gap: 3px;
    line-height: 0;
    pointer-events: none;
  }

  .collapsed-wave.recording {
    display: flex;
  }

  .collapsed-wave i {
    width: 2px;
    height: calc(var(--bar-height) * 0.5);
    background: linear-gradient(to top, #d52d24, #d7a747);
    border-radius: 1px;
    transform-origin: center;
    animation: signal 780ms ease-in-out infinite alternate;
    animation-delay: var(--delay);
    box-shadow: 0 0 7px rgba(213, 45, 36, 0.25);
  }

  @keyframes signal {
    to {
      height: var(--bar-height);
    }
  }
</style>
