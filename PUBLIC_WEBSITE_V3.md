# CodecIndex Public Website V3

**Release scope:** Approved homepage content, FAQ/policy copy, current unlimited monthly-cap rule, and public-page support/purchase links. Built on Public Website V2.

## What changed

### Plans homepage (`/` and `/plans`)

The public Plans experience remains the homepage. It now includes:

1. CodecIndex service introduction and CTAs to open the configured activation bot or browse `/library`.
2. Live public-only movie, TV-show and episode counts from the configured storage databases.
3. Dynamic subscription cards sourced from `tracking.sub_plans`.
4. A service feature overview based on the owner-supplied CodecIndex description.
5. A four-step activation/setup overview.
6. Plan usage information.
7. Interactive FAQ accordion entries covering activation, payment verification, Nuvio/Stremio compatibility, managed hosting, limits, tokens, playback options, refunds, service availability and support.
8. Fair-use, refund, service-availability and third-party disclaimers using neutral, non-promissory wording.
9. Links to the public library, configured bot/support destinations, official Telegram channel and the supplied Terms/Usage Policy URL.

No fake reviews, testimonials, star ratings, or invented customer statistics are included.

### Public wording decisions

- Canonical brand spelling: `CodecIndex`.
- Compatibility is described as intended for supported Nuvio and Stremio environments; the copy notes that behaviour can vary by player, platform and version. It does not claim universal compatibility.
- Payment and activation are described as flows handled through the Telegram bot. Review time is explicitly variable; no fixed activation time is promised.
- Refund requests may be reviewed under the applicable policy; eligibility and outcome are not guaranteed.
- Service availability copy notes third-party infrastructure and possible interruptions. It does not promise a restoration time or compensation.
- Terms & Conditions and Usage Policy both initially link to `https://t.me/CodecIndex/5`, because that is the single destination supplied by the owner.

### Monthly cap policy for this release

Monthly usage is currently Unlimited for all offered plans.

- `monthly_cap` remains in the tracking plan schema for future flexibility.
- `Database.get_subscription_plans()` performs a one-time-per-process migration of existing plan documents to `monthly_cap: "Unlimited"`.
- New and edited plans are stored with `monthly_cap: "Unlimited"` regardless of submitted client values.
- The admin plan editor displays the monthly field as read-only (`Unlimited (current release)` context); name, validity, price, currency and daily cap remain editable.
- The public plan projection returns `Unlimited` even before/without a completed legacy migration, preventing stale finite values from appearing publicly.
- This is a display/configuration rule for this release. It does not alter daily quota enforcement, existing token quotas, or subscriber usage state.

### Purchase and support links

- Purchase CTA opens the configured Telegram bot directly, without a plan-specific start payload.
- Support CTA reads the existing `public_support_contact` Admin Settings field.
- The support contact setting and URL validation were already present in V2 and are retained; no duplicate setting is added.

### Public catalogue and privacy

All V2 rules remain in place:

- `/library` is the public catalogue.
- Public movie/TV details remain informational only.
- Library cards show title, year and poster; no stream-quality badges or technical file fields.
- Existing anonymous/public visibility and exclusivity checks remain enforced in database reads.
- Public collection counts aggregate only records matching those public visibility rules.
- Public endpoints do not expose Telegram IDs/file IDs, original filenames, storage DB index, Mongo `_id`, access tokens, add-on URLs, subscriber/payment records or technical stream data.
- Library RAM cache remains in-process and bounded with a 24-hour maximum TTL; application media changes invalidate it. Plans continue to be read from the tracking DB.
- Library Back navigation retains filters, search, sort, page and scroll restoration.
- TV episode rows keep the responsive thumbnail/text layout.

## Files changed

- `Backend/fastapi/templates/public_plans.html` — expanded homepage content and cautious policies/FAQ.
- `Backend/fastapi/templates/public_base.html` — responsive styles for information and useful-link sections.
- `Backend/fastapi/routes/public_site.py` — supplies public-only collection stats and the approved FAQ/link context.
- `Backend/helper/database.py` — one-time normalization of all current monthly caps to Unlimited; new/updated plan documents remain Unlimited.
- `Backend/fastapi/routes/api_routes.py` — admin plan API always stores Unlimited monthly cap for this release.
- `Backend/fastapi/templates/subscriptions_manage.html` — monthly cap shown read-only as Unlimited.
- `tests/test_public_site_safety.py` and `tests/test_plan_admin_payload.py` — regression coverage for approved copy and fixed monthly-cap behaviour.

## Validation

- `pytest -q`: **42 passed, 5 subtests passed**.
- `python -m compileall -q Backend`: passed.
- All 19 Jinja templates parsed successfully.
- Public statistics are obtained through the existing public-only aggregation helper.
- Plan payload tests confirm submitted numeric monthly values are ignored and stored as Unlimited.
- Existing public privacy, visibility, pagination, cache and navigation regression tests pass.

## Not performed

- No live MongoDB connection was made. The database credentials pasted into chat should be rotated before any live validation; they were not used or copied into this source.
- No live Telegram/bot purchase flow was performed.
- No browser-based screenshot or production deployment test was performed.
- Plan cap values here are not synchronized into the token quota/enforcement engine; that remains outside this release.
- TMDb commercial-use licensing/logo requirements remain an operator action from V2.

## Deployment notes

1. Back up V2 source and databases.
2. Rotate the MongoDB credentials exposed in the conversation and update deployment secrets separately.
3. Deploy to staging.
4. Verify `/` renders plans and public-only collection statistics.
5. Edit a plan under `/admin/plans`; verify name, validity, price, currency and daily cap update on `/`.
6. Confirm monthly cap remains read-only and Unlimited after create/edit/reload.
7. Configure `public_bot_username` and `public_support_contact` under Admin → Settings → Public Website; verify their links on the public page.
8. Confirm `/admin` remains authenticated and private/exclusive media remains absent from `/library` and public counts.
9. Confirm the FAQ, neutral policy notes and Terms/Usage Policy links are accurate before public launch.
