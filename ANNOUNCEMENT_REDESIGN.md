# Telegram announcement redesign — second pass

This redesign continues to modify the existing Telegram announcement pipeline and keeps the configured announcement channel, Stremio/Nuvio/Get Addon buttons, existing message tracking and media indexing behaviour.

## Image selection

- **Movies:** one official TMDb portrait poster, followed by up to four landscape TMDb backdrops from `GET /movie/{movie_id}/images`. The canonical TMDb `poster_path` and `backdrop_path` are used as safe fallbacks when the images endpoint does not include them.
- **TV series and consolidated seasons:** one official portrait series/season poster, followed by up to four landscape promotional backdrops from `GET /tv/{series_id}/images`. A season-specific poster is preferred when TMDb has one; otherwise the canonical series poster is used.
- **Individual TV episodes:** up to five landscape episode stills from `GET /tv/{series_id}/season/{season_number}/episode/{episode_number}/images`. Generic series posters or backdrops are not substituted when no episode still is available.
- The announcement gallery uses TMDb-owned image paths. Portrait posters are never placed in a landscape slot; every backdrop and episode still is checked against its TMDb aspect ratio / width / height fields.
- The assets are embedded in one Telegram Rich Message slideshow rather than sent as separate album messages.

## Announcement layout

The rich text now follows the supplied two-column table layout:

1. A title line with media type and episode/season number where applicable.
2. A compact two-column table with `Original Name` / `AKA`, `Type` / `Genres`, and `Runtime` / embedded `TMDB Link`; each cell places its label above its value, as in the supplied example.
3. A collapsed `Overview (Tap to Expand)` section.
4. A separate `📶 Quality: 480p | 720p | 1080p` line.

TMDb supplies original names, alternative titles, runtime, episode stills, and links. For episodes, AKA is formatted as `Series Name S02E01 - Episode Name`. When an alias or runtime is not available, the announcement uses `N/A` or the best available episode-runtime estimate. Rating was omitted to match the latest sample template.

## Announcement and quality behaviour

- Movies retain one active post per TMDb movie. When a genuinely new quality is indexed, a fresh replacement post is published with a `NEW QUALITY ADDED` banner, its new Telegram message ID is saved, and only then are the old active post and any V3 alert copies deleted.
- Episodes retain one active post per season/episode. New qualities use the same publish → persist → delete replacement sequence.
- A completed season is published once the metadata-defined checklist is complete and each expected regular episode has at least one non-dead indexed stream. Season 0, specials, and synthetic combined-pack entries are excluded.
- The season post is confirmed before individual episode posts are deleted. Failed deletions are marked for retry by the hourly reconciler.
- Later quality additions replace the season post using the same publish → persist → delete sequence.
- Quality values are unique, sorted low-to-high, and dead streams (`is_dead=True`) are excluded.
- The hourly reconciler can recover earlier episode-tracking rows even if older rows do not have the newer `kind` metadata field.

## Telegram compatibility

The main path uses Telegram Rich Messages (`sendRichMessage`) and updates existing rich posts with `editMessageText.rich_message`. The compatibility fallback uses a conventional HTML message and a single poster because traditional Telegram captions do not support Rich Message tables, collapsible sections, or slideshows.

## Season completion diagnosis

The season checker now emits a clear log when TMDb metadata is incomplete, an episode is missing an air date, a season document is absent, or particular released episode numbers have not yet been indexed with a live stream. Negative metadata results are cached for five minutes instead of an hour so a temporary incomplete TMDb response does not delay another attempt for as long.

For Marshals Season 1, current public listings show 13 episodes, ending with S01E13 `Wolves at the Door`. If only S01E01–S01E12 are indexed, consolidation correctly waits for E13. If all 13 episodes are present and healthy, the new pending log will identify whether the metadata endpoint or database is still preventing consolidation. The supplied ZIP does not include the live MongoDB contents, so the actual production records could not be independently inspected here.

## Files changed in this pass

- `Backend/helper/announcer.py` — per-media TMDb image selection, poster/backdrop orientation enforcement, two-column rich template, collapsed overview, runtime/AKA enrichment, and season reconciliation diagnostics/recovery.
- `Backend/helper/metadata/providers/tmdb.py` — alternative-title extraction now supports both movie `title` and TV `name` fields and exposes a public cached helper for announcements.

No database schema migration or new dependency is required.

## Validation

Python bytecode compilation was run across `Backend`. Focused local smoke tests were run for the rich template (movie and episode), quality ordering and dead-stream filtering, movie/TV poster-plus-backdrop selection, landscape-only episode stills, one-message slideshow structure, and metadata season completion (13 aired episodes, a future-dated episode, and a special). A live Telegram/MongoDB/TMDb integration test still requires the user's deployment credentials and actual production database.

## Production database limitation

The supplied ZIP contains the application source only; it does not include the live MongoDB collections or the tracking rows for the user's Marshals test. The code therefore cannot verify which S01E numbers or stream qualities are actually present in production. The new log lines report the expected episode numbers and the exact missing numbers when the hourly season reconciliation runs.
