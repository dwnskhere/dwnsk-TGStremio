# Telegram announcement redesign

This update redesigns the existing announcement sender used by the configured announcement channel. It does not add a second announcement system or change the existing channel setting.

## Behaviour

- **Movies:** one post per TMDb movie. New quality versions edit that post in place.
- **Episodes:** one post per season/episode. New qualities edit that episode's post in place.
- **Completed seasons:** once TMDb metadata lists all regular episodes for a season as released, and every one of those episodes has at least one live indexed Telegram stream, the bot publishes one season post. It only deletes the episode posts after the season post has been successfully sent or edited.
- **Later season quality additions:** edit the season post rather than recreating an episode post.
- **Quality text:** a simple ascending list, e.g. `480p | 720p | 1080p`. Dead links (`is_dead=True`) are not shown or counted toward season completion.
- **Specials:** Season 0 and episodes marked `episode_type=special` are excluded from season completion and the season quality summary. Unreleased episodes are excluded until their metadata air date has passed.
- **Posters:** up to five distinct portrait posters are presented in a Telegram Rich Message slideshow. Landscape backdrops are not substituted for posters.
- **Buttons:** the existing Stremio, Nuvio and Get Addon buttons and their existing URL patterns are retained.

The compact announcement contains the title/year, type, rating (when available), genres (when available), the quality list, and a shortened plot/overview. Titles and metadata are HTML-escaped before being sent.

## Telegram API compatibility

The slideshow uses Telegram Rich Messages (`sendRichMessage`) and updates them with `editMessageText.rich_message`. Telegram added Rich Messages in Bot API 10.1 and the explicit rich-message media list in Bot API 10.2. The code uses the hosted Bot API endpoint and the existing `BOT_TOKEN`; no additional Python dependency or local Bot API server is required. If Rich Messages are unavailable, it falls back to the existing Pyrogram sender with a single poster (or a text post).

The bot must be an administrator in the announcement channel with permission to post messages. It also needs permission to delete its prior episode posts when a season is consolidated. The current Stremio/Nuvio/Get Addon buttons continue to be generated from the existing settings and helper.

## Season completion requirements

Season consolidation needs a working TMDb API key from the existing `tmdb_api` setting (or `TMDB_API` config value), a season metadata response with a complete episode list, an air date for every regular episode, all regular episode air dates in the past, and at least one non-dead indexed stream for each expected episode. Missing or incomplete metadata deliberately prevents consolidation rather than guessing.

An hourly reconciler rechecks pending seasons. This handles the case where all episodes were already indexed before the final episode's official air date. The reconciler also retries deletion of episode announcements if Telegram temporarily refuses a delete.

## Database and migration

No collection migration or new dependency is required. The existing `tracking.announced` collection is reused with these IDs:

- `movie:<tmdb_id>`
- `tv:<tmdb_id>:episode:<season>:<episode>`
- `tv:<tmdb_id>:season:<season>`

The records store the Telegram `chat_id` and `message_id` needed to edit or delete posts. The previous TV key (`tv:<tmdb_id>`) is retired when a replacement episode or season post has been successfully published. The current announcement channel, enable/disable setting, and indexing/streaming settings remain unchanged.

## Files changed

- `Backend/helper/announcer.py` — redesigned formatter, poster slideshow, message editing, quality aggregation, metadata-based season consolidation, delete/retry tracking.
- `Backend/pyrofork/plugins/receiver.py` — route manual/personal-session additions through the same announcement updater.
- `Backend/__main__.py` — start the idempotent hourly season reconciler after startup.
- `Backend/fastapi/templates/settings.html` — clarify the existing announcement setting's behaviour.

## Validation performed

- Python `compileall` across `Backend`.
- Focused pure-function smoke tests for sample formatting, quality sorting and dead-link exclusion, multi-poster slideshow markup, metadata completion gating (specials/future/missing metadata), and season quality aggregation.

A live integration test was not possible in this workspace because the project runtime dependencies, MongoDB, Telegram bot token, and TMDb API credentials were not available here. Run a test deployment with the above permissions before replacing a production instance.
