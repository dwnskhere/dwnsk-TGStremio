# Missing Episode & Quality Audit Alerts for Approvers

## Scope

This update expands the existing approver notification sent by the season reconciler. It keeps the existing `Season announcement pending...` INFO log and the existing `APPROVER_IDS` recipient routing. The subscription-expiry `CHAT_ID_INVALID` code path is not changed.

## Telegram message format

Notifications are sent through the official Telegram Bot API `sendRichMessage` method using an `InputRichMessage` with explicit blocks and an `InputRichBlockTable`. The table contains three columns:

- **Episode** — season/episode code and TMDb episode title.
- **Available** — detected non-dead stream quality labels.
- **Missing** — `ALL MISSING` if no live stream exists, otherwise the expected quality tiers that are absent.

The message also includes the TV show title/year, a clickable TMDb ID, season number, season coverage, the number of episodes missing entirely, the number of episodes with quality gaps, and a short explanatory footer. If the Rich Message method is unavailable, the notifier falls back to a readable HTML message with the same episode and quality details.

Only episodes needing attention appear in the table. Fully indexed episodes with all expected tiers are omitted.

## Independent quality audit (Option A)

Each episode is checked independently for these three tiers:

| Tier | Accepted stored quality labels |
| --- | --- |
| 480p | `480p` |
| 720p | `720p` or `540p` |
| 1080p | `1080p`, `1440p`, `2160p`, `4K`, `8K`, or another parsed resolution above 1080p |

The tiers are independent. For example, an episode containing only `1080p` is reported as missing `480p` and `720p`. A higher tier does not satisfy a lower tier. `540p` is accepted only as the substitute for the 720p tier; the original stored label remains unchanged in the Available column.

Unknown labels do not satisfy any expected tier. They may still be shown in Available for an episode that has a live stream. Dead streams are excluded from both the indexed-episode and quality-tier checks.

## Aired-episode filtering

The audit uses the existing TMDb season metadata response and existing `Asia/Kolkata` date handling. Only regular episodes with a valid air date strictly before the current IST calendar date are included. Specials, season-zero/synthetic entries, episodes dated today or in the future, and episodes with missing/invalid air dates are excluded. Incomplete TMDb season metadata is not used to produce an audit.

## Season consolidation remains independent

The quality audit does not make quality completeness a prerequisite for season consolidation. An episode counts as indexed for season consolidation if it has at least one non-dead stream, as before. The consolidated season announcement still waits for every regular metadata episode to have aired and every expected aired episode to have at least one active stream. Missing optional quality tiers are reported to approvers but do not block consolidation.

## Notification state and deduplication

State remains in `tracking.season_missing_episode_alerts`, one record per TV show, season, and approver. The state now contains:

- `missing_episode_numbers` — episodes with no active stream;
- `missing_quality_tiers` — each episode's available labels and missing expected tiers;
- `alert_format_version` — format version `2`, ensuring existing episode-only alert states receive the new audit format once.

A new notification is sent only when the missing-episode set or quality-gap state changes. Unchanged hourly reconciliation runs do not send duplicates. When all episode and quality issues are resolved, the state is cleared without sending a separate completion DM. Each approver's delivery and retry remains independent.

## Validation

The source includes tests for:

- independent quality tiers (1080p does not satisfy 480p/720p);
- acceptance of 540p for the 720p tier;
- acceptance of 1440p/2160p/4K+ for the 1080p tier;
- all-missing episodes and exclusion of dead streams;
- native Telegram Rich Message table generation and clickable TMDb link;
- quality-gap changes triggering a new notification while unchanged state is deduplicated;
- existing aired-episode filtering for future dates, specials, and incomplete TMDb metadata.

Run:

```bash
python -m compileall -q Backend tests
python -m unittest tests.test_missing_episode_alert_logic tests.test_missing_episode_notifier -v
```

A live Telegram/TMDb/MongoDB test is still required after deployment.
