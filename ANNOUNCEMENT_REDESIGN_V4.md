# Telegram Announcement Redesign V4 — replace posts on quality updates

## Behaviour change from V3

V3 edited the original announcement and then published a separate quality-alert copy. That left multiple posts for the same media item in the channel. V4 changes the quality-update flow to replacement semantics:

1. Detect a genuinely new quality label by comparing the current live quality set with `published_qualities` (or the V2 pre-ingestion snapshot during migration).
2. Publish one new announcement containing the full current quality list and a `🆕 NEW QUALITY ADDED: ...` banner.
3. Persist the new Telegram `chat_id` and `message_id` as the active announcement in MongoDB.
4. Queue the previous active post, all V3 `quality_alert_message_ids`, the V3 `last_quality_alert.message_id`, and any earlier pending deletion references for removal.
5. Delete the queued old posts only after the replacement post and its active tracking record have been saved.

A successful quality update therefore leaves one active announcement per movie, episode, or consolidated season. The fresh replacement provides a new channel post/notification while retaining the complete list of qualities.

## Safety and recovery

- If Telegram cannot confirm that the replacement post was sent, the previous post and V3 alert copies are left untouched.
- If MongoDB cannot save the new active message ID, previous posts are deliberately not deleted.
- Old Telegram messages are queued in `pending_delete_messages` as `{chat_id, message_id}` pairs before deletion starts. This preserves the correct channel ID even if the configured announcement channel changes later.
- If Telegram refuses or temporarily fails to delete an old post, the failed reference remains queued. The announcement reconciler retries queued deletions on its next pass; the current active message ID is not changed by a cleanup retry.
- Repeated ingestion of an already-listed quality edits the current active post and does not publish another quality alert.
- Existing V3 `quality_alert_message_ids` and `last_quality_alert` entries are collected and removed on the next successful replacement or ordinary announcement refresh.
- The season consolidation cleanup also understands the V4 pending deletion queue and continues to remove episode posts only after a consolidated season post exists.

## Files changed in V4

- `Backend/helper/announcer.py`
  - Replaced V3 edit-plus-alert behaviour with publish → persist → delete.
  - Added per-message deletion references and retryable pending cleanup.
  - Cleans up V3 alert copies on the next successful refresh/replacement.
  - Includes pending cleanup references when deleting a full movie/series announcement or retiring episode posts after season consolidation.
- `Backend/fastapi/templates/settings.html` — updates the setting description to explain replacement posts.
- `README.md` — documents the replacement behaviour.
- `ANNOUNCEMENT_REDESIGN.md` — updates the quality-update description.

## Database compatibility

No new collection or external dependency is introduced. Existing `tracking.announced` documents remain usable. V3 fields (`quality_alert_message_ids`, `last_quality_alert`) are read during migration and cleared after those old posts are queued. V4 adds `pending_delete_messages`, a list of Telegram chat/message pairs; it is stored on the existing announcement tracking document.

## Validation

- Python compilation is checked for the modified module and the full Backend package.
- Local mocked state-machine tests verify: new quality sends a replacement before MongoDB tracking is updated; old canonical/V3 posts are deleted only afterwards; send failure preserves the old post; repeated quality edits the active post without sending a duplicate; V3 alert copies are cleaned on the next successful refresh; and failed deletions remain queued.
- A live Telegram + MongoDB test still requires the deployment bot token, channel permissions, and actual production database. This package does not claim that an online integration test was run.

## Deployment note

Back up MongoDB and the current source before deployment. The first subsequent announcement event for each existing V3 movie/episode/season will either clean older V3 alert copies during a normal refresh or replace all tracked prior copies when a new quality is detected. A failed Telegram deletion remains queued for retry rather than being silently forgotten.
