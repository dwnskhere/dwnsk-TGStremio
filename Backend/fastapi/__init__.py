import logging

import uvicorn

from Backend.config import Telegram
from Backend.fastapi.main import app
from Backend.fastapi.log_redaction import RedactSensitiveQueryFilter

#----- Uvicorn server bound to the FastAPI app
Port = Telegram.PORT
config = uvicorn.Config(
    app=app,
    host="0.0.0.0",
    port=Port,
    loop="uvloop",
    http="httptools",
    timeout_keep_alive=60,
    timeout_graceful_shutdown=5,
    # Heroku terminates public HTTPS at its router and forwards the original
    # scheme in X-Forwarded-Proto. Trust that proxy metadata so PlaybackInfo
    # returns https:// URLs to Jellyfin clients rather than internal http://.
    forwarded_allow_ips="*",
)
_credential_log_filter = RedactSensitiveQueryFilter()
for _logger_name in ("uvicorn.error", "uvicorn.access"):
    logging.getLogger(_logger_name).addFilter(_credential_log_filter)
server = uvicorn.Server(config)
