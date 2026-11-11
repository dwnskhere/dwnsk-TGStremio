# Telegram-Stremio → Jellyfin Android Compatibility Layer

**Status:** Proposed architecture and implementation plan\
**Target:** Existing Telegram-Stremio Heroku deployment + Jellyfin
Android client\
**Deployment constraint:** No separately hosted Jellyfin server, VPS,
NAS, or local server\
**Authentication choice:** Separate Jellyfin-compatible identity and
server-issued access tokens

------------------------------------------------------------------------

## 1. Executive summary

The goal is to make the existing Telegram-Stremio Heroku application
expose enough of the Jellyfin Server API for the **Jellyfin Android
app** to connect, browse the Telegram-Stremio catalogue, and play media
through the existing Telegram streaming pipeline.

The proposed design adds a **Jellyfin API compatibility layer** to the
existing FastAPI application. Heroku remains the only application host.
MongoDB remains the persistent data store. Telegram remains the media
source.

This is **not** an official Jellyfin server installation and must not be
described as one. It is a compatibility implementation that will need to
be tested against the actual Jellyfin Android client. Jellyfin clients
may depend on API behavior beyond the endpoints listed in this plan.

### Intended user experience

1.  Install the Jellyfin Android app.
2.  Add the existing Telegram-Stremio Heroku URL as the server address.
3.  Authenticate using a separate Jellyfin-compatible account managed by
    Telegram-Stremio.
4.  The backend issues a Jellyfin-compatible access token.
5.  Browse Movies and TV Shows in the Jellyfin interface.
6.  Play an item; the backend resolves it to the existing Telegram
    stream and serves byte ranges.

**No second Jellyfin deployment is part of this design.**

------------------------------------------------------------------------

## 2. Current repository baseline

The supplied repository snapshot already contains useful components:

  ---------------------------------------------------------------------------
  Existing component          Current role            Planned reuse
  --------------------------- ----------------------- -----------------------
  FastAPI application         Main HTTP application   Register the Jellyfin
                                                      compatibility router

  MongoDB database layer      Tracking and media      Store Jellyfin users,
                              storage databases       device sessions, tokens
                                                      and playback state

  Existing API-token          Validates               Reuse the underlying
  validation                  Telegram-Stremio stream media authorization
                              tokens                  checks; do not reuse a
                                                      Stremio token as the
                                                      Jellyfin session token

  `/dl/{token}/{id}/{name}`   Existing Telegram media Use internally as the
                              stream route            media source / adapt
                                                      shared streaming
                                                      service

  Range parsing and stream    Supports HTTP           Reuse or extract into a
  response helpers            byte-range streaming    shared streaming
                                                      service

  TMDB metadata helpers and   Movie, series, season   Map existing metadata
  media schemas               and episode metadata    to Jellyfin item
                                                      responses

  `.strm` / `.nfo` exporter   File-based              Keep as a separate
                              Emby/Jellyfin           existing feature; it is
                              integration             not the solution for a
                                                      serverless Jellyfin
                                                      Android client
  ---------------------------------------------------------------------------

### Important current limitation

The repository currently documents a **file export integration** using
`.strm` and `.nfo` files. That workflow expects a Jellyfin/Emby server
with access to the exported media directory.

The proposed API compatibility layer is a new feature. The current
repository does not already provide a complete Jellyfin Server API.

------------------------------------------------------------------------

## 3. Target architecture

``` text
                 Jellyfin Android app
                          |
                          | HTTPS
                          | Server URL = Telegram-Stremio Heroku URL
                          v
+-----------------------------------------------------------+
|             Existing Telegram-Stremio on Heroku           |
|                                                           |
|  +-------------------------+                              |
|  | Jellyfin API layer      |                              |
|  |                         |                              |
|  | - Server discovery      |                              |
|  | - Authentication        |                              |
|  | - User/session tokens   |                              |
|  | - Library views         |                              |
|  | - Item/metadata APIs    |                              |
|  | - Playback information |                              |
|  | - Playback reporting    |                              |
|  +------------+------------+                              |
|               |                                           |
|       +-------+--------------------+                      |
|       |                            |                      |
|       v                            v                      |
|  Existing MongoDB             Existing media services    |
|  - media catalogue            - Telegram file resolution |
|  - Jellyfin identities        - range-based streaming    |
|  - device sessions            - subtitle resolution      |
|  - playback state                                        |
|       |                            |                      |
+-------|----------------------------|----------------------+
        |                            |
        v                            v
 Persistent metadata          Telegram media source
 and auth records              (existing bot/session setup)
```

### Design principles

-   Do not run a second media server process.
-   Do not store video files on Heroku's ephemeral filesystem.
-   Do not copy media from Telegram to Heroku.
-   Do not expose Telegram-Stremio admin credentials or raw Stremio API
    tokens to Jellyfin clients.
-   Reuse existing metadata and streaming logic instead of creating a
    second media catalogue.
-   Keep the Jellyfin API layer isolated from existing Stremio routes.
-   Treat the Jellyfin Android app as an untrusted client; validate
    every token and item request server-side.

------------------------------------------------------------------------

## 4. Authentication design --- separate identity, server-issued token

The selected option is **B: independent Jellyfin-compatible
authentication**, with token-based sessions.

A key implementation detail: the Jellyfin Android app generally does not
provide a generic field where a user pastes an arbitrary custom API
token. The compatibility layer must implement the authentication
exchange expected by the client. The backend then returns an access
token, which the client stores and sends on later requests.

### Recommended first implementation: username/password exchange → access token

Create a separate Jellyfin identity in Telegram-Stremio. This identity
is independent of the existing Stremio API token.

Example account record:

``` json
{
  "_id": "jellyfin_user_01",
  "username": "dhanraj",
  "password_hash": "<argon2-or-bcrypt-hash>",
  "is_disabled": false,
  "created_at": "2026-11-11T10:00:00Z"
}
```

Example successful authentication response shape (illustrative; verify
exact fields against the targeted Jellyfin client/API version):

``` json
{
  "User": {
    "Id": "jellyfin_user_01",
    "Name": "dhanraj",
    "HasPassword": true,
    "HasConfiguredPassword": true
  },
  "AccessToken": "<server-issued-random-token>",
  "ServerId": "<stable-server-id>"
}
```

The token should be generated using a cryptographically secure random
generator. Store only a hash of the token where practical, together with
its user, device/client identity, creation time, last-used time,
expiry/revocation state and permission scope.

### Example request flow

``` mermaid
sequenceDiagram
    participant App as Jellyfin Android
    participant API as Telegram-Stremio on Heroku
    participant DB as MongoDB

    App->>API: POST /Users/AuthenticateByName
    Note over App,API: Separate Jellyfin username/password
    API->>DB: Find Jellyfin user and verify password hash
    DB-->>API: User record
    API->>DB: Create device session and token hash
    DB-->>API: Session saved
    API-->>App: User + AccessToken + ServerId
    App->>API: GET /Users/Me
    Note over App,API: Token sent in Authorization header
    API->>DB: Validate token and session
    DB-->>API: Valid user/session
    API-->>App: User profile
```

### Token rules

-   Jellyfin session tokens and Telegram-Stremio streaming tokens are
    separate credentials.
-   Never place a long-lived secret in a public catalogue URL.
-   Support token revocation per device.
-   Use HTTPS only for authentication and playback.
-   Apply rate limits to authentication and pairing endpoints.
-   Avoid logging passwords, authorization headers, raw tokens or
    Telegram file identifiers.
-   Keep an expiry / rotation policy configurable.
-   Do not silently grant admin access to Jellyfin-compatible users.

### Optional second step: Quick Connect

Quick Connect can be investigated after basic authentication works. It
requires the expected initiation, polling and authorization behavior and
must be tested with the exact Android app version.

Do not assume that a custom six-digit pairing screen will work merely
because it looks similar. The client must support the relevant Quick
Connect protocol.

------------------------------------------------------------------------

## 5. API compatibility roadmap

The endpoint list below is a **development target**, not a claim that
every route is sufficient for every Jellyfin Android release. Capture
the actual Android client's requests and implement the smallest
compatible subset first.

  ------------------------------------------------------------------------------------
  Priority                API area                             Expected purpose
  ----------------------- ------------------------------------ -----------------------
  P0                      Server/system information            Identify the server and
                                                               provide compatible
                                                               capabilities/version
                                                               information

  P0                      `POST /Users/AuthenticateByName`     Authenticate the
                                                               separate
                                                               Jellyfin-compatible
                                                               account and issue a
                                                               session token

  P0                      `GET /Users/Me`                      Return the
                                                               authenticated user

  P0                      `GET /Users/{userId}/Views`          Return Movies and TV
                                                               Shows library views

  P0                      `GET /Items`                         Browse and search
                                                               catalogue items

  P0                      `GET /Items/{itemId}`                Return item details

  P0                      `GET /Items/{itemId}/PlaybackInfo`   Return playable media
                                                               source information

  P0                      `GET /Videos/{itemId}/stream` or     Stream the selected
                          compatible media route               Telegram media

  P1                      `POST /Sessions/Playing`             Record playback started

  P1                      `POST /Sessions/Playing/Progress`    Record playback
                                                               position

  P1                      `POST /Sessions/Playing/Stopped`     Record playback stopped

  P1                      `GET /Shows/{seriesId}/Seasons`      List seasons

  P1                      `GET /Shows/{seriesId}/Episodes`     List episodes

  P1                      Image routes                         Posters, backdrops and
                                                               thumbnails

  P2                      Subtitle routes                      Expose available
                                                               subtitle tracks

  P2                      Quick Connect routes                 Pair a client without
                                                               typing a password

  P2                      User configuration / favorites       Only if requested and
                                                               supported by the client
  ------------------------------------------------------------------------------------

The implementation should use a captured request/response fixture set
from the target Jellyfin Android app rather than guessing response
fields.

------------------------------------------------------------------------

## 6. Library and metadata mapping

The Jellyfin API layer should map the existing Telegram-Stremio
catalogue to Jellyfin item types.

  Telegram-Stremio data   Jellyfin representation
  ----------------------- ---------------------------
  Movie record            `Movie`
  TV show record          `Series`
  Season record           `Season`
  Episode record          `Episode`
  TMDB ID                 Provider ID / external ID
  Poster URL              Primary image
  Backdrop URL            Backdrop image
  Runtime                 Run time ticks
  Release year            Production year
  Overview                Overview
  Episode number          Index number
  Season number           Parent index number

### Example library response (simplified illustration)

``` json
{
  "Items": [
    {
      "Id": "movie_12345",
      "Name": "Example Movie",
      "Type": "Movie",
      "ProductionYear": 2025,
      "Overview": "Example overview.",
      "ProviderIds": {
        "Tmdb": "12345"
      }
    },
    {
      "Id": "series_67890",
      "Name": "Example Series",
      "Type": "Series",
      "ProductionYear": 2024,
      "ProviderIds": {
        "Tmdb": "67890"
      }
    }
  ],
  "TotalRecordCount": 2,
  "StartIndex": 0
}
```

The IDs shown here are examples only. Production IDs must be stable
across restarts and catalogue refreshes. Prefer deterministic IDs
derived from the media type and stable database/TMDB identity, while
handling duplicate editions and multiple qualities deliberately.

### Quality selection

For the MVP, choose one playable Telegram quality per Jellyfin item
using a deterministic rule, such as:

1.  Respect a user-configured preferred quality if available.
2.  Otherwise choose the highest available quality within the supported
    set.
3.  Keep alternate qualities for a later version.

Do not create duplicate movie or episode entries for every quality in
the first release.

------------------------------------------------------------------------

## 7. Playback flow

Jellyfin asks the compatibility API for playback information. The API
resolves the selected catalogue item to the Telegram message/file
already known by Telegram-Stremio.

``` mermaid
sequenceDiagram
    participant App as Jellyfin Android
    participant API as Heroku Jellyfin API
    participant DB as MongoDB
    participant TG as Telegram streaming layer

    App->>API: GET /Items/{id}/PlaybackInfo
    API->>DB: Resolve item and selected quality
    DB-->>API: Telegram stream reference
    API-->>App: MediaSources + compatible stream URL
    App->>API: GET stream URL (Range: bytes=0-)
    API->>DB: Validate Jellyfin session and media permission
    API->>TG: Read requested Telegram byte range
    TG-->>API: Requested bytes
    API-->>App: 206 Partial Content + Content-Range
    App->>API: GET next byte range
    API-->>App: Next media bytes
```

### Example playback response (illustrative)

``` json
{
  "MediaSources": [
    {
      "Id": "source_movie_12345",
      "Name": "Example Movie 2025",
      "Container": "mkv",
      "SupportsDirectPlay": true,
      "SupportsDirectStream": true,
      "SupportsTranscoding": false
    }
  ]
}
```

The exact `MediaSources` fields and URL conventions must be adjusted to
what the target Android client expects.

### Streaming requirements

-   Preserve HTTP `Range` requests.
-   Return `206 Partial Content` for valid partial requests.
-   Return correct `Content-Length`, `Content-Range`, `Accept-Ranges`
    and MIME type.
-   Return `416 Range Not Satisfiable` for invalid ranges.
-   Support `HEAD` if the client probes media before playback.
-   Handle client disconnects and Telegram download cancellation
    cleanly.
-   Avoid buffering entire videos in memory.
-   Reuse the existing streaming implementation where safe; refactor
    shared logic rather than making a second independent Telegram
    downloader.
-   Ensure the Jellyfin session token is validated on the playback
    route. If a short-lived media ticket is required for player
    compatibility, make it scoped, expiring and revocable.

### Transcoding boundary

The MVP should advertise **direct play/direct stream only when the
source is compatible**. Do not claim transcoding support unless an
actual transcoding pipeline exists. Heroku resource constraints make
server-side video transcoding a poor initial target.

------------------------------------------------------------------------

## 8. MongoDB data model

Use collections in the existing MongoDB deployment. Do not introduce
another database service for the MVP.

### `jellyfin_users`

``` json
{
  "_id": "jellyfin_user_01",
  "username_normalized": "dhanraj",
  "password_hash": "<password-hash>",
  "is_disabled": false,
  "created_at": "2026-11-11T10:00:00Z"
}
```

### `jellyfin_sessions`

``` json
{
  "_id": "session_01",
  "user_id": "jellyfin_user_01",
  "token_hash": "<hash-of-random-token>",
  "device_id": "android-device-id",
  "client": "Jellyfin Android",
  "created_at": "2026-11-11T10:00:00Z",
  "last_seen_at": "2026-11-11T10:05:00Z",
  "expires_at": "2026-12-11T10:00:00Z",
  "revoked_at": null
}
```

### `jellyfin_playback_state`

``` json
{
  "_id": "jellyfin_user_01:movie_12345",
  "user_id": "jellyfin_user_01",
  "item_id": "movie_12345",
  "position_ticks": 1200000000,
  "played": false,
  "updated_at": "2026-11-11T10:05:00Z"
}
```

Create unique indexes for normalized usernames and token hashes, plus
indexes for user/device sessions and playback state lookups. Add TTL
indexes only where expiry semantics are safe; revocation/audit records
may need to remain available.

------------------------------------------------------------------------

## 9. Heroku deployment considerations

The compatibility layer runs inside the existing FastAPI web process.

### Configuration

Proposed environment variables:

``` env
JELLYFIN_COMPAT_ENABLED=true
JELLYFIN_SERVER_ID=<stable-random-server-id>
JELLYFIN_TOKEN_TTL_DAYS=30
JELLYFIN_QUICK_CONNECT_ENABLED=false
```

The existing MongoDB configuration should be reused. Do not duplicate
database URLs unless a clear isolation requirement appears.

### Heroku constraints

-   Heroku filesystem is ephemeral: no persistent media library
    directory.
-   Keep catalogue and session state in MongoDB.
-   Stream media progressively; do not load whole files into RAM.
-   Watch dyno memory, response duration, concurrent streams and
    Telegram client limits.
-   Verify that the existing Heroku plan supports the expected
    long-lived streaming behavior.
-   Use HTTPS and a stable public base URL.
-   Do not add a background worker unless a concrete feature requires
    one.

------------------------------------------------------------------------

## 10. Phased implementation plan

### Phase 0 --- Compatibility spike and request capture

**Goal:** Prove that the installed Jellyfin Android app can connect to a
lightweight compatible API.

Tasks: - Record the exact Jellyfin Android app version. - Capture
requests made during server URL entry and login. - Identify required
server discovery, system info and authentication endpoints. - Build a
tiny test router returning only the required responses. - Confirm the
app reaches an authenticated state.

**Exit criteria:** Jellyfin Android can add the Heroku URL and
authenticate. No media catalogue work begins until this passes.

### Phase 1 --- Authentication and token lifecycle

Tasks: - Add `jellyfin_users` and `jellyfin_sessions`. - Add a secure
account-creation/admin workflow. - Implement password verification and
compatible authentication response. - Issue random session tokens; store
token hashes. - Validate token headers on protected routes. - Add
logout/revocation and per-device session management. - Add rate limits
and security tests.

**Exit criteria:** Login, app restart, token reuse, logout and
revocation work as expected.

### Phase 2 --- Read-only library browsing

Tasks: - Add stable Jellyfin item IDs. - Map Movies and Series. - Add
Seasons and Episodes. - Implement library views, pagination, sorting and
item details. - Map TMDB metadata and image endpoints. - Keep the
library read-only.

**Exit criteria:** Jellyfin Android can browse the actual
Telegram-Stremio catalogue without duplicate or unstable entries.

### Phase 3 --- Playback

Tasks: - Implement playback information. - Resolve a Jellyfin item to an
existing Telegram file/quality. - Add a Jellyfin-authenticated playback
route. - Reuse existing range-based Telegram streaming. - Test seeking,
pause/resume, app backgrounding, network interruption and large files. -
Verify that the Android player receives correct MIME/container
information.

**Exit criteria:** A movie and an episode play reliably and seeking
works without downloading the full file first.

### Phase 4 --- Playback state and quality behavior

Tasks: - Record started/progress/stopped events. - Store per-user resume
position and watched state. - Add deterministic quality selection. -
Test multiple devices and concurrent sessions.

**Exit criteria:** Resume and watched state behave consistently for the
same user.

### Phase 5 --- Optional enhancements

Only after the MVP is stable: - Quick Connect. - Subtitle discovery and
delivery. - Favorites. - User-specific library visibility. - Multiple
quality selection. - Better image caching and client compatibility.

------------------------------------------------------------------------

## 11. Test matrix

  -----------------------------------------------------------------------
  Test                                Expected result
  ----------------------------------- -----------------------------------
  Add Heroku URL in Jellyfin Android  Server is recognized

  Login with separate                 Access token is issued
  Jellyfin-compatible account         

  Restart Android app                 Existing session remains valid

  Revoke device session               That device is denied on next
                                      request

  Browse Movies                       Correct movie list and metadata

  Browse TV Shows                     Series, seasons and episodes are
                                      correctly nested

  Open poster/backdrop                Correct image is returned

  Start playback                      Correct Telegram media is resolved

  Seek forward/backward               Byte-range requests work

  Pause and resume                    Playback continues correctly

  Invalid token                       Protected API returns unauthorized

  Expired/revoked media ticket        Playback is denied

  Two devices                         Sessions and progress remain
                                      isolated

  Heroku dyno restart                 Users, tokens and catalogue persist
                                      in MongoDB
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 12. Security and operational safeguards

-   Use password hashing (Argon2id preferred; bcrypt acceptable if
    already standardized in the project).
-   Generate access tokens with a cryptographically secure random
    source.
-   Store token hashes rather than raw tokens.
-   Validate user ownership and permissions for every item and playback
    request.
-   Keep admin and Jellyfin-compatible user roles separate.
-   Rate-limit authentication, pairing and expensive catalogue queries.
-   Do not log credentials, tokens, authorization headers or sensitive
    Telegram references.
-   Restrict CORS where browser-based flows require it; native app API
    calls do not justify unrestricted CORS by default.
-   Apply request timeouts and bounded concurrency to protect Heroku
    memory and Telegram clients.
-   Do not advertise unsupported transcoding, trick-play, live TV or
    other Jellyfin features.

------------------------------------------------------------------------

## 13. Risks and honest limitations

1.  **This is not the official Jellyfin server.** It is a partial API
    implementation.
2.  **Client compatibility is the largest unknown.** Jellyfin Android
    may call endpoints or expect fields not listed here. Phase 0 is
    mandatory.
3.  **Token-only manual login may not be available in the Android app.**
    The server must implement the client's supported authentication
    exchange and issue the token as part of that exchange.
4.  **Quick Connect is not assumed for the MVP.** Add it only after
    confirming the client supports the required protocol.
5.  **Playback compatibility varies by container and codec.** Direct
    play should be tested with representative Telegram files.
6.  **No transcoding is planned.** If a source cannot be played by the
    Android client, the MVP should report it as unsupported rather than
    pretending to transcode.
7.  **Heroku is not a general-purpose media server host.** Long streams,
    memory, concurrency and dyno lifecycle must be load-tested.

------------------------------------------------------------------------

## 14. Definition of done for the MVP

The MVP is complete only when all of the following are demonstrated on a
real Android device:

-   The Jellyfin Android app connects to the existing Heroku URL.
-   A separate Telegram-Stremio-managed Jellyfin-compatible account can
    authenticate.
-   The app receives and retains a server-issued access token.
-   Movies and TV Shows appear in the app with correct metadata.
-   At least one movie and one episode stream from Telegram.
-   Seeking works using HTTP byte ranges.
-   Token revocation blocks further protected access.
-   No separate Jellyfin server, VPS, NAS or persistent Heroku media
    directory is required.

**Recommended order:** Phase 0 → Phase 1 → Phase 2 → Phase 3. Do not
implement the full API surface before the Android client proves the
initial handshake works.
