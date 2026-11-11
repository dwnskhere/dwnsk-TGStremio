# What changed for Jellyfin Android

This note is the plain-language overview of the implementation. The detailed setup procedure is in `JELLYFIN_ANDROID_SETUP.md`; technical limits and test status are in `JELLYFIN_COMPATIBILITY_STATUS.md`.

## The goal

Use the Jellyfin Android app on your phone to browse and play the movies and shows already stored in Telegram through your existing Telegram-Stremio Heroku app. You do not deploy a second Jellyfin server.

## What the new code adds

- A Jellyfin-compatible server identity and API inside the existing FastAPI application.
- The official Jellyfin 10.10.7 Web interface files, copied into the application image during Docker build. Only static website files are copied; the Jellyfin Server process is not run.
- A separate Jellyfin username and password. After login, the service gives the Android app a separate Jellyfin session token.
- Movie, show, season and episode browsing based on the Telegram-Stremio catalogue.
- Posters and metadata when Telegram-Stremio already has those values.
- Direct playback through the existing Telegram stream handler, including HTTP Range/seek support and the existing subtitle index.
- Per-device sessions, logout/revocation, short-lived scoped playback links and protections against exposing the Telegram-Stremio source token.
- Health checks and automated contract/deployment tests.

## What is deliberately not part of this version

- No Quick Connect; use the separate username/password.
- No transcoding. The Android player must support the video's original format.
- No Live TV, music library, multi-user account management, Jellyfin plugins or full Jellyfin admin dashboard.
- No second Jellyfin installation, VPS, NAS or persistent video directory.

## How we know it works so far

The local automated checks pass. They use fake database records and a fake Telegram stream response. They check the API contracts, session/token protections, library filtering, stream URL construction, HTTP Range forwarding logic, update safety and frontend asset checks.

Those tests do not prove that a real Telegram file streams through a deployed Heroku container or that the user's Android app accepts the whole service. The GitHub Actions Docker build must pass, then the live app's `/Jellyfin/Compat/Health` endpoint and the phone tests in `JELLYFIN_ANDROID_SETUP.md` are required before calling it end-to-end verified.

## Important deployment fact

The Jellyfin Web interface is copied from the official Jellyfin 10.10.7 container image during build. This feature therefore requires the Heroku **container** stack, not a normal buildpack-only deployment. The manual GitHub Actions deployment workflow runs the Python compatibility and deployment checks before it releases the image.
