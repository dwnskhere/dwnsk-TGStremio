# Jellyfin Web client asset notice

The Docker build uses the official `jellyfin/jellyfin:10.10.7` image as a
**build-only asset source** and copies `/jellyfin/jellyfin-web/` into
`Backend/fastapi/jellyfin_web/` in the Telegram-Stremio image.

No Jellyfin server executable, service, database or media server process is
started by Telegram-Stremio. Only the static Web client files are copied so
the official Jellyfin Android app's embedded WebView can load `/web/` from the
same HTTPS origin as the compatibility API.

Jellyfin Web is an upstream project. Its license and notices are retained in
the copied distribution. Upstream project: https://github.com/jellyfin/jellyfin-web

The Web client version is intentionally pinned to 10.10.7 to match the
compatibility API version exposed by this integration. Change both versions
together only after running the Android compatibility test suite.
