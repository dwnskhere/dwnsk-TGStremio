# Public Library and Plans — V1

## Scope

This release adds a public, read-only Library and Plans experience using the existing CodecIndex editorial design. The root route (`/`) is the public Library (selected homepage behaviour C). The authenticated admin home is available at `/admin`; `/admin/dashboard` remains available. Login redirects administrators to `/admin`.

## Public routes

- `/` and `/library`: public media catalogue.
- `/library/movie/{tmdb_id}`: safe movie availability details.
- `/library/tv/{tmdb_id}`: safe TV seasons and episode availability details.
- `/plans`: public subscription plans.
- `/api/public/library`: paginated, searchable public catalogue API.
- `/api/public/library/{movie|tv}/{tmdb_id}`: public detail API.
- `/api/public/plans`: sanitized current plan records.

## Visibility and privacy

Public media queries include only records whose `visibility` is `public` or is absent for legacy records. Token-only and owner-only entries are not included. Existing exclusive-catalog visibility rules are preserved.

Public responses are explicit projections. They do not return original filenames, Telegram `chat_id`/`msg_id`, Telegram file IDs, Mongo `_id`, storage database indices, private visibility fields, access tokens, subscriber records, or raw internal stream objects. Detail lookups resolve the highest-priority (newest) storage record before checking visibility, so a restricted newer record cannot fall through to an older public duplicate. File rows expose parsed technical facts only: quality/resolution, source, codec, video/HDR tags, audio, language, container, size and aggregate part count. The public API does not provide Stremio/Nuvio deep links or direct playback links.

Public poster/backdrop URLs are resolved through the existing cover helper. The detail pages show only active (non-dead) streams.

## Library behaviour

- Search supports title, original title, English title, IMDb ID (`tt...`), TMDb numeric ID, and a title followed by a year.
- Filters: All / Movies / Shows, Everything / 4K / New episodes. No language filters or public `Needs update` filter are included (the latter is omitted because its public meaning was not confirmed).
- `New episodes` means at least one episode was indexed within the last 14 days, using `Episode.indexed_on`.
- `indexed_on` is stamped only when an episode is first inserted; adding another quality to an existing episode does not refresh the date. Existing database episodes without this timestamp are not backfilled, so they will not be classified as new until newly added after this release.
- Public file counts are distinct quality labels per movie or episode. For TV shows, the displayed file count is the sum of distinct quality labels across active episodes.
- 4K filter recognises 2160p, 4K, UHD and 4320p labels or parsed file metadata.

## Plans behaviour

Plan duration, price and currency are loaded from `tracking.sub_plans` at request time. If a plan record contains a `name` or `title`, that value is used; current plan records do not define either field, so the existing 30/90/180/365-day labels map to Starter/Basic/Pro/Premium and other durations display as `{days}-Day Plan`. No FX conversion or discount is inferred.

Daily caps shown on the page are a manually maintained V1 mapping based on the operator-provided published plan copy: 30 days = 50 GB/day, 90 = 70 GB/day, 180 = 98 GB/day, 365 = 137 GB/day. These are not stored in `sub_plans` and are not dynamically inferred from user-specific API token limits. A future release should add explicit plan entitlements to the plan schema before making these limits fully data-driven. Monthly data is shown as Unlimited based on the operator-provided plan copy.

The plan CTA opens the configured purchase bot's normal plan picker; it does not preselect a plan or implement web checkout. Admins can configure the purchase bot username and support username/link in **Admin → Settings → Public Website**. Title detail pages are informational listings only: no Stremio/Nuvio app-opening buttons or deep links are exposed. If the purchase bot field is empty, the currently connected StreamBot username is used as a fallback. The Support navigation item is hidden unless a valid support contact is configured.

The public page intentionally omits trials, payment methods, detailed policy blocks, token/fair-use rules, VPN claims, auto-renew claims, FAQs, and plan-specific resolution/screen entitlements. Support is available only through the configured Support navigation link. The public plans currently show the manually mapped daily allowance from the published operator copy; they do not read each subscriber's per-token limits. Making plan entitlements fully dynamic is explicitly deferred to a later release.

## Admin routing

- `/` is public.
- `/admin` is the authenticated admin home.
- `/admin/dashboard` remains protected.
- Login redirects to `/admin`.
- The admin base navigation now points Home to `/admin`.

## Operational notes

- Public stats are cached in-process for 90 seconds. Catalogue API responses are cached by clients/CDNs for 20 seconds, detail for 30 seconds, and plans for 60 seconds. Catalogue pagination can request up to 500 pages of 60 records; per-store candidate reads are hard-bounded at 30,000 records to prevent unbounded public queries. This supports the current 11K+ title catalogue without the earlier 1,200-candidate ceiling.
- New `indexed_on` values are written for newly added TV episodes. This does not rewrite historical episodes.
- This release is read-only for visitors. It does not mutate media, subscriptions, or storage records through public routes.
- Live production DB and browser integration testing still needs to be performed in the deployment environment. The current build environment does not include the `motor` MongoDB driver, so importing the complete FastAPI application and testing against a live MongoDB instance was not possible here; Python compilation, source-level tests, and Jinja template parsing were run instead.
