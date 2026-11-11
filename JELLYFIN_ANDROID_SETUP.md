# Jellyfin Android + Telegram-Stremio — setup and test guide

This guide is written for the person using the Android app. It explains what is being built, what needs to be deployed, and how to test it without needing to understand the source code.

## 1. What this project does

You already have Telegram-Stremio running on Heroku. This change adds a Jellyfin-compatible interface to that same app.

- Your Android phone runs the **Jellyfin app**.
- Heroku continues running **Telegram-Stremio**.
- MongoDB keeps the existing catalogue and the new Jellyfin login/session information.
- Telegram remains the place where the actual video files are stored.
- The Jellyfin app connects to your Heroku address, browses the catalogue and asks Heroku to play the selected Telegram file.

**You do not install Jellyfin Server on your phone and you do not need another VPS, PC or NAS.**

This is a compatibility service built into Telegram-Stremio. It is **not the official Jellyfin Server**. It is designed for one Jellyfin-compatible account, multiple logged-in devices, Movies and TV Shows, direct playback, seeking, playback progress and existing sidecar subtitles. It does not provide every feature of a full Jellyfin server.

## 2. Before deploying

The updated project uses the official Jellyfin 10.10.7 web-client files, but copies only those static website files into the Telegram-Stremio image. It does not start a Jellyfin server process.

That means this build **must be deployed as a Heroku container/Docker app**. A normal buildpack deployment does not run the Dockerfile and will not include the Jellyfin web files. Heroku's Docker deployment instructions require the app's stack to be `container` and use `heroku.yml` or the Container Registry workflow. See the official [Heroku Docker deployment guide](https://devcenter.heroku.com/articles/build-docker-images-heroku-yml).

### Required Heroku Config Vars

Open Heroku Dashboard → your app → Settings → Config Vars. Keep your existing Telegram-Stremio variables. Add or check these values:

| Config Var | Value |
|---|---|
| `JELLYFIN_COMPAT_ENABLED` | `true` |
| `JELLYFIN_USERNAME` | A new username used only for Jellyfin, e.g. `dhanraj-jf` |
| `JELLYFIN_PASSWORD` | A new strong password used only for Jellyfin |
| `JELLYFIN_SERVER_NAME` | `Telegram-Stremio` |
| `JELLYFIN_ALLOW_UPSTREAM_AUTO_UPDATE` | `false` |

Optional settings:

| Config Var | When to use it |
|---|---|
| `JELLYFIN_SOURCE_TOKEN` | Set this to a valid Telegram-Stremio API token if you have not configured a valid STRM default token in the Telegram-Stremio settings. Keep it in Heroku only. Never enter it in Jellyfin Android or send it in chat. |
| `JELLYFIN_SERVER_ID` | Leave blank to let the app generate a stable ID. |
| `JELLYFIN_TOKEN_TTL_DAYS` | Session lifetime in days; default is `30`. |
| `JELLYFIN_PLAYBACK_TICKET_TTL_SECONDS` | Lifetime of an individual video/subtitle URL ticket; default is `21600` seconds (6 hours). Active playback refreshes this ticket, up to a maximum of 24 hours per idle window. |

Do **not** put your Telegram bot token, MongoDB URI, Telegram-Stremio source API token or Heroku API key into Jellyfin Android. The Android app needs only the separate Jellyfin username and password.

## 3. Deploy the Docker version to Heroku

The repository contains:

- `Dockerfile` — copies the official Jellyfin Web static files and builds Telegram-Stremio.
- `heroku.yml` — tells Heroku which Dockerfile to use for a container-stack Git deployment.
- `.github/workflows/jellyfin-compat-tests.yml` — runs code checks and builds the Docker image in GitHub Actions.
- `.github/workflows/deploy-heroku-container.yml` — optional manual GitHub Actions deployment to your Heroku app.

### Recommended path: deploy from the GitHub Actions page

This avoids installing Docker on your own computer, but needs one-time setup.

1. In your Heroku account, open **Account Settings → API Key** and reveal/copy your Heroku API key. Do not send it to anyone.
2. In your GitHub repository, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Add these two repository secrets:
   - `HEROKU_API_KEY` = your Heroku API key.
   - `HEROKU_APP_NAME` = `codecindex-bf9c88aafb49` (the app name from your Heroku URL).
4. In the Heroku Dashboard → Deploy tab, turn off any old automatic buildpack deployment for this app. Use the manual workflow below as the single deployment path so two systems do not deploy over one another.
5. Push the updated repository to GitHub.
6. Open GitHub → **Actions** → **Deploy Telegram-Stremio to Heroku Container** → **Run workflow**. Choose the branch containing these changes and run it.
7. The workflow sets the app's stack to `container`, builds the Docker image on GitHub's runner, pushes it to Heroku Container Registry and releases it. Your Heroku API key remains a GitHub Actions secret; never add it to a source file.
8. Open that workflow run and confirm every step succeeded, including **Set app stack to container**, **Build and push web image**, and **Release web image**.

The one-time stack change switches the app's deployment type from buildpacks to Docker containers. This is required for the Dockerfile and Jellyfin Web files to be used. The workflow does this for you; you do not need to install Docker or the Heroku CLI locally.

### Alternative: deploy using Heroku CLI and `heroku.yml`

If you prefer deploying from a local Git checkout instead of GitHub Actions, use Heroku's documented `heroku.yml` path after setting the app stack to `container`:

```bash
heroku login
heroku stack:set container -a codecindex-bf9c88aafb49
git remote add heroku https://git.heroku.com/codecindex-bf9c88aafb49.git
git push heroku main
```

If your local branch is not named `main`, replace `main` with the branch you are deploying. Do not use both deployment methods at the same time.

## 4. Confirm that Heroku is ready before opening Jellyfin

Open each address in Chrome on your phone. Use your actual app host; for this project it is:

```text
https://codecindex-bf9c88aafb49.herokuapp.com
```

### Check A — server identity

Open:

```text
https://codecindex-bf9c88aafb49.herokuapp.com/system/info/public
```

The JSON must contain:

```json
{
  "ServerName": "Telegram-Stremio",
  "ProductName": "Jellyfin Server",
  "Version": "10.10.7",
  "StartupWizardCompleted": true
}
```

This only proves that the app identifies itself correctly. It does not prove that the web client, MongoDB, Telegram client or video playback is ready.

### Check B — actual readiness

Open:

```text
https://codecindex-bf9c88aafb49.herokuapp.com/Jellyfin/Compat/Health
```

This page does not reveal passwords or tokens. Check these fields:

| Field | Expected value |
|---|---|
| `Enabled` | `true` |
| `WebClientAssetsPresent` | `true` |
| `ContentSourceTokenConfigured` | `true` |
| `ContentSourceTokenValid` | `true` |
| `TrackingDatabaseReachable` | `true` |
| `MediaStorageDatabasesReachable` | `true` |
| `TelegramStreamClientConnected` | `true` |
| `ReadyForAndroidClient` | `true` |

Do not proceed to the Android test if `ReadyForAndroidClient` is false. Send the health JSON to the person helping you, but never send credentials or source tokens. A false field tells us which dependency needs attention.

### Check C — Jellyfin web files

Open:

```text
https://codecindex-bf9c88aafb49.herokuapp.com/web/
```

You should see the Jellyfin web interface or its initial loading page, not a 404 or an application error.

## 5. Connect Jellyfin Android

1. Open the Jellyfin Android app.
2. Add a server.
3. In the **Host** field, enter only:

   ```text
   https://codecindex-bf9c88aafb49.herokuapp.com
   ```

4. Do not append `/web`, `/system/info/public`, `/dl/...`, a token, `:8096` or `:8920`.
5. Tap Connect.
6. Sign in with the separate `JELLYFIN_USERNAME` and `JELLYFIN_PASSWORD` you configured in Heroku.
7. You do not paste an API token manually. The server issues a Jellyfin session token during login and the app stores it. Individual video and subtitle URLs use separate short-lived tickets; the long-lived session token is not put in those URLs.

Heroku serves the public app over HTTPS on the normal HTTPS port. You do not need to add Jellyfin's usual direct-server ports to the URL.

## 6. Test the library and playback

Test in this order and tell us the first step that fails:

1. The Android app accepts the server and displays the login screen.
2. Login succeeds.
3. The Movies and TV Shows libraries appear.
4. Open a movie and verify its title, year and artwork.
5. Open a TV show → season → episode.
6. Start a movie. Let it play for a short time.
7. Seek forward, then backward.
8. Pause, resume and stop. Reopen the item and check resume position / watched state.
9. If the title has indexed Telegram-Stremio subtitles, open player subtitles and select one.
10. Try one episode after the movie test.

Start with a modest-size file and a format your Android device can already play. This project does not transcode video. It forwards the original Telegram file to the Jellyfin player, so container/codec compatibility still depends on the Android playback engine.

## 7. What is supported, and what is intentionally not included

| Feature | Current design |
|---|---|
| Connect from Jellyfin Android | Targeted; must pass the real-device test |
| Separate Jellyfin username/password | One configured account |
| Session token per device | Yes |
| Movies | Yes |
| TV Shows → Seasons → Episodes | Yes |
| Artwork and TMDB metadata | Yes, when present in source metadata |
| Search and common library filters | Yes, supported subset |
| Favorites, watched state and resume position | Stored per Jellyfin account |
| Existing external subtitles | SRT, VTT, ASS, SSA and SUB when indexed in Telegram-Stremio |
| Telegram direct playback | Yes, when a valid source token and Telegram stream client are available; verify on device |
| Seeking / HTTP byte ranges | Forwarded to the existing Telegram streamer; verify on device |
| Transcoding | No |
| Quick Connect | Disabled; use username/password |
| Multiple independent Jellyfin users | Not in this version |
| Live TV, music-only libraries, plugins, server administration, server-side media scanning | Not included |

This should not be marketed as a full Jellyfin Server replacement. It is a focused compatibility layer for this Android client and this Telegram-Stremio catalogue.

## 8. If something fails, what to send

Send:

- The first failing step from section 6.
- A screenshot of the complete error message.
- The Android app version.
- The JSON from `/Jellyfin/Compat/Health`.
- The matching Heroku log lines from the same minute (hide sensitive values).

Never send `JELLYFIN_PASSWORD`, `JELLYFIN_SOURCE_TOKEN`, any Telegram bot token, MongoDB URI, Heroku API key, or a Jellyfin session token. If a screenshot/log shows one, hide it first.

## 9. What these local tests mean

The repository includes automated contract tests with fake MongoDB records and a fake Telegram stream response. They check API response shapes and application logic. The tests do **not** simulate a real Telegram download, real Heroku container boot, or the actual Jellyfin Android player. The GitHub workflow also builds the Docker image so missing Web files or Docker build failures are caught before deployment. The final proof is still the real-device test described above.
