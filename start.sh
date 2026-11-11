#!/usr/bin/env bash
set -euo pipefail

# The Jellyfin Web frontend is added to the application image during the
# Docker build. It is intentionally not committed to Git. The legacy updater
# rebuilds the working tree from UPSTREAM_REPO at runtime and would therefore
# remove those generated assets even when it updates to this same branch.
compat_enabled="${JELLYFIN_COMPAT_ENABLED:-false}"
case "${compat_enabled,,}" in
  true|1|yes|on)
    echo "[START] Jellyfin compatibility is enabled; skipping runtime upstream self-update."
    if [[ ! -s "Backend/fastapi/jellyfin_web/index.html" ]]; then
      echo "[START] FATAL: Jellyfin Web assets are missing from this deployment image." >&2
      echo "[START] Deploy using the repository Dockerfile / Heroku Container stack." >&2
      exit 1
    fi
    ;;
  *)
    echo "[START] Jellyfin compatibility is disabled; running the normal upstream updater."
    uv run update.py
    ;;
esac

exec uv run -m Backend
