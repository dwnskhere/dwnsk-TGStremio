# Jellyfin compatibility — implementation status

## In plain language

The goal is to let the **Jellyfin Android app** connect to the same Heroku address that already runs Telegram-Stremio. Your phone runs the Jellyfin app. Heroku continues running Telegram-Stremio. There is no separate Jellyfin installation in this design.

This is an experimental **Jellyfin-compatible API layer**, not the official Jellyfin Server. The official Android app may ask for extra API behavior that has not been implemented yet.

## What has been added to this repository

- A new API module: `Backend/fastapi/routes/jellyfin_routes.py`.
- Heroku settings for enabling the feature and choosing a separate Jellyfin-compatible username/password.
- Server information, login, session tokens, logout and token expiry.
- Movie and TV library views, including Series → Season → Episode browsing.
- Basic posters, backdrops and media details.
- Playback information and a protected stream route that reuses Telegram-Stremio's existing Telegram streaming code.
- Byte-range requests are passed to the existing streamer so seeking can work.
- Playback progress storage.
- A simple Android setup guide: `JELLYFIN_ANDROID_SETUP.md`.
- An implementation reference: `docs/JELLYFIN_COMPATIBILITY_PLAN.md`.
- A local API contract smoke test: `tools/jellyfin_compat_contract_smoke_test.py`.

## What you need to configure after deploying

Add these four values in Heroku → your app → Settings → Config Vars:

- `JELLYFIN_COMPAT_ENABLED=true`
- `JELLYFIN_USERNAME=<a new username you choose>`
- `JELLYFIN_PASSWORD=<a new strong password>`
- `JELLYFIN_SERVER_NAME=Telegram-Stremio`

The Jellyfin login is separate from your Stremio login. The backend issues a separate Jellyfin session token. For video playback, the server-side bridge uses the STRM default API token or an existing active Telegram-Stremio API token; that token is not sent to the Jellyfin app.

## Checks completed in this workspace

The following commands completed successfully:

```bash
python -m compileall -q Backend tools
python tools/jellyfin_compat_contract_smoke_test.py
```

The smoke test uses an in-memory fake MongoDB and a stub for the Telegram streamer. It passed checks for:

- Public server info and route registration.
- Separate username/password login and token issuance.
- Jellyfin's SHA-1 password-field compatibility.
- Authenticated user and library views.
- Movie and TV show/season/episode browsing.
- Item details and runtime metadata.
- Playback-info generation.
- Media URL authentication using the Jellyfin token only.
- Forwarding an HTTP byte-range request to the existing stream handler.
- Logout and rejection of a revoked token.

## What has NOT been tested

This workspace does not contain a live connection to your Heroku app, your real MongoDB, your Telegram userbot, or your Android phone. Therefore these are **local code checks, not a real end-to-end streaming test**.

The official Jellyfin Android app has not yet confirmed that it accepts this API layer. It may request more endpoints or additional response fields. If it rejects the server or a screen fails, the next step is to inspect the app's exact error and the matching Heroku logs, then implement the missing request/response behavior.

## Important limitations in this first build

- One Jellyfin-compatible account is configured through Heroku Config Vars.
- Quick Connect is not implemented; use the separate username/password.
- No server-side video transcoding is implemented.
- The selected source API token still applies its existing expiry/subscription/data limits.
- Quality selection currently chooses the best available quality by label; there is no in-player quality picker.
- Subtitle selection, favorites and full Jellyfin user management are not included.

## Where to start

Follow `JELLYFIN_ANDROID_SETUP.md` in order. First open `/System/Info/Public` in your phone browser. Then add the Heroku base URL (without `/stremio` or any extra path) in Jellyfin Android and try signing in.
