---
name: transcripter-client-build
description: Build the transcripter Tauri v2 + SvelteKit desktop client from client/ — pnpm install, pnpm build (adapter-static -> build/), cargo build in src-tauri, and pnpm tauri build release bundles (.app/.dmg, .exe/.msi). Use when asked to build/compile the client, produce a release bundle, run the frontend typecheck (pnpm check), or why the first cargo build is slow.
metadata:
  version: "1.0"
---

# transcripter-client-build

Build the Tauri v2 + SvelteKit client in `client/`. All commands run from `client/`
unless noted. The frontend (SvelteKit) and the Rust shell (`src-tauri/`) build
together; Tauri drives the frontend build itself, so you usually do not hand-build
the frontend separately.

## Prerequisites

- Node 22+ (v26.7.0 verified), pnpm (10.30.2 verified)
- Rust stable (rustc/cargo 1.97.1 verified)
- macOS: Xcode Command Line Tools (`xcode-select --install`)

## Build (debug)

```bash
cd client
pnpm install            # ~2s
pnpm build              # SvelteKit adapter-static -> client/build/
cd src-tauri
cargo build             # ~34s after warm; first build compiles ~419 crates
```

`pnpm build` runs `vite build` (`client/package.json`), which uses
`@sveltejs/adapter-static` (`client/svelte.config.js`) to emit a static site into
`client/build/`. `cargo build` then links the Rust shell against that.

`tauri.conf.json` sets `beforeBuildCommand: "pnpm build"` and
`frontendDist: "../build"`. So `pnpm tauri build` (or `pnpm tauri dev`) runs
`pnpm build` for you. A standalone `pnpm build` is only needed when you want the
frontend alone, or want to rebuild it before a separate `cargo build`.

## Typecheck gate

```bash
cd client
pnpm check              # svelte-kit sync && svelte-check --tsconfig ./tsconfig.json
```

## Release bundles

```bash
cd client
pnpm tauri build        # -> client/src-tauri/target/release/bundle/
```

Output layout under `src-tauri/target/release/bundle/`:

- macOS: `macos/*.app` and `dmg/*.dmg`
- Windows: `nsis/*.exe` and `msi/*.msi`

`tauri.conf.json` sets `bundle.targets: "all"`, so every target for the host OS is
produced. Cross-compiling Tauri is NOT supported — build on the target OS. There is
no macOS→Windows path; run `pnpm tauri build` on a Windows machine for Windows
bundles.

## Gotchas

- **First `cargo build` is slow (~419 crates), dominated by tauri/tao/coreaudio-sys.**
  Subsequent builds are seconds. Do not treat a long first build as a hang.
- **`frontendDist: "../build"` means a stale/missing `client/build/` yields a blank
  window.** Let Tauri run `beforeBuildCommand` rather than hand-building the frontend
  out of order; if you build the frontend manually, build it before `cargo build`.
- **macOS `.app`/`.dmg` are unsigned** — first launch needs right-click → Open to
  bypass Gatekeeper.
- **Cross-compiling Tauri is unsupported.** No macOS→Windows; build on the target OS.
- **`pnpm check` is the typecheck gate** (`svelte-kit sync && svelte-check`); it does
  not emit a bundle, so run it separately before shipping.

## See also

- `transcripter-client-run` — launching the app and verifying the UI.
- `transcripter-test-suite` — `cargo test` / `cargo clippy` for the Rust shell.
