# Missing Episode & Quality Audit Alerts for Approvers

## Scope

This update expands the existing approver notification sent by the season reconciler. It keeps the existing `Season announcement pending...` INFO log and the existing `APPROVER_IDS` recipient routing. The subscription-expiry `CHAT_ID_INVALID` code path is not changed.

## Delivery format: Telegraph page + compact Telegram notice

The detailed episode and quality audit is now published to **Telegraph**. Approvers receive a short Telegram message containing the show name, season, indexed/missing counts, and an **Open full audit** button linking to the Telegraph page.

Telegraph's official API supports rich headings, paragraphs, links, lists, quotes, code and media nodes. It does **not** support HTML `<table>` elements. To avoid unsupported markup, the page represents each affected episode as a clearly separated section with three labelled values:

- **Episode** — season/episode code and TMDb episode title;
- **Available** — detected non-dead stream quality labels;
- **Missing** — `ALL MISSING` if no live stream exists, otherwise the expected quality tiers that are absent.

The page also includes the TV show title/year, a clickable TMDb ID, season number, season coverage, counts for completely missing episodes and quality gaps, quality rules, and the aired-episode filtering explanation. Only episodes needing attention are included. Fully indexed episodes with all expected tiers are omitted.

### Telegraph account and page storage

- The app lazily creates a Telegraph account via `createAccount` the first time it needs to publish an audit.
- The access token is stored in MongoDB at `tracking.telegraph_accounts`, document `_id=season_quality_audit`; no additional environment variable is required.
- One stable page is maintained per TV show and season in `tracking.season_missing_episode_telegraph_pages`.
- When the audit changes, the app edits the existing page with `editPage`, keeping the URL stable. If Telegraph reports that the saved page no longer exists or cannot be edited, the app creates a replacement page and stores its new URL/path.
- When all issues are resolved, the same page is updated to show an all-clear status. Failed page updates are retried on later reconciliation runs.

**Visibility note:** Telegraph pages are public to anyone who has the URL. The Telegram notification itself is still sent only to the configured approvers, but the linked audit page should not contain secrets or private user information.

## Independent quality audit (Option A)

Each episode is checked independently for these three tiers:

| Tier | Accepted stored quality labels |
| --- | --- |
| 480p | `480p` |
| 720p | `720p` or `540p` |
| 1080p | `1080p`, `1440p`, `2160p`, `4K`, `8K`, or another parsed resolution above 1080p |

The tiers are independent. For example, an episode containing only `1080p` is reported as missing `480p` and `720p`. A higher tier does not satisfy a lower tier. `540p` is accepted only as the substitute for the 720p tier; the original stored label remains unchanged in the Available value.

Unknown labels do not satisfy any expected tier. They may still be shown in Available for an episode that has a live stream. Dead streams are excluded from both the indexed-episode and quality-tier checks.

## Aired-episode filtering

The audit uses the existing TMDb season metadata response and existing `Asia/Kolkata` date handling. Only regular episodes with a valid air date strictly before the current IST calendar date are included. Specials, season-zero/synthetic entries, episodes dated today or in the future, and episodes with missing/invalid air dates are excluded. Incomplete TMDb season metadata is not used to produce an audit.

## Season consolidation remains independent

The quality audit does not make quality completeness a prerequisite for season consolidation. An episode counts as indexed for season consolidation if it has at least one non-dead stream, as before. The consolidated season announcement still waits for every regular metadata episode to have aired and every expected aired episode to have at least one active stream. Missing optional quality tiers are reported to approvers but do not block consolidation.

## Notification state and deduplication

State remains in `tracking.season_missing_episode_alerts`, one record per TV show, season, and approver. It contains:

- `missing_episode_numbers` — episodes with no active stream;
- `missing_quality_tiers` — each episode's available labels and missing expected tiers;
- `alert_format_version` — format version `3`, causing existing version-2 alerts to receive the Telegraph link format once;
- `delivery_status` — `sent` or `resolved`;
- `telegraph_url` and `telegraph_path` — link to the current season audit page.

A new Telegram notice is sent only when the missing-episode set or quality-gap state changes, or when a prior delivery has not succeeded. Unchanged hourly reconciliation runs do not send duplicates. When all episode and quality issues are resolved, the state is cleared without a separate completion DM, and the existing Telegraph page is updated to show that the audit is clear.

## Validation

The source includes tests for:

- independent quality tiers (1080p does not satisfy 480p/720p);
- acceptance of 540p for the 720p tier;
- acceptance of 1440p/2160p/4K+ for the 1080p tier;
- all-missing episodes and exclusion of dead streams;
- Telegraph-compatible node tags and separated episode sections;
- automatic Telegraph account creation and token persistence;
- stable Telegraph page creation/editing;
- Telegram link notifications and deduplication when audit state changes;
- existing aired-episode filtering for future dates, specials, and incomplete TMDb metadata.

Run:

```bash
python -m compileall -q Backend tests
python -m unittest tests.test_missing_episode_alert_logic tests.test_missing_episode_notifier -v
```

A live Telegraph/TMDb/Telegram/MongoDB test is still required after deployment.

## Official reference

- [Telegraph API](https://telegra.ph/api) — `createAccount`, `createPage`, `editPage`, and supported content NodeElement tags.
