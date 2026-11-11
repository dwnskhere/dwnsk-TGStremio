"""Central runtime log-file location.

Heroku container dynos run as a non-root user and the application directory is
read-only. Use /tmp there; local/source deployments retain the historical
log.txt path unless APP_LOG_FILE explicitly overrides it.
"""
from os import environ

LOG_FILE = environ.get("APP_LOG_FILE") or (
    "/tmp/telegram-stremio.log" if environ.get("DYNO") or environ.get("PORT") else "log.txt"
)
