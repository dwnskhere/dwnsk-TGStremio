"""Small logging filters shared by the FastAPI/Uvicorn runtime."""

from __future__ import annotations

import logging
import re


class RedactSensitiveQueryFilter(logging.Filter):
    """Remove API credentials from request targets before they reach logs."""

    _sensitive_query = re.compile(r"(?i)([?&](?:api_key|apikey|token)=)[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            redacted = self._sensitive_query.sub(r"\1[REDACTED]", rendered)
            if redacted != rendered:
                record.msg = redacted
                record.args = ()
        except Exception:
            # Logging must never interfere with the server request lifecycle.
            pass
        return True
