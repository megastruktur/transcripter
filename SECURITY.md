# Security Policy

## Pinned external images

The compose stack pins these third-party images. They are NOT auto-updated;
bumping any of them is a deliberate act:

| Service     | Image                                          | Pinned tag / version |
|-------------|------------------------------------------------|----------------------|
| diarization | `lintoai/linto-diarization-pyannote`           | `2.3.0`              |
| stt         | `ghcr.io/speaches-ai/speaches`                 | `0.9.0-rc.3-cpu`     |
| postgres    | `postgres`                                     | `16-alpine`          |
| temporal    | `temporalio/auto-setup`                        | `1.28.2`             |
| temporal-ui | `temporalio/ui`                                | `2.35.0`             |

## Pre-update checklist (run for EVERY image bump)

1. **Read the changelog / release notes** for every skipped version between
   the pinned tag and the candidate. For Speaches specifically watch for
   changes to: form-field parsing (`timestamp_granularities[]`), response
   shapes (`words` top-level vs nested), VAD defaults, `PRELOAD_MODELS` env
   semantics, `/health` endpoint.
2. **Verify the exact tag exists** on the registry, including the `-cpu`
   suffix for Speaches (`docker manifest inspect <image>:<tag>`).
3. **Re-run the integration proof after bumping**: Smoke C
   (`STT=speaches bash server/scripts/e2e_smoke.sh`) must still produce
   non-empty `words` in `segments.json` and a diarized transcript.
4. **Prefer digest pinning** for anything security-sensitive:
   `image: ghcr.io/speaches-ai/speaches@sha256:<digest>` (note: compose
   interpolation of `${...}` inside digests is not supported — write the
   digest literally).
5. Update the table above together with the compose change, in the same
   commit.

## Trust boundaries

- LAN-first deployment; bearer-token auth on the API (`TRANSCRIPTER_TOKEN`).
- No secrets in `config.yaml`: API keys are referenced by env var *name*
  (`api_key_env`), never by value.
- Speaches and LinTO containers are trusted internal services on the compose
  network; they are not published to the host by default (diarization excepted
  for debugging on :8070 — close it if not needed).
