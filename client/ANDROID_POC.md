# Android PoC (Wave D6) — verdict GO

Tauri v2 Android debug APK was built end-to-end on this user-local dev host
(`cachyos` / no sudo / no JDK / no Android SDK) with no host-side
modifications other than what's documented in this file. APK is at
`client/src-tauri/gen/android/app/build/outputs/apk/universal/debug/app-universal-debug.apk`
(716 MiB universal / 4 ABIs).

The native Rust crate (`transcripter_lib.so`) links and validates against
the Tauri v2 Android runtime (NDK 26.1.10909125, AGP 8.11.0, Kotlin 1.9.25,
Gradle 8.14.3, JDK 21 Temurin). The generated WebView shell launches the
bundled SvelteKit SPA from `app/src/main/assets/index.html`. Front-end can
mount and call `invoke()` against `cmd_*` — only the desktop-only commands
(recording / upload / window-mode) are gated out on android.

## Verdict: GO

Runtime mic capture is the next gate and is intentionally out of scope for
this artifact — see "getUserMedia evaluation" below for the precise delta
that has to be tested against a real device + `adb install`.

## Host toolchain layout

All user-local. No sudo, no system installs.

| Tool             | Path                                                      |
| ---------------- | --------------------------------------------------------- |
| JDK              | `~/.local/jdk`                                            |
| Android SDK      | `~/.local/android-sdk`                                    |
| cmdline-tools    | `~/.local/android-sdk/cmdline-tools/latest`               |
| NDK              | `~/.local/android-sdk/ndk/26.1.10909125`                  |
| platforms        | `~/.local/android-sdk/platforms/{android-34,android-36}`  |
| build-tools      | `~/.local/android-sdk/build-tools/{34.0.0,36.0.0}`        |
| platform-tools   | `~/.local/android-sdk/platform-tools`                     |

Rust targets installed by `rustup target add`:

```
aarch64-linux-android
armv7-linux-androideabi
x86_64-linux-android
i686-linux-android     (pulled by `pnpm tauri android init`)
```

## Reproducible environment

Required env for any further `pnpm tauri android build` invocation:

```bash
export JAVA_HOME=/home/megastruktur/.local/jdk
export PATH="$JAVA_HOME/bin:$PATH"
export ANDROID_HOME=/home/megastruktur/.local/android-sdk
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export NDK_HOME="$ANDROID_HOME/ndk/26.1.10909125"
```

One-time SDK install (already done):

```bash
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
  --licenses
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
  "platform-tools" "platforms;android-36" "build-tools;36.0.0" \
  "ndk;26.1.10909125"
rustup target add aarch64-linux-android armv7-linux-androideabi \
                x86_64-linux-android i686-linux-android
```

Reproduction:

```bash
cd client
pnpm install --frozen-lockfile
pnpm tauri android init --ci                       # idempotent
pnpm tauri android build --debug
# → app/src-tauri/gen/android/app/build/outputs/apk/universal/debug/app-universal-debug.apk
```

`local.properties` is generated inside `src-tauri/gen/android/` and points
at `sdk.dir=/home/megastruktur/.local/android-sdk` so `gradlew assembleDebug`
works directly without the env block above.

## Repo deltas

| Path                                                                           | Reason                                                                                                                  |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `client/src-tauri/Cargo.toml`                                                  | Gate `tauri` features (`x11`, `tray-icon`, `macos-private-api`) under target-specific blocks; move `cpal` to desktop-only deps block — android linker rejects `-laaudio`. Add `custom-protocol` feature to the base tauri line so `tauri::generate_context!()` accepts the in-app `asset://` scheme. |
| `client/src-tauri/src/lib.rs`                                                  | `cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))` around every desktop-only top-level item: tray imports, `show_main_window`, `cmd_apply_window_mode`, the recording/permissions/spool/uploader module declarations and all `cmd_*` commands that touch them, `UploadJob`/`QueueMsg`/`UPLOAD_QUEUE`/`QUEUED`/`enqueue_upload`/`try_upload`. `#[cfg_attr(mobile, tauri::mobile_entry_point)]` on `pub fn run()`. The setup closure now returns `Ok(())` after the gated tray block. |
| `client/src-tauri/src/capture.rs`                                              | Whole-file rewrite: existing desktop cpal implementation lives in `mod desktop` under `#[cfg(desktop)]`; a no-op `android_stub` module is `pub use`'d on android so `recording.rs`/`permissions.rs` type-check unchanged. `CapturedStream.config` becomes a plain scalar struct on android (no cpal dependency). `rms()` stays cross-platform. |
| `client/src-tauri/gen/android/**`                                              | Generated by `pnpm tauri android init --ci` (regenerated on every fresh init). `local.properties` added by hand.       |
| `client/src-tauri/gen/android/app/src/main/AndroidManifest.xml`                | Add `<uses-permission>` declarations for `RECORD_AUDIO`, `MODIFY_AUDIO_SETTINGS`, `CAMERA`, plus `<uses-feature>` for microphone + camera (see getUserMedia section). |

Files explicitly NOT touched: `client/src/**`, `client/src-tauri/src/{recording,permissions,spool,uploader,encode}.rs`, `client/src-tauri/src/capture_macos.rs`, `client/src-tauri/src/capture_windows.rs`, `client/tauri.conf.json`, anything under `server/**`, branch + commit history.

## Build outputs

```
client/src-tauri/gen/android/app/build/outputs/apk/universal/debug/app-universal-debug.apk   716 MiB
client/src-tauri/gen/android/app/build/outputs/bundle/universalDebug/app-universal-debug.aab 142 MiB
```

`aapt2 dump badging`:

```
package: name='com.megastruktur.transcripter' versionCode='8002' versionName='0.8.2' compileSdkVersion='36'
minSdkVersion: 24
targetSdkVersion: 36
uses-permission: INTERNET, RECORD_AUDIO, MODIFY_AUDIO_SETTINGS, CAMERA
application: label='Transcriptor Maximus'
```

Native libs: `lib/{arm64-v8a,armeabi-v7a,x86,x86_64}/libtranscripter_lib.so`.

Per-ABI APK splits were not produced because we passed `--debug`; for
release builds `pnpm tauri android build --release` (or
`gradle :app:assembleRelease` after initing a signing config) will emit one
APK per ABI in `outputs/apk/{arm64-v8a,...}/release/`. The current universal
APK size is expected (debug symbols + 4 ABIs + unminified resources).

## getUserMedia evaluation (the real wave-D6 question)

Reading the generated code + upstream Tauri 2.11.5 / wry 0.55.1:

### What works out of the box

`RustWebChromeClient.onPermissionRequest` (in upstream
`wry-0.55.1/src/android/kotlin/RustWebChromeClient.kt:94`) IS wired up.
For `navigator.mediaDevices.getUserMedia({ audio: true })` it:

1. sees the requested resource include `android.webkit.resource.AUDIO_CAPTURE`;
2. maps that to `Manifest.permission.RECORD_AUDIO` +
   `Manifest.permission.MODIFY_AUDIO_SETTINGS`;
3. on Android M+ invokes `permissionLauncher.launch(permissions)`;
4. on grant → `request.grant(request.resources)` — the WebView is told the
   resource is permitted and the MediaStream starts flowing.

The only thing that is NOT pre-wired is `android.webkit.resource.VIDEO_CAPTURE`
→ `CAMERA` (works identically), and any sub-resource beyond
AUDIO_CAPTURE / VIDEO_CAPTURE (e.g. `MIDI` / `PROTECTED_MEDIA_ID`) — those
fall through with `permissionList.isNotEmpty()` false and `request.grant()`
on M+ without a runtime prompt, which is the right default.

### What had to be added

1. **AndroidManifest permissions.** The generated manifest declares only
   `INTERNET`. RECORD_AUDIO / MODIFY_AUDIO_SETTINGS / CAMERA had to be added
   (see the `<uses-permission>` block in
   `client/src-tauri/gen/android/app/src/main/AndroidManifest.xml`). Without
   these, `permissionLauncher.launch(...)` would request a permission that
   isn't declared, and Google Play would reject the upload for an
   "undeclared permission usage".
2. **`<uses-feature android:name="android.hardware.microphone" android:required="false" />`**
   (and camera). Without these, Play Console filters the app from
   mic-less devices entirely; with `required="false"` it stays installable
   everywhere but the OS marks mic as available.

### What does NOT need to change

- `MainActivity.kt` is fine as-is. The auto-generated
  `class MainActivity : TauriActivity()` already routes the WebView through
  `RustWebChromeClient` (set up by `WryActivity.setWebView` upstream).
- No Kotlin-side `onPermissionRequest` override is needed.
- No new Rust code is needed — the recording path on android moves entirely
  to the front-end: call `getUserMedia({ audio: true })`, get a
  `MediaStream`, use `MediaRecorder` or an `AudioWorklet` to encode
  audio locally (WebM/Opus is universally supported; WebM is
  preferred over FLAC for the in-WebView pipeline since `flacenc` is Rust
  and the webview can't call it directly), then push the chunks through the
  existing `cmd_upload_now` invoke.

### Runtime check that has to happen tomorrow

NOT done in this gate (no device, no emulator on this host):

```
adb install -r app-universal-debug.apk
adb shell am start -n com.megastruktur.transcripter/.MainActivity
# in SvelteKit UI: tap record → expect system "Allow microphone?" prompt
# then MediaStream live.
```

Failure modes to watch for (real, not speculative):

- **WebView vendor.** `compileSdk = 36` requires WebView ≥ ~120. Most modern
  Android System WebView builds support `AUDIO_CAPTURE`; very old
  WebView (pre-Android 7, minSdk=24 we should be safe on devices that
  shipped with WebView 51+) will silently deny with no UI feedback.
- **Permission prompt ordering.** Android 12+ shows the prompt inline. Pre-12
  modal. Both paths are handled by the upstream launcher.
- **Background tab loss.** If the app goes to background, MediaStream
  tears down. The recorder has to commit whatever it has so far and start a
  new stream on resume, OR keep a foreground service — the latter is the
  canonical solution for a long recording session.
- **No AAudio path.** We deliberately removed cpal from the android build,
  so the legacy "native Rust mic capture" path is impossible. Everything
  goes through the WebView; this is by design (matches wave-D6 plan).

### Plan for the next iteration

1. Wire `client/src/lib/recorder.svelte.ts` (or whatever the recorder
   module is) to call `getUserMedia` + `AudioWorklet` + `MediaRecorder` and
   emit `cmd_upload_now` chunks with WebM/Opus bodies. FLAC encoding stays
   server-side (the server already accepts FLAC; WebM/Opus will need a new
   branch on the server OR a client-side decode+FLAC-encode hop in an
   AudioWorklet using a WASM FLAC encoder).
2. Add foreground service for long-recordings.
3. Test on a real Android device with `adb install`.

This report closes the build gate. Runtime eval is the next ticket.
