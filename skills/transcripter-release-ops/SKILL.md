---
name: transcripter-release-ops
description: Cut a transcripter client release — bump the version in all four version files, commit with conventional messages, push main, tag vX.Y.Z, push the tag, and verify the release.yml CI run started. Use when asked to release, bump the version, tag a release, publish client binaries, or "коммить, пуш, бамп релиза".
metadata:
  version: "1.0"
---

# transcripter-release-ops

Releasing the Tauri client = **bump versions → commit → push → tag → push tag**.
The tag push is the ONLY build trigger; there is no local release build (the dev
host has no webkitgtk — GUI bundles exist only from CI).

## Version lives in FOUR files — all must match

| File | Field |
|---|---|
| `client/package.json` | `"version"` |
| `client/src-tauri/tauri.conf.json` | `"version"` |
| `client/src-tauri/Cargo.toml` | `[package] version` |
| `client/src-tauri/Cargo.lock` | the `name = "transcripter"` entry's `version` |

Cargo.lock trap: other crates (e.g. `bit-set`) may share the same version number.
Edit ONLY the entry scoped by `name = "transcripter"` — locate it with
`grep -n -A2 'name = "transcripter"' client/src-tauri/Cargo.lock`, never a global
find-replace.

## The tag MUST equal tauri.conf.json version

`release.yml` passes `tagName: v__VERSION__` to `tauri-action`, and `__VERSION__`
is resolved from `tauri.conf.json` — NOT from the git tag. Tagging `v0.8.1` while
the config still says `0.8.0` attaches assets to the OLD v0.8.0 release (and
overwrites them). Always bump first, tag second.

## Procedure

1. Current version: `git tag --sort=-v:refname | head -1`. Bump semver patch for
   fixes, minor for features.
2. Edit all four files above to the new version.
3. `pnpm check` in `client/` (svelte-check) must pass. Run
   `transcripter-test-suite` gates first if Rust/server code changed.
4. Commits (conventional, user prefers them): feature/fix commits first, then a
   dedicated `chore(release): vX.Y.Z` commit containing ONLY the four version
   files.
5. `git push` (main), then `git tag vX.Y.Z && git push origin vX.Y.Z`.
6. Confirm CI started: `gh run list --workflow=release.yml --limit=2` — a new
   `Release` run must appear within a minute.

## What CI does (`.github/workflows/release.yml`)

- Trigger: `push` of tags `v*` (or `workflow_dispatch`).
- Matrix: `macos-15` (aarch64-apple-darwin), `macos-15` (x86_64-apple-darwin),
  `windows-2022`. `fail-fast: false`.
- The `android` job (ubuntu-latest) builds a signed universal APK via
  `tauri android build --apk` and uploads it as
  `Transcriptor.Maximus_<version>_universal.apk` with `gh release upload`.
  Signing: env-driven `signingConfigs.release` in
  `client/src-tauri/gen/android/app/build.gradle.kts` fed by secrets
  `ANDROID_KEYSTORE_BASE64` / `ANDROID_KEYSTORE_PASSWORD` /
  `ANDROID_KEY_ALIAS` (= `transcripter`) / `ANDROID_KEY_PASSWORD`. The job
  fails fast if the secrets are absent — never fall back to a debug key:
  an ephemeral signature breaks in-place updates. Keystore backup (with
  passwords) lives outside git at
  `~/projects/backups/transcripter/android-signing/` on the megaserver.
- `pnpm install --frozen-lockfile` in `client/` → `tauri-apps/tauri-action@v0`
  builds and attaches bundles to release `Transcriptor Maximus vX.Y.Z`
  (non-draft, non-prerelease).
- Windows job additionally generates `checksums.txt` (SHA-256 of nsis/msi) and
  uploads it as the `checksums` artifact.

## Post-release verification

- Re-runs of the same tag OVERWRITE assets. After any re-run, verify asset
  timestamps match the run's headSha; `git diff <build-sha>..main -- client/`
  empty means binaries are fresh.
- macOS users must clear quarantine on the unsigned bundle:
  `xattr -cr /Applications/Transcripter.app` (documented in release notes since
  v0.2.2). No notarization — Apple Developer Program is the permanent fix path.
