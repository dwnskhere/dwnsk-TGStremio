# Use the matching official Jellyfin Web distribution as a build-time asset
# source only.  We copy its static frontend into Telegram-Stremio; this does
# NOT run or deploy a Jellyfin server process.
FROM jellyfin/jellyfin:10.10.7 AS jellyfin-web-assets

FROM ghcr.io/astral-sh/uv:debian-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV MALLOC_ARENA_MAX=2
ENV LANG=en_US.UTF-8
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_NO_SYNC=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
	bash \
        git \
        curl \
        ca-certificates \
        locales && \
    locale-gen en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
# Jellyfin Android's embedded WebView loads the server's /web/ frontend.
# The official Jellyfin 10.10.7 image ships the matching static web client at
# this path.  Copy only the web assets, not the server executable/runtime.
COPY --from=jellyfin-web-assets /jellyfin/jellyfin-web/ /app/Backend/fastapi/jellyfin_web/
# Fail the image build instead of deploying an API-only build that the Android
# WebView cannot open. The verifier checks index.html and every static src/href.
RUN python tools/verify_jellyfin_web_assets.py
RUN uv sync --locked --no-dev
RUN chmod +x start.sh
CMD ["bash", "start.sh"]
