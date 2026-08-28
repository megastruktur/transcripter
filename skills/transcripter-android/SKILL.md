---
name: transcripter-android
description: Build the transcripter Android client (Tauri v2 + SvelteKit) end-to-end on a user-local Android toolchain, install the universal debug APK on a device or emulator via adb, launch the MainActivity, verify the on-device capture path (system mic → getUserMedia → MediaRecorder webm/opus → POST /recordings/direct), and tail logcat / chrome://inspect for debugging. Use when asked to "build android apk", "install on phone", "adb transcripter", "android client debug", "android smoke test", or anything that touches the Tauri-Android build, install, or on-device run.
metadata:
  version: "1.0"
---

# transcripter-android

Build, install, launch, and debug the Transcriptor Maximus Android client.
The Android client ships the same SvelteKit SPA as the desktop build (see
`transcripter-client-build`), but the capture path is web-only: there is no
Rust-side `cmd_*` capture IPC on Android (gated out by `cfg(desktop)` in
`client/src-tauri/src/lib.rs`), so recording goes through `getUserMedia` →
`MediaRecorder` → a single `POST /recordings/direct` to the server.

The server stack must be up before the app is useful on device — see
`transcripter-stack-up`. Pipeline behaviour, artifact paths, and stage states
on the server side are unchanged.

## When to use / when NOT to use

Use this skill for:

- Building the APK (`pnpm tauri android build --debug`).
- Installing and launching on a real device or emulator via `adb`.
- Verifying the on-device capture → upload → pipeline loop end-to-end.
- Reading `logcat` for the app process, or opening the WebView in
  `chrome://inspect`.

Do NOT use this skill for:

- Desktop client builds, releases, or signing → `transcripter-client-build`,
  `transcripter-release-ops`.
- Server bring-up, pipeline stages, server tests → `transcripter-stack-up`,
  `transcripter-e2e-smoke`, `transcripter-test-suite`.
- Release builds of the Android APK: `release.yml` ships desktop bundles only
  (macOS × 2 + Windows). Android release builds, signing, and Play Store
  publication are an open item — see "Release builds (open item)" below.

## Prerequisites (host side)

User-local toolchain on the dev host. No sudo, no system installs.

| Tool           | Path                                                |
| -------------- | --------------------------------------------------- |
| JDK (Temurin 21) | `~/.local/jdk`                                    |
| Android SDK    | `~/.local/android-sdk`                              |
| cmdline-tools  | `~/.local/android-sdk/cmdline-tools/latest`         |
| NDK            | `~/.local/android-sdk/ndk/26.1.10909125`            |
| platforms      | `~/.local/android-sdk/platforms/{android-34,android-36}` |
| build-tools    | `~/.local/android-sdk/build-tools/{34.0.0,36.0.0}`  |
| platform-tools | `~/.local/android-sdk/platform-tools`               |

Rust targets required (Android linker rejects `-laaudio`, so `cpal` is
desk-only — see `client/src-tauri/src/lib.rs`):

```
aarch64-linux-android
armv7-linux-androideabi
x86_64-linux-android
i686-linux-android    # pulled in by `pnpm tauri android init`
```

Tauri CLI is `@tauri-apps/cli ^2.0.0` (`client/package.json`).

### Required env block (every shell)

```bash
export JAVA_HOME=/home/megastruktur/.local/jdk
export PATH="$JAVA_HOME/bin:$PATH"
export ANDROID_HOME=/home/megastruktur/.local/android-sdk
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export NDK_HOME="$ANDROID_HOME/ndk/26.1.10909125"
```

`local.properties` inside `client/src-tauri/gen/android/` already pins
`sdk.dir=/home/megastruktur/.local/android-sdk`, so `./gradlew assembleDebug`
works directly without the env block — but the Tauri CLI shell commands want
the env above.

### One-time SDK install (already done on this dev host)

```bash
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
  "platform-tools" "platforms;android-36" "build-tools;36.0.0" \
  "ndk;26.1.10909125"
rustup target add aarch64-linux-android armv7-linux-androideabi \
                x86_64-linux-android i686-linux-android
```

## Build

From the repo root:

```bash
cd client
pnpm install --frozen-lockfile
pnpm tauri android init --ci                  # idempotent; safe to re-run
pnpm tauri android build --debug
```

Output:

- APK: `client/src-tauri/gen/android/app/build/outputs/apk/universal/debug/app-universal-debug.apk`
  - **~716 MiB** — universal / 4 ABIs (arm64-v8a, armeabi-v7a, x86, x86_64) +
    debug symbols + unminified resources. Per-ABI release builds are
    ~10× smaller (see "Per-ABI / release builds" below).
- AAB: `client/src-tauri/gen/android/app/build/outputs/bundle/universalDebug/app-universal-debug.aab` (~142 MiB).

Confirmed package metadata (from `aapt2 dump badging` on this host):

```
package: name='com.megastruktur.transcripter' versionCode='8002' versionName='0.8.2' compileSdkVersion='36'
minSdkVersion: 24
targetSdkVersion: 36
uses-permission: INTERNET, RECORD_AUDIO, MODIFY_AUDIO_SETTINGS, CAMERA
application: label='Transcriptor Maximus'
```

Native libs: `lib/{arm64-v8a,armeabi-v7a,x86,x86_64}/libtranscripter_lib.so`.

## Install + launch (adb)

```bash
# device connected? (USB debugging on, "Allow USB debugging" accepted)
adb devices

# install (replace if present)
adb install -r client/src-tauri/gen/android/app/build/outputs/apk/universal/debug/app-universal-debug.apk

# launch
adb shell am start -n com.megastruktur.transcripter/.MainActivity
```

Activity is `MainActivity` (auto-generated `TauriActivity` subclass at
`client/src-tauri/gen/android/app/src/main/java/com/megastruktur/transcripter/MainActivity.kt`).
Package id (`applicationId`) is `com.megastruktur.transcripter`.

## On-device debugging

### Logcat (Rust + WebView console)

The debug build's `buildTypes.debug` (`client/src-tauri/gen/android/app/build.gradle.kts`)
sets `isDebuggable = true`, `isJniDebuggable = true`, so logcat shows both
the native Rust crate and the WebView JS console. Filter to the app:

```bash
adb logcat -c                                                      # clear
adb logcat \
  --pid=$(adb shell pidof -s com.megastruktur.transcripter) \
  chromium:V *:S
```

`chromium:V` surfaces WebView console messages (errors, warnings, `console.log`);
add `*:S` to silence everything else. Drop the filter and grep instead:

```bash
adb logcat -d | grep -E 'transcripter|chromium|MediaRecorder'
```

If `pidof` is missing on the device's busybox, fall back to
`adb shell ps -A | awk '/com.megastruktur.transcripter/ {print $2}'`.

### Chrome DevTools (chrome://inspect)

Tauri v2 debug builds expose the WebView to Chrome DevTools
(`chromium`-based Android WebView). The Tauri docs confirm the procedure
("Opening the Web Inspector > Android"):

1. Enable **Developer Mode** + **USB Debugging** on the device.
2. Plug the device in over USB, accept the "Allow USB debugging?" prompt.
3. On the host, open `chrome://inspect/#devices` in Google Chrome.
4. The device + the running `Transcriptor Maximus` WebView appear under
   "Remote Target". Click **inspect** to attach DevTools.

The Tauri docs note the runtime WebView is the system's updatable
`Android System WebView` (Chromium-based); there is no bundled WebView, so
`getUserMedia`/`MediaRecorder` support depends on the device's currently
selected WebView provider — see "Known pitfalls" below.

## How capture works on Android (current state)

Post-`d1c0591`, the Android path has **no** Rust capture IPC. Flow:

1. App boot. `MainActivity` extends `TauriActivity` and loads
   `app/src/main/assets/index.html` (the SvelteKit SPA built by `pnpm build`).
   Window chrome (collapse / minimize / close) is hidden in the layout
   (`client/src/routes/+layout.svelte`), the OS owns the window.
   On Android the left navigation rail becomes an overlay drawer (toggled by
   tapping the app sigil top-left; 160ms slide, scrim/Esc/nav-click close),
   and the record page dissolves its panel chrome and pins the record button
   to the bottom for thumb reach. The titlebar/status-strip absorb `env(safe-area-inset-top/bottom)` so the UI
   clears the status bar and gesture pill (needs `viewport-fit=cover` in
   `client/src/app.html` and a WebView ≥ 140 — older WebViews report the
   insets as 0, see tauri-apps/tauri#14142). Desktop layout is untouched
   (all rules are gated on the `shell--android` class / `isAndroidTauri()`).
2. Settings → Capture devices panel is replaced by a note
   ("Android manages the input device … the microphone permission prompt
   appears when you start a recording"), see
   `client/src/routes/settings/+page.svelte`.
3. The shared store (`client/src/lib/stores.svelte.ts`) reports a single
   pseudo device `ANDROID_MIC_ID = 'android-system-mic'` (`System microphone`)
   and skips every desktop-only IPC call
   (`cmd_list_audio_devices`, `cmd_pre_flight`, …).
4. Record page tap → `startMobileRecorder(...)` in
   `client/src/lib/mobile-recorder.ts` calls
   `navigator.mediaDevices.getUserMedia({ audio: true })`.
5. The WebView's `RustWebChromeClient.onPermissionRequest`
   (upstream wry `0.55.1/src/android/kotlin/RustWebChromeClient.kt`) maps the
   requested `android.webkit.resource.AUDIO_CAPTURE` resource to
   `RECORD_AUDIO` + `MODIFY_AUDIO_SETTINGS`, calls
   `permissionLauncher.launch(permissions)`, and grants the resource on
   approval. Grant lives in the manifest (added by hand, see "Permissions" below).
6. `pickRecorderMime()` probes
   `audio/webm;codecs=opus → audio/webm → audio/ogg;codecs=opus → audio/ogg → audio/mp4;codecs=mp4a.40.2`
   and uses whatever `MediaRecorder.isTypeSupported` accepts. webm/opus is
   canonical; the server transcodes non-FLAC bodies to canonical FLAC via
   ffmpeg (`POST /recordings/direct`).
7. `MediaRecorder.start(1000)` — 1 s timeslice feeds `ondataavailable`.
8. Stop → `recorder.stop()` → final `Blob` → `uploadDirect(cfg, blob, title, tags, durationSec)`
   in `client/src/lib/api.svelte.ts`, which POSTs multipart to
   `POST /recordings/direct`. Server seeds the standard pipeline (same
   finalize + stages as a desktop recording).
9. UI shows `recording queued for processing (<uuid-prefix>…)` on success, or
   surfaces a failed-upload block with a "Retry upload" affordance (the blob
   stays in component state — see "Known pitfalls" for why this is in-memory).

## Permissions

Generated manifest at
`client/src-tauri/gen/android/app/src/main/AndroidManifest.xml` declares
(only the audio/camera bits are non-stock Tauri — they were added by hand
per `ANDROID_POC.md` so Google Play does not reject on undeclared-permission
usage):

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.RECORD_AUDIO" />
<uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS" />
<uses-permission android:name="android.permission.CAMERA" />
<uses-feature android:name="android.hardware.microphone" android:required="false" />
<uses-feature android:name="android.hardware.camera" android:required="false" />
```

`<uses-permission>` is the **declaration**; the actual runtime prompt comes
from `permissionLauncher.launch(RECORD_AUDIO)` triggered by
`RustWebChromeClient.onPermissionRequest`. Both are required — without the
manifest declaration, `permissionLauncher` requests a permission the manifest
does not name, and Play rejects the upload.

The debug build additionally sets
`manifestPlaceholders["usesCleartextTraffic"] = "true"` so the WebView can
hit `http://<lan-host>:8090` (the dev server) without TLS. Release builds
default to `false` — cleartext to the LAN transcription server will be
blocked unless you re-enable it or front the server with TLS.

## Runtime verification checklist (what "working" looks like on device)

1. **App launches.** `am start` returns and logcat shows the WebView loading
   `file:///android_asset/index.html`. No Rust panic in the first 5 s.
2. **Settings loads.** Tap Settings → server address is the LAN URL
   (`http://<dev-host>:8090` from the device's perspective), bearer token
   matches `server/.env`. "Test and save connection" returns
   `Connection established`.
3. **Capture devices note visible.** Settings → "Capture devices" panel
   shows the Android-specific note, NOT the desktop mic/system dropdowns.
4. **Record prompt.** Tap Record → first tap shows the Android system
   microphone permission prompt ("Allow <app> to record audio?"). Grant.
   The recorder's `ready` promise resolves once `MediaRecorder.start` fires
   (a denied prompt rejects with the error string
   `microphone blocked — allow it in the system prompt` from
   `normalizeMediaError`).
5. **Timer runs.** UI shows a running clock; `ondataavailable` fires every 1 s.
6. **Stop → upload.** Tap Stop. The blob POSTs to `/recordings/direct`.
   On success, the warning strip shows
   `recording queued for processing (<uuid-prefix>…)`.
7. **Library entry.** Open the Library page; a card with the new
   recording id appears, state `uploading` → `processing`.
8. **Pipeline completes.** Stages move through
   `transcribe → diarize → merge_speakers → summarize`
   (`done,done,done,skipped` is the expected SUCCESS on default config —
   `summarize` skips until a model is configured in `server/config.yaml`;
   see `transcripter-stack-up` and `transcripter-e2e-smoke`).
9. **Artifacts exist** under `server/storage/recordings/<id>/meta/`
   (`transcript.md`, `segments.json`, `diarization.json` iff diarize ran,
   `diarized-transcript.md` iff `merge_speakers` ran).

If any of those fail, jump to `transcripter-troubleshooting` with the logcat
output and the server-side `last_error`.

## Known pitfalls

- **Universal debug APK is ~716 MiB.** 4 ABIs + debug symbols +
  unminified resources. Do not ship this to a tester. Per-ABI release builds
  (next section) drop to ~10–20 MiB per ABI.
- **Mic permission is gated at record start, not at app launch.** A denied
  prompt surfaces as the error string `microphone blocked — allow it in the
  system prompt` (from `normalizeMediaError` in
  `client/src/lib/mobile-recorder.ts`); a missing mic surfaces as
  `no microphone available` (`NotFoundError` / `OverconstrainedError`). The
  desktop "no microphones found" / "Pre-flight failed" errors do not apply
  on Android — they are gated out at the store level.
- **Background tab loss tears down `MediaStream`.** Going to another app or
  turning the screen off ends the capture; the recorder stops, the blob
  POSTs whatever it has so far. There is **no foreground service** — that
  is the open item called out in `ANDROID_POC.md`'s "Plan for the next
  iteration". For long recordings, stay in the foreground.
- **Failed upload keeps the blob in memory only.** The record page stashes
  the blob + title + tags + duration in component state and shows a "Retry
  upload" affordance. Leaving the page loses it. A durable spool
  (IndexedDB / OPFS, or a Rust-side queue) is the explicit PoC follow-up.
- **WebView vendor matters for `getUserMedia`.** Tauri uses the system
  Android WebView (Chromium-based). `compileSdk = 36` implies
  WebView ≥ ~120, which supports `AUDIO_CAPTURE`. Very old WebView builds
  (pre-Android 7, but `minSdk = 24` should be safe on any device that
  shipped with WebView 51+) silently deny without UI feedback. Verify with
  `chrome://inspect/#devices` → the inspected page's User-Agent lists the
  Chrome version.
- **Cleartext is debug-only.** Release builds set `usesCleartextTraffic =
  "false"`. Hitting `http://<lan-host>:8090` from a release build will fail
  with a WebView network error — either front the server with TLS, or
  re-enable cleartext per build type.
- **No native mic path.** cpal is desk-only (Android linker rejects
  `-laaudio`); the desktop Rust mic path is impossible. Everything goes
  through the WebView, by design.
- **First `pnpm tauri android build` is slow.** Gradle downloads + AGP
  initialization + four ABIs to compile. Subsequent builds are minutes.
  Re-runs without `--debug` skip the debug-symbol packaging.
- **`tauri android init` is idempotent with `--ci`.** Re-running it
  regenerates `client/src-tauri/gen/android/**`. The hand-added manifest
  permissions survive only because `pnpm tauri android init --ci` is
  careful — verify the manifest after any non-`--ci` init or a manual edit.
- **APK signing for release is not configured.** See next section.

## Per-ABI / release builds (open item)

`pnpm tauri android build --release` (no `--debug`) produces per-ABI APKs
under `client/src-tauri/gen/android/app/build/outputs/apk/{arm64-v8a,armeabi-v7a,x86,x86_64}/release/`.
They are minified (`isMinifyEnabled = true`) and small (~10–20 MiB each),
but **not signed** — the generated `app/build.gradle.kts` does not set up a
signing config. Signing config + Play upload key + Play Console tracks are
the explicit open items from `ANDROID_POC.md`. Until those land, do not ship
release APKs externally; use the universal debug APK for internal testing
and route desktop releases through `transcripter-release-ops`.

## Release builds (open item)

`transcripter-release-ops` covers the desktop flow only (macOS × 2 + Windows,
`release.yml` matrix). It does **not** publish Android binaries, and the
workflow has no Android runner. Adding `macos-15` → Android or a Linux
Android runner to the matrix is the natural next step, but:

- a signing config (`signingConfigs { release { … } }` in
  `client/src-tauri/gen/android/app/build.gradle.kts`) needs a Play upload
  key + `PLAY_CONFIG_JSON` secret + `tauri-action` Android flags;
- minSdk / targetSdk / cleartext / per-ABI selection belong in
  `tauri.conf.json` (currently absent — defaults are used);
- a Play Console app must exist with the matching `applicationId`
  (`com.megastruktur.transcripter`).

Until those are landed, a desktop release does **not** imply an Android
release — keep them separate tickets.

## Gotchas

- **`pnpm tauri android build --debug` from a repo checkout that hasn't run
  `pnpm install` will fail at the `pnpm build` step.** Always
  `pnpm install --frozen-lockfile` first.
- **`pnpm tauri android init --ci` regenerates `gen/android/**`.** If you
  hand-edited a file in there (e.g. `AndroidManifest.xml`, `build.gradle.kts`),
  re-check it after a fresh `init`. The hand-added permissions in
  `AndroidManifest.xml` and the `local.properties` `sdk.dir` line are the two
  manual edits to watch.
- **`adb install` returns `INSTALL_FAILED_UPDATE_INCOMPATIBLE`** if a
  previously installed APK was signed by a different key (or is a release
  build vs the debug build). Uninstall first:
  `adb uninstall com.megastruktur.transcripter` then re-install.
- **The shell toolchain assumes bash.** `sdkmanager --licenses` and the
  Gradle wrapper work under bash; if your shell is fish/zsh, ensure the
  quoted `yes | sdkmanager --licenses` line does not fork oddly.
- **No sudo on the dev host.** All Android tools are user-local under
  `~/.local/`; do not try to `apt install` them.

## Sources

Local artifacts (verified on this host):

- `client/ANDROID_POC.md` — env block, sdkmanager/rustup one-time setup,
  `pnpm tauri android build --debug` reproduction, output paths,
  getUserMedia + `RustWebChromeClient` wiring.
- `client/src-tauri/gen/android/app/src/main/AndroidManifest.xml` —
  permission declarations (`RECORD_AUDIO`, `MODIFY_AUDIO_SETTINGS`,
  `CAMERA`); manifestPlaceholders `usesCleartextTraffic`.
- `client/src-tauri/gen/android/app/build.gradle.kts` — `compileSdk=36`,
  `minSdk=24`, `targetSdk=36`, debug build types, ABI list, no signing config.
- `client/src-tauri/gen/android/app/src/main/java/com/megastruktur/transcripter/MainActivity.kt`
  — `TauriActivity` subclass, no Kotlin-side `onPermissionRequest` override
  (wry handles it).
- `client/src/lib/mobile-recorder.ts` — `ANDROID_MIC_ID`, `isAndroidTauri`,
  `startMobileRecorder`, `pickRecorderMime`, `normalizeMediaError` strings.
- `client/src/lib/stores.svelte.ts` — Android branch returns
  `ANDROID_MIC_ID` pseudo device, skips `cmd_list_audio_devices` /
  `cmd_pre_flight`.
- `client/src/routes/+layout.svelte` — hides collapse/minimize/close chrome
  on Android.
- `client/src/routes/settings/+page.svelte` — Android note in the capture
  devices panel.
- `client/src/routes/+page.svelte` — record page Android branch,
  `uploadDirect`, `failedUpload` retry path.
- `client/src/lib/api.svelte.ts` — `uploadDirect` multipart POST shape.
- `server/api/app/routes/recordings.py` — `POST /recordings/direct`
  one-shot multipart endpoint (FLAC passthrough, otherwise ffmpeg transcode).
- Commit `d1c0591` (`fix(client): gate desktop audio IPC out of the Android path`)
  — establishes the post-PoC state described above.

Context7 (current Tauri v2 docs, https://github.com/tauri-apps/tauri-docs,
verified 2026-08-28):

- `pnpm tauri android build --apk` — canonical command for an APK
  (debug flag is implicit via the desktop `pnpm tauri build --debug`).
- `chrome://inspect/#devices` → inspect the running WebView (debug
  builds only). Per upstream: enable Developer Mode + USB Debugging on
  the device, accept the USB-debugging prompt, click inspect in Chrome.
- Android runtime WebView is the system's updatable Android System WebView
  (Chromium-based); no bundled WebView — `getUserMedia` support depends on
  the device's currently selected provider.
- Tauri docs do **not** prescribe WebView mic permission manifest
  entries — the `RECORD_AUDIO` / `MODIFY_AUDIO_SETTINGS` / `CAMERA`
  additions are repo-specific (Play Store undeclared-permission guard).
  The `wry` upstream `RustWebChromeClient.onPermissionRequest` is the
  documented runtime-grant path.

Sibling skills:

- `transcripter-client-build` — desktop bundles (`.app`, `.dmg`, `.exe`, `.msi`).
- `transcripter-client-run` — desktop dev-mode launch + UI verification.
- `transcripter-stack-up` — server bring-up in any mode (prerequisite for on-device capture).
- `transcripter-release-ops` — desktop release flow (does NOT cover Android).
- `transcripter-troubleshooting` — symptom → fix table for server + desktop.
- `transcripter-e2e-smoke` — server-side pipeline smoke (works without the app).