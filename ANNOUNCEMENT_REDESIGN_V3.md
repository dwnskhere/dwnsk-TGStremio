# Announcement Redesign V3 — Quality-added alerts

## Behaviour

When a movie, episode, or consolidated season receives a quality label that was not in its previous published quality set:

1. Edit the tracked canonical Telegram announcement so it always shows the complete current quality list.
2. Publish a second copy of the same rich announcement so the channel gets a new post notification.
3. Mark the new copy with `🆕 NEW QUALITY ADDED: 1080p` (or the newly-added labels), while retaining the full quality line such as `480p | 720p | 1080p`.
4. Track the alert's Telegram message ID so later content deletion or season consolidation also removes the alert copies.

A repeated ingestion event with no new quality label only refreshes the existing post; it does not create another alert. Replacing a stream at an already-listed quality is not treated as a new quality.

## V2 migration

V2 tracking records did not persist the quality set previously shown in the announcement. To avoid guessing, the database update path now captures the live quality labels immediately before it writes an incoming file. This pre-write snapshot is passed to the announcement handler. It allows the first quality addition after deploying V3 to generate the correct alert for existing V2 movie, episode, and consolidated-season posts.

If a legacy announcement record is encountered from a code path that does not provide a pre-write snapshot, V3 initializes its quality baseline silently rather than inventing a false "new quality" alert.

## Files changed

- `Backend/helper/announcer.py`: quality-delta detection, edited canonical post + fresh alert post, alert-ID tracking and cleanup.
- `Backend/helper/database.py`: pre-write snapshots for movie, episode, and season quality labels.
- `Backend/pyrofork/plugins/receiver.py`: pass snapshots from normal ingestion and manual-session ingestion to the announcer.
- `Backend/fastapi/templates/settings.html`: explain the new quality-alert behaviour.
- `README.md`: document the new behaviour.

## Validation

Python compilation and local smoke tests passed for:

- a new quality causing one edit plus one marked new post;
- a repeated event not creating a second alert;
- the first post-deployment quality addition on a legacy V2 tracking record;
- legacy records without a baseline being seeded without a false alert;
- rich-message and classic-fallback update banners.

A live Telegram/MongoDB integration test still needs to be run in the deployment environment.
