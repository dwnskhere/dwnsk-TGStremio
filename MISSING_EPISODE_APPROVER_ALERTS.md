# Missing Aired Episode Alerts for Approvers

## Scope

This change implements the missing-season-episode notification only. The subscription-expiry `CHAT_ID_INVALID` issue is intentionally unchanged and remains a separate follow-up.

## Behaviour

When the existing season consolidation check runs, the bot now also checks the complete TMDb season episode response for regular episodes whose `air_date` is strictly before the current date in `Asia/Kolkata`. TMDb's season response provides a date but not a reliable per-episode release time, so episodes dated today are held until the next IST calendar day to avoid alerting for an episode that may still be scheduled to air later today.

For each season:

1. Fetch the TV show and season metadata from TMDb.
2. Verify that TMDb returned the full season episode list. Partial metadata is not used to generate a missing-episode alert.
3. Exclude specials, season-zero entries and synthetic combined-pack episode numbers.
4. Include only episodes with a valid `air_date` that is today or earlier.
5. Compare those aired episode numbers with the indexed season records. An episode counts as indexed only when it has at least one non-dead Telegram stream.
6. Send a private HTML-formatted notification to each configured approver when the missing episode list changes.
7. Keep the existing season-pending log message and existing season-consolidation rules.

Episodes dated today or later, plus episodes with missing/invalid air dates, are not listed as missing. A season with future or same-day episodes is still not consolidated; the existing all-regular-episodes-must-have-aired rule remains intact.

## Example notification

```text
⚠️ Missing Aired Episodes — Season Announcement Pending

📺 The Family Man (2021) [TMDB ID: 123456]
Season 02 — Missing Episodes
• S02E04 — Exile
• S02E07 — The New Threat

📦 Indexed: 6/8 aired episodes
Only episodes with an air date before today's IST date are listed. Today's and future episodes are excluded until confirmed aired.
Please add the missing aired episodes to the media library.
```

The actual message uses Telegram HTML formatting, with the series name/year, TMDb ID, season number and each missing episode code/title.

## Recipients

Recipients are read from the current `APPROVER_IDS` setting in SettingsManager. If that list is empty, the environment-backed `Telegram.APPROVER_IDS` list is used. It does not fall back to `OWNER_ID`; only configured subscription approvers receive these alerts.

Each approver must have opened the bot and pressed `/start` at least once so the bot can initiate a private conversation.

## Duplicate prevention and retries

Alert state is stored in the tracking database collection `season_missing_episode_alerts`, with one record per TV show, season and approver.

- The same missing episode list is notified only once per approver.
- If the missing list changes, a new notification is sent with the current list.
- If the season becomes complete, the stored missing list is cleared without sending a separate completion DM.
- A failed send is not marked as delivered, so the next season check retries it.
- Delivery is tracked independently per approver; a failure for one approver does not cause duplicates for other approvers.

## Logging

The existing log line is retained:

```text
Season announcement pending for tv=<TMDB_ID> S<SEASON>: indexed <INDEXED>/<AIRED_EXPECTED> released metadata episodes; missing episode numbers=[...]
```

A successful private alert also writes an INFO log identifying the approver and missing episode numbers. A failed send writes a WARNING log. The subscription-expiry `CHAT_ID_INVALID` path is not modified.

## Validation

The source includes focused tests for:

- excluding future episodes and specials from the alertable list;
- ignoring episodes without valid air dates;
- refusing to alert from incomplete TMDb metadata;
- preserving season completion when every regular episode has aired;
- notification message fields and per-approver duplicate suppression.

Run:

```bash
python -m compileall -q Backend tests
python -m unittest tests.test_missing_episode_alert_logic tests.test_missing_episode_notifier -v
```

A live TMDb/Telegram/MongoDB deployment test is still required after deployment.
