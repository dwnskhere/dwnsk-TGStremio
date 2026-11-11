# Use the Jellyfin Android app with Telegram-Stremio

This guide is for the experimental Jellyfin-compatible API added to Telegram-Stremio.

## What this change is trying to do

You already have Telegram-Stremio running on Heroku. This change makes that same Heroku address answer a small set of Jellyfin-style requests, so the Jellyfin Android app can try to use it as its server.

**You do not install Jellyfin Server on your phone. You do not need a VPS.** Your phone runs the Jellyfin app, while Heroku runs Telegram-Stremio and the new compatibility routes.

This is an experimental compatibility layer, not the official Jellyfin Server. The Android app may request additional endpoints or expect response details that still need to be added after real-device testing.

## What you need

- Your Telegram-Stremio app already deployed and working on Heroku.
- Its existing MongoDB databases connected and populated with movies/episodes.
- At least one working Telegram-Stremio API token. The playback bridge uses the configured STRM default token, or the existing active-token fallback.
- The Jellyfin Android app installed on your phone.

## Step 1 — Add four Heroku Config Vars

Open **Heroku Dashboard → your Telegram-Stremio app → Settings → Config Vars**.

Add these variables:

| Key | Value |
|---|---|
| `JELLYFIN_COMPAT_ENABLED` | `true` |
| `JELLYFIN_USERNAME` | A new username you choose, for example `dhanraj-jf` |
| `JELLYFIN_PASSWORD` | A new, strong password that you have not used elsewhere |
| `JELLYFIN_SERVER_NAME` | `Telegram-Stremio` |

Optional:

| Key | Value | Purpose |
|---|---|---|
| `JELLYFIN_TOKEN_TTL_DAYS` | `30` | How long a Jellyfin app login remains valid before signing in again |
| `JELLYFIN_SERVER_ID` | Leave blank | The application generates a stable ID automatically |

Do not use your Telegram password, Heroku password, or existing Stremio API token as `JELLYFIN_PASSWORD`.

## Step 2 — Deploy the updated repository

The code changes must be committed to the Git repository connected to Heroku, then deployed using your normal deployment method. Restart the Heroku app after adding Config Vars.

When the app starts, the log may show a Jellyfin compatibility index message. That is expected. The compatibility collections are created in the existing tracking MongoDB database; video files are not copied to Heroku.

## Step 3 — Check that Heroku answers the Jellyfin server check

Open this address in your phone browser, replacing the example host with your actual Heroku app URL:

```text
https://YOUR-APP-NAME.herokuapp.com/system/info/public
```

You should see JSON text containing fields similar to:

```json
{
  "ServerName": "Telegram-Stremio",
  "ProductName": "Jellyfin Server",
  "Id": "a stable server id",
  "StartupWizardCompleted": true
}
```

This is the first compatibility check. The response must be JSON and `ProductName` must read `Jellyfin Server`. The Android app may also request the PascalCase variant `/System/Info/Public`; both path forms are supported.

If you get `404`, check that `JELLYFIN_COMPAT_ENABLED` is exactly `true` and both Jellyfin username/password Config Vars are present. If you get `Application Error`, check the Heroku logs and deployment.

## Step 4 — Add the server in Jellyfin Android

1. Open the Jellyfin Android app.
2. Choose **Add Server** (or the equivalent add-server option in your app version).
3. Enter only your Heroku app's base URL, for example:

   ```text
   https://YOUR-APP-NAME.herokuapp.com
   ```

   Do not add `/stremio`, `/manifest.json`, or `/System/Info/Public` to the URL.
4. Continue to the login screen.
5. Enter the `JELLYFIN_USERNAME` and `JELLYFIN_PASSWORD` values you added in Heroku.
6. Sign in.

The compatibility API should return a separate server-issued access token. The Jellyfin app should store it and reuse it on later requests. You should not have to paste that token manually.

## Step 5 — Check your library

The initial library views are:

- **Movies** — movie items with at least one available Telegram quality.
- **TV Shows** — shows with at least one episode that has an available Telegram quality.

Open a movie or series and check its title and artwork. Open a series, then a season, then an episode.

The catalogue is cached briefly (about 45 seconds), so newly added items may not appear immediately.

## Step 6 — Test playback

Start with one small or medium-sized movie file. Then test:

1. Start playback.
2. Seek forward by about one minute.
3. Seek backward.
4. Pause and resume.
5. Stop playback and try another episode.

The bridge reuses Telegram-Stremio's current streaming implementation. It is intended to preserve byte-range requests and split-file handling. It does not transcode video. Whether a file plays depends on the Jellyfin Android player supporting that file's container and codecs.

## What to send back if something fails

Please send:

1. The exact screen/error message from Jellyfin Android.
2. Whether the lowercase `/system/info/public` page displays JSON with `ProductName` set to `Jellyfin Server`.
3. The Android app version (Jellyfin → Settings → About, or the app info page).
4. The matching Heroku log lines from the time you tried.

Do not send your Jellyfin password, access token, Telegram API token, bot token, or MongoDB URI. Hide those values in screenshots and logs.

## Current limits of this first implementation

- One Jellyfin-compatible login is configured through Heroku Config Vars.
- No Quick Connect pairing screen yet.
- No transcoding; direct play/direct stream only.
- No admin dashboard for managing Jellyfin users yet.
- Playback uses the Telegram-Stremio default/fallback API token, so its subscription and data limits still matter.
- Android client compatibility is not confirmed until you test the deployed build.
