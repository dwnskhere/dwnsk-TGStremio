# Jellyfin Android compatibility — implementation status

**Status:** Compatibility implementation prepared; real Heroku/Android/Telegram end-to-end test still required.

## What this is

A Jellyfin-compatible API and official Jellyfin Web static frontend embedded in the existing Telegram-Stremio FastAPI application. Heroku remains the only host. This does **not** run the official Jellyfin Server process and is not a full replacement for it.

## Implemented in this repository

- Server discovery and information endpoints using Jellyfin 10.10.7 identity values.
- Pre-login capability negotiation, anonymous keep-alive WebSocket and bootstrap endpoints.
- Official Jellyfin Web static files copied from the matching `jellyfin/jellyfin:10.10.7` image at Docker build time; only the frontend assets are copied into the final image.
- Separate configured Jellyfin username/password and a separate opaque, server-issued token for each device session.
- Session expiry, logout/revocation, basic authentication throttling, JSON 401 responses for Jellyfin API routes and request-token redaction in Uvicorn logs.
- User display preferences and session views.
- Stable UUID5 IDs for movies, series, seasons and episodes. Internal MongoDB and Telegram references are not returned in item IDs.
- Existing Telegram-Stremio visibility checks so private, token-restricted, owner-only and exclusive-catalog titles are not exposed to a broader Jellyfin view than the configured source token permits.
- Movie / TV Show / Season / Episode navigation, common item filtering, search, latest, resume, Next Up, played and favorite state.
- Authenticated Live TV capability information reports Live TV as disabled, so the client does not expose unsupported tuner/guide features.
- TMDB artwork and metadata routes.
- Playback information and direct media URLs protected by short-lived, item-scoped playback tickets bound to the Jellyfin session. The long-lived session token is not embedded in video or subtitle URLs. Both stream URL forms (with and without a MediaSourceId path segment) are accepted, and a supplied MediaSourceId is validated.
- Existing Telegram-Stremio stream handler reused internally with its source API token kept server-side.
- HTTP range and HEAD requests forwarded to the existing streaming handler.
- External subtitle tracks from Telegram-Stremio's existing subtitle index, with Jellyfin subtitle stream URLs protected by the Jellyfin token.
- Readiness endpoint that checks Web frontend assets, source-token validity, MongoDB connectivity and Telegram stream-client availability.
- Heroku-safe writable log path and protection against the legacy updater overwriting the compatibility build.
- API contract smoke tests, deployment-safety tests, static Web asset verification, token-redaction tests, Docker build CI and optional manual Heroku Container Registry deploy workflow. The manual deploy workflow now runs compile/API/deployment checks before pushing or releasing a container.

## Local checks executed in this workspace

The following completed successfully:

```bash
python -m compileall -q Backend tools update.py
python tools/jellyfin_compat_contract_smoke_test.py
python tools/deployment_safety_smoke_test.py
```

The contract test covers server discovery, pre-login capability handshake, anonymous and authenticated WebSocket keep-alive, separate login/token, display preferences, movie/TV browsing and filters, visibility restrictions, Live TV disabled-capability response, stable IDs across simulated cache/dyno restart, progress/resume/favorites, subtitle track mapping, media URL token separation, both stream URL variants, source-ID validation, mocked byte-range forwarding, logout and OpenAPI route registration.

## What these checks do not prove

- Docker was not installed in the editing environment, so the actual Docker image could not be built locally. A GitHub Actions Docker build job has been added to perform that check when the branch is pushed.
- The contract test uses a fake MongoDB and a fake Telegram stream handler. It does not prove a real Telegram download or real seek operation.
- The Jellyfin Android app has not been tested against this final build.
- The actual Heroku Container stack deployment, startup and readiness endpoint have not been verified from this workspace.

## Deployment requirement

The Jellyfin Web assets are introduced by the Dockerfile. Deploying this code with the normal Heroku buildpack stack will omit those assets and will not work. The Heroku app must use the `container` stack. The repository provides `heroku.yml` and a manual GitHub Actions workflow for building/pushing/releasing the container. See `JELLYFIN_ANDROID_SETUP.md`.

## Known intentional limits

- One configured Jellyfin-compatible account (multiple device sessions are supported).
- No Quick Connect; use username/password.
- No server-side transcoding; direct play/direct stream only.
- No Live TV, music-only library, Jellyfin plugin management, server administration UI, multi-user management or media-library write operations.
- Codec/container compatibility and real seeking depend on the existing Telegram stream response and Android playback engine.

## Acceptance criteria for calling the integration usable

The feature should not be considered complete until a real Android device can:

1. Connect to the Heroku base URL.
2. Reach login and authenticate with the separate Jellyfin credentials.
3. Browse Movies and TV Shows → Seasons → Episodes.
4. Load artwork and item details.
5. Play at least one real Telegram-hosted movie and episode.
6. Seek forward/backward using HTTP byte ranges.
7. Load an indexed external subtitle track.
8. Resume playback and persist watched/favorite state.

See `JELLYFIN_ANDROID_SETUP.md` for the user test procedure and safe diagnostics.
