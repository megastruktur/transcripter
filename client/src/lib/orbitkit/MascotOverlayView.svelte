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

  <!-- Radial menu -->
  {#if menuOpen}
    <RadialMenu
      config={config.menu}
      open={menuOpen}
      onselect={handleMenuSelect}
      onclose={handleMenuClose}
    />
  {/if}
</div>

<style>
  .mascot-overlay {
    position: relative;
    display: inline-block;
  }

  .mascot-root {
    display: inline-block;
    line-height: 0;
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
