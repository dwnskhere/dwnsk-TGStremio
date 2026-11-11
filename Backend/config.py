from os import getenv, path

from dotenv import load_dotenv

load_dotenv(path.join(path.dirname(path.dirname(__file__)), "config.env"))


def _int_env(key: str, default: int = 0) -> int:
    try:
        return int((getenv(key) or "").strip())
    except ValueError:
        return default


#----- Environment-backed configuration
class Telegram:
    #----- Required: Telegram clients
    API_ID              = _int_env("API_ID")
    API_HASH            = getenv("API_HASH", "")
    BOT_TOKEN           = getenv("BOT_TOKEN", "")

    #----- Required: Database URIs
    DATABASE = [db.strip() for db in (getenv("DATABASE") or "").split(",") if db.strip()]

    #----- Required: Server
    PORT     = _int_env("PORT", 8000)
    OWNER_ID = _int_env("OWNER_ID")

    #----- Read/Write via SettingsManager
    REPLACE_MODE                  = getenv("REPLACE_MODE", "true").lower() == "true"
    HIDE_CATALOG                  = getenv("HIDE_CATALOG", "false").lower() == "true"
    AUTH_CHANNEL                  = [c.strip() for c in (getenv("AUTH_CHANNEL") or "").split(",") if c.strip()]
    TMDB_API                      = getenv("TMDB_API", "")
    TVDB_API                       = getenv("TVDB_API", "")
    BASE_URL                      = getenv("BASE_URL", "").rstrip("/")
    UPSTREAM_REPO                 = getenv("UPSTREAM_REPO", "")
    UPSTREAM_BRANCH               = getenv("UPSTREAM_BRANCH", "")
    ADMIN_USERNAME                = getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD                = getenv("ADMIN_PASSWORD", "admin")
    SUBSCRIPTION                  = getenv("SUBSCRIPTION", "false").lower() == "true"
    SUBSCRIPTION_GROUP_ID         = _int_env("SUBSCRIPTION_GROUP_ID")
    APPROVER_IDS                  = [int(x.strip()) for x in (getenv("APPROVER_IDS") or "").split(",") if x.strip().isdigit()]
    HTTP_PROXY_URL                = getenv("HTTP_Proxy_URL", "")
    SHOW_PROXY_AND_NON_PROXY_BOTH = getenv("SHOW_ProxyAndNonProxyBoth", "false").lower() == "true"

    #----- WebDAV (optional env fallback; prefer Settings page)
    WEBDAV_USER     = getenv("WEBDAV_USER", "")
    WEBDAV_PASSWORD = getenv("WEBDAV_PASSWORD", "")

    #----- Emby / Jellyfin STRM Export
    STRM_EXPORT_DIR = getenv('STRM_EXPORT_DIR', '')

    #----- Experimental Jellyfin Android API compatibility (no separate Jellyfin host)
    JELLYFIN_COMPAT_ENABLED = getenv("JELLYFIN_COMPAT_ENABLED", "false").lower() == "true"
    JELLYFIN_USERNAME = getenv("JELLYFIN_USERNAME", "")
    JELLYFIN_PASSWORD = getenv("JELLYFIN_PASSWORD", "")
    # API token used to decide which Telegram-Stremio titles this Jellyfin
    # account may see and to authorize the existing Telegram stream pipeline.
    # Falls back to the STRM default token stored in SettingsManager.
    JELLYFIN_SOURCE_TOKEN = getenv("JELLYFIN_SOURCE_TOKEN", "").strip()
    JELLYFIN_SERVER_ID = getenv("JELLYFIN_SERVER_ID", "")
    JELLYFIN_SERVER_NAME = getenv("JELLYFIN_SERVER_NAME", "Telegram-Stremio")
    JELLYFIN_TOKEN_TTL_DAYS = _int_env("JELLYFIN_TOKEN_TTL_DAYS", 30)
    JELLYFIN_PLAYBACK_TICKET_TTL_SECONDS = _int_env("JELLYFIN_PLAYBACK_TICKET_TTL_SECONDS", 21600)
