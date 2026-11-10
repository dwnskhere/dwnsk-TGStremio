# Public Website V2 — Library, Plans, Admin Plan Editor, and Cache

## 1. Purpose

V2 refines the Public Library & Plans V1 around the operator's requested visitor experience:

- The public Plans page is the homepage (`/`).
- The public Library is available at `/library`.
- Movie and TV detail pages are informational catalogue pages only.
- Plan fields are editable by administrators and stored in the tracking database.
- The Library read model is cached in server RAM for up to 24 hours, with immediate invalidation after application-managed media writes.
- Public pages avoid exposing internal stream/file details or subscriber/security data.
- The design retains the dark editorial/cinematic style from the supplied references.

This release does not attempt to redesign the admin dashboard or change Telegram/Stremio ingestion, playback, token authentication, or payment verification.

## 2. Route map

| Route | Access | Purpose |
|---|---|---|
| `/` | Public | Plans and service-information homepage |
| `/plans` | Public | Alias for the plans page |
| `/library` | Public | Searchable public movie/TV catalogue |
| `/library/movie/{tmdb_id}` | Public | Movie information page |
| `/library/tv/{tmdb_id}` | Public | TV show, season and episode guide |
| `/api/public/library` | Public, read-only | Paginated, filtered catalogue data |
| `/api/public/library/{movie\|tv}/{tmdb_id}` | Public, read-only | Sanitized movie/TV metadata |
| `/api/public/plans` | Public, read-only | Sanitized current plan fields |
| `/admin` | Authenticated | Existing admin home/dashboard |
| `/admin/dashboard` | Authenticated | Existing admin dashboard alias |
| `/admin/plans` | Authenticated | Subscription plan editor |
| `/admin/subscriptions` | Authenticated | Existing subscription management route |

Login/admin navigation remains separate from the public homepage. The old protected admin page is not made public by moving `/` to Plans.

## 3. Admin plan data model

Subscription plan records are owned by the **tracking database**, collection `sub_plans`. V2 stores:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Public plan name, e.g. Starter or HD Plus |
| `days` | integer | Subscription validity in days |
| `price` | number | Advertised plan price |
| `currency` | string | Three-letter currency code, e.g. INR |
| `daily_cap_gb` | number or `"Unlimited"` | Displayed daily usage allowance |
| `monthly_cap` | number or `"Unlimited"` | Displayed monthly usage allowance |
| `created_at` / `updated_at` | datetime | Internal administration timestamps |

### Admin form and API

`/admin/plans` supports adding and editing the plan name, validity, price, currency, daily cap and monthly cap. The protected API validates:

- Plan name is required and at most 80 characters.
- Validity is 1–3650 days.
- Price is non-negative and bounded.
- Currency is a three-letter uppercase code.
- Daily/monthly caps accept non-negative numbers (including zero) or `Unlimited`.

Blank/`None` monthly cap is normalized to `Unlimited`. A numeric zero remains zero and is not silently converted to `Unlimited`.

The public `/` Plans page and `/api/public/plans` read the same tracking collection. Public projections expose only name, validity, price, currency and the two cap values. Mongo `_id`, collection metadata, user/subscription details, tokens and other internal fields are excluded.

### Legacy plan backfill

Older plan documents that do not contain the newer fields receive an idempotent backfill on `get_subscription_plans()`:

- 30 days → Starter, 50 GB daily, Unlimited monthly.
- 90 days → Basic, 70 GB daily, Unlimited monthly.
- 180 days → Pro, 98 GB daily, Unlimited monthly.
- 365 days → Premium, 137 GB daily, Unlimited monthly.
- Other durations receive a `{days}-Day Plan` label; daily cap remains unspecified unless an administrator sets it; monthly defaults to Unlimited.

Each field is written only if it is absent, so a later administrator edit is not overwritten by the compatibility backfill. The values for legacy plans are based on the operator-provided CodecIndex plan/usage copy; they are not inferred from live subscriber tokens.

### Important: display configuration vs enforcement

V2 persists and displays the plan caps in `sub_plans`. This change does **not** automatically rewrite the separate token/subscriber quota records or change the quota-enforcement engine. If the operator changes `daily_cap_gb` or `monthly_cap` in `/admin/plans`, the public plan page reflects the new advertised value, but the actual runtime quota is still controlled by the existing subscriber/token configuration until a separate entitlement-sync/enforcement project is implemented and tested.

This separation is deliberate for this release to avoid silently changing active subscribers' limits.

## 4. Data ownership and database separation

- **Tracking DB:** subscription plans and existing tracking/subscriber state.
- **Storage DBs:** movie and TV media metadata, visibility and episode/stream availability.
- **Public cache:** in-process RAM only; no new MongoDB cache collection is created.

The public pages aggregate the configured `storage_N` databases in their current storage ordering. They do not assume the tracking DB contains media documents.

## 5. 24-hour public Library cache

### Current strategy

A bounded in-process LRU cache is used:

- TTL: 24 hours maximum.
- Maximum entries: 512.
- Uses a lock around cache mutation and deep copies values on read/write to prevent callers mutating cached state.
- Caches final, sanitized Library API pages, public detail documents and public collection statistics.
- Does not cache complete storage collections or write cached media into MongoDB.
- Plan reads remain live from the tracking DB, so plan edits are reflected immediately on the next request.

### Cache invalidation

Application code invalidates the public cache after relevant media changes, including media insert/update/delete, visibility changes and exclusivity changes. This makes the 24-hour TTL a maximum stale window rather than the normal wait after an application-managed write.

If an operator edits MongoDB directly, bypassing the application's database methods, no in-process invalidation event is emitted. Such an out-of-band change may remain cached for up to 24 hours (or until the app restarts / an admin-triggered cache clear is added to the route). Prefer using the application/admin workflows for changes that should become public immediately.

### Is RAM caching the best fit?

For the current deployment, yes. `Backend/fastapi/__init__.py` constructs one Uvicorn server without configuring multiple worker processes. The cache therefore lives in the same process that serves these routes and is not persisted to either database.

FastAPI's deployment documentation explains that separate worker processes normally do not share application memory; each worker has its own memory. If this service is later run with multiple Uvicorn workers, multiple containers, or multiple replicas, each instance would warm a separate cache and could serve different snapshots. At that point the preferred next step is **Redis cache-aside** with TTL and explicit invalidation on writes, not an additional MongoDB cache collection. See [FastAPI: Memory per process](https://fastapi.tiangolo.com/deployment/concepts/#memory-per-process) and [Redis: Cache-aside](https://redis.io/docs/latest/develop/use-cases/cache-aside/).

## 6. Public Library visitor experience

### Listing

- Cards show only poster, title and release year.
- No quality badges, stream/file counts, audio, codecs, file sizes, filenames or technical metadata appear on cards.
- Public filters are All / Movies / Shows and Everything / New episodes.
- Search supports title, year, IMDb ID and TMDb ID.
- No language filter or public `Needs update` filter is included.
- Sorting includes Recently added, A–Z and Top rated.
- Pagination is performed over the globally merged movie/TV result set to avoid repeating page 1 when a visitor loads more items.

### Back-to-library state

The Library saves query, media type, freshness filter, sort order, page and scroll position in the browser's `sessionStorage`. A detail page's Back to Library control uses browser history when the visitor came from the Library. The Library reconstructs the saved pages and restores the previous scroll position. If the visitor opened the detail page directly or in a new tab, the normal `/library` link is used.

### TV episode rows

Episode cards use a bounded thumbnail column and a separate min-width-zero text column. Text wraps/clamps inside the row, and the mobile breakpoint narrows the thumbnail without allowing the title/overview to overflow.

## 7. Public details and privacy boundary

Public movie/TV detail pages are informational only. They show approved catalogue metadata such as title, year, genre, runtime, overview, artwork, seasons, episode titles, air dates and episode overviews.

They do not expose:

- Original filenames or parsed filename text.
- Quality / resolution labels.
- File size or multipart count.
- Audio tracks, channels, languages, codec, source or HDR/video tags.
- Telegram chat/message IDs or Telegram file IDs.
- Mongo `_id`, storage database index or internal visibility flags.
- Add-on URLs, access tokens, direct playback URLs or Stremio/Nuvio deep links.
- Subscriber names/IDs, usage, payments, invoices or private admin data.

Only publicly visible records are eligible (`visibility=public`, or a legacy record with visibility absent), and exclusive-catalog records are omitted. The detail lookup resolves the newest record across storage DBs before applying visibility. That prevents an older public duplicate from being shown when the newest copy is private/restricted.

The listing API emits only title/year/poster/type and internal sorting values are stripped before returning the response.

## 8. Plans homepage and service content

The Plans experience is the public homepage. It reads plan values dynamically from `tracking.sub_plans`, while service feature copy and FAQ entries are static operator-authored content derived from the supplied CodecIndex description and operating notes.

The page includes:

- Dynamic plan cards (name, validity, price/currency, daily and monthly caps).
- Purchase CTA to the configured Telegram bot.
- Support CTA from the configured support contact.
- Service feature summary and a short activation flow.
- Interactive FAQ/Q&A.
- No fabricated reviews, star ratings, customer counts or testimonials.
- No web checkout/payment form, trial flow, or automatic plan pre-selection.

The purchase CTA opens the bot's regular start/purchase entry point. It does not preselect a particular plan. Support and bot destinations are configurable in Admin → Settings → Public Website (`public_support_contact`, `public_bot_username`). If no bot username is configured, the connected StreamBot username is used as a fallback.

## 9. Product copy and factuality

Features/FAQs are derived from the operator-provided CodecIndex copy. Compatibility, player support, payment handling, service limits, reset schedules and service availability are statements supplied by the operator and have not been independently audited in this code change. Keep them aligned with the actual service configuration and operational policy.

No fake reviews are displayed. No language filter is included. No public app-opening CTA is included.

## 10. TMDb attribution and commercial-use action

The public pages use TMDb-sourced metadata/artwork. TMDb's current official FAQ says API use is free for non-commercial purposes with attribution, defines a commercial project as one whose primary purpose is revenue for the owner, and says commercial API/data/image licensing should be discussed with TMDb sales. The FAQ also requires the approved TMDb logo and the notice: “This product uses the TMDB API but is not endorsed or certified by TMDB.”

The public footer currently includes the required wording and links to TMDb/its logo guidance, but **the official approved TMDb logo asset has not been embedded in this release**. Because this is a subscription service, do not assume the non-commercial developer API permission applies; contact TMDb sales before launch and add an approved logo to the site's Credits/About area as instructed. See [TMDb API FAQ](https://developer.themoviedb.org/docs/faq) and [TMDb logo & attribution guidance](https://www.themoviedb.org/about/logos-attribution).

## 11. Validation performed

- `pytest -q`: 38 passed, 6 subtests passed.
- `python -m compileall -q Backend`: passed.
- All 19 Jinja templates parsed successfully.
- Node syntax checks passed for the Library, detail page and Admin plan-editor scripts.
- Tests cover safe public projections, private visibility, cache invalidation, plan payloads, monthly-cap zero handling, public templates, episode layout, history/session state and route/page expectations.

### Not performed

- No live MongoDB connection or DB integration test was run.
- The supplied MongoDB connection strings contained credentials in chat. They were not copied into source/config files or used. Rotate the three exposed database-user passwords in MongoDB Atlas and replace the values in your deployment secrets.
- The current environment does not have the `motor` MongoDB driver installed, so a full application boot against MongoDB was not possible here.
- No real browser screenshot/visual regression test or production deployment test was performed.
- TMDb licensing/approved-logo completion remains an operator action as described above.

## 12. Deployment checklist

1. Rotate the exposed MongoDB credentials and update secrets outside source control.
2. Back up the source and tracking/storage databases.
3. Deploy the V2 source to staging first.
4. Open `/` and verify dynamic plan fields and the purchase/support links.
5. Open `/admin/plans`; edit a test plan and confirm the public Plans page reflects the edit.
6. Check `/admin` remains protected and `/admin/plans` requires admin authentication.
7. Check `/library`, search, pagination, New episodes, sorting and a title detail page.
8. Navigate back from a detail page and confirm the previous filter/page/scroll position is restored.
9. Confirm token/owner/exclusive media does not appear and public pages expose no technical stream details.
10. Confirm all three storage DBs are included in public aggregation according to the configured `storage_N` registry.
11. Confirm the public support and bot settings in Admin → Settings → Public Website.
12. Confirm plan cap changes are understood as display data only until a separate quota-entitlement sync is implemented.
13. Resolve TMDb licensing/branding with TMDb before public commercial launch.

---

**Release status:** Source-level V2 implementation and local tests are complete. Live database, browser and production verification remain deployment-stage checks.
