# Telegram-Stremio + Jellyfin Android: shared project reference

**Current implementation snapshot:** November 11, 2026  
**Deployment:** Existing Heroku app, using the Container stack  
**Client:** Jellyfin Android  
**Purpose:** One reference that explains what we are building, how it works, what it supports, and what still needs a real test.

## Read this first

You have Telegram-Stremio on Heroku and the Jellyfin Android app on your phone. You do not have another computer or VPS running Jellyfin Server.

We are adding a **Jellyfin-compatible interface to the existing Telegram-Stremio app**. The Jellyfin phone app will connect to your Heroku URL. Heroku will answer its requests, return your movie/show catalogue and route playback to Telegram.

This is **not the official Jellyfin Server** and it is not a full replacement for that server. It is a focused compatibility layer for the Android app and the media types that Telegram-Stremio already stores.

## The simple flow

```text
Jellyfin Android on your phone
          |
          | HTTPS: your Heroku app URL
          v
Telegram-Stremio on Heroku
  - Jellyfin Web interface files
  - Jellyfin-compatible API
  - separate Jellyfin login/session token
  - movie/show metadata and artwork
          |
          +--------------------+
          |                    |
          v                    v
Existing MongoDB         Existing Telegram streamer
  - movie/show data        - resolve Telegram file
  - subtitles              - stream bytes
  - Jellyfin sessions      - support byte ranges/seeking
  - playback progress
```

Nothing copies the entire video to Heroku. The phone asks for media; Telegram-Stremio looks up the corresponding Telegram file and streams the requested bytes.

## Important credentials distinction

There are two separate forms of access:

1. **Jellyfin login:** The username and password that you create in Heroku Config Vars. When the phone signs in, Telegram-Stremio issues a new Jellyfin session token. The phone stores and uses this token.
2. **Telegram-Stremio source token:** A valid Telegram-Stremio API token that remains on the Heroku server. It decides which source catalogue items the Jellyfin view may see and authorizes the existing Telegram stream. The phone never receives this source token.

Do not enter the source token into the Jellyfin app or send it in a message.

## What each part does

| Part | Responsibility |
|---|---|
| Jellyfin Android | The app you install and use to browse and play |
| `/web/` static files | The Jellyfin Web interface used by the Android app's embedded WebView |
| Jellyfin compatibility API | Understands Jellyfin-style requests and returns compatible responses |
| MongoDB | Stores the existing catalogue and Jellyfin session/playback/preferences data |
| Telegram streaming layer | Finds and streams the existing Telegram video/subtitle files |
| Heroku Container stack | Builds and runs the application image that includes the web files |

## Implemented scope

- Jellyfin public server discovery and startup information.
- Pre-login capability negotiation, WebSocket keep-alive and bootstrap API responses.
- Official Jellyfin Web static frontend files copied from the matching 10.10.7 container image at build time.
- Separate Jellyfin username/password and a server-issued session token, with per-device session records and logout/revocation.
- Movie and TV Show library views, then Seasons and Episodes.
- Metadata, TMDB artwork, common search/filter/sort behaviour and user playback state.
- Stable UUID item identifiers that do not expose MongoDB object IDs or Telegram file IDs.
- Source-token visibility checks for public, token-restricted, owner-only and catalogue-exclusive content.
- Direct playback through the existing Telegram stream handler, forwarding HTTP ranges and HEAD requests.
- External subtitle track mapping from the existing Telegram-Stremio subtitle index.
- Readiness and diagnostic routes; tests for the API contract and deployment safeguards.

## What we intentionally do not build

| Not included | Reason |
|---|---|
| Full official Jellyfin server process | The goal is to use the existing Heroku app, not host another media server |
| Video transcoding | Would need a transcoding engine and more CPU/memory; first release only direct plays/direct streams |
| Live TV, tuners and DVR | Telegram-Stremio does not provide these sources |
| Music-only or audiobook libraries | Initial catalogue is Movies and TV Shows |
| Jellyfin plugin management or admin dashboard | The compatibility service is not an official Jellyfin server |
| Multiple separately managed Jellyfin users | First version uses one configured account with multiple device sessions |
| Quick Connect | Initial authentication uses username/password |
| Media editing/deletion through Jellyfin | Jellyfin catalogue is read-only; source management stays in Telegram-Stremio |

## Deployable files

| File | Why it exists |
|---|---|
| `Backend/fastapi/routes/jellyfin_routes.py` | Compatibility API, auth, library and playback routes |
| `Backend/fastapi/main.py` | Registers routes and mounts the `/web/` static frontend |
| `Dockerfile` | Copies official Jellyfin Web files into the application image, then installs the Python app |
| `heroku.yml` | Container deployment declaration for Heroku's Docker build flow |
| `.github/workflows/jellyfin-compat-tests.yml` | Runs Python contract tests and builds the Docker image without deploying |
| `.github/workflows/deploy-heroku-container.yml` | Optional manual workflow to set the Container stack, push and release the app image |
| `tools/jellyfin_compat_contract_smoke_test.py` | Tests API request/response behavior with fake database and stream fixtures |
| `tools/deployment_safety_smoke_test.py` | Tests updater/log/static asset safety |
| `JELLYFIN_ANDROID_SETUP.md` | Plain-language deploy and phone-testing guide |
| `JELLYFIN_COMPATIBILITY_STATUS.md` | Implemented features, local test evidence and known limits |

## Deployment rule

The Jellyfin Web interface is added during the Docker image build. A normal Heroku buildpack deployment will not run the Dockerfile and will not include the web frontend.

The Heroku app must use the **`container` stack**. The repository includes a manual GitHub Actions deployment workflow. Before using it, add GitHub repository secrets named `HEROKU_API_KEY` and `HEROKU_APP_NAME`, and turn off the old Heroku automatic buildpack deploy so two deployment paths cannot race. Exact user steps are in `JELLYFIN_ANDROID_SETUP.md`. When compatibility is enabled, the startup script deliberately skips the runtime upstream updater: the official Web assets are generated in the container image and are not stored in the Git branch, so resetting the working tree would delete them.

## How we know whether the app is ready

After deployment, open:

- `/system/info/public` — confirms public product identity.
- `/Jellyfin/Compat/Health` — checks whether the Web files, source token, MongoDB and Telegram streaming client are ready.
- `/web/` — checks that the Jellyfin Web interface is being served.

The health response must report `ReadyForAndroidClient: true` before Android testing begins.

## Local testing versus real testing

Local checks run without the user's production databases, Telegram session or phone. The contract test uses fake MongoDB data and a fake Telegram stream handler. Passing these tests proves API wiring and expected data shapes only; it does not prove that a real Telegram file plays or that the official Android app accepts the service.

The real acceptance test is:

1. Add the base Heroku URL to Jellyfin Android.
2. Sign in with the separate Jellyfin credentials.
3. Browse Movies and TV Shows → Seasons → Episodes.
4. Open artwork/details.
5. Play a real Telegram movie and episode.
6. Seek forward and backward.
7. Select an indexed subtitle.
8. Resume playback and check watched/favorite state.

The feature is not considered proven until these real-device steps pass.
