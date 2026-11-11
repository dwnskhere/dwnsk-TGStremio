# Jellyfin `/web` assets missing after Heroku startup — root cause and fix

## What happened

The `jellyfin-test` branch and the v3 ZIP are the same compatibility code. The Heroku log's `Latest commit ID: e50d33a98eebd9d23762830e03947d3631ca0b85` is the branch commit containing that code; it is not evidence that another/unknown repository was deployed.

The Dockerfile copies the official Jellyfin Web 10.10.7 static frontend into:

```text
Backend/fastapi/jellyfin_web/
```

Those files are generated into the container image during `docker build`; they are intentionally not committed into the Git repository.

The old entrypoint was:

```bash
uv run update.py && uv run -m Backend
```

At process startup, `update.py` reads the upstream repository/branch saved in MongoDB or Config Vars, initializes Git in the app directory, stages the current image contents, and then performs `git reset --hard origin/<branch>`. The reset restores the Git-tracked repository state and removes the generated web assets that were copied into the image. FastAPI then starts without `jellyfin_web/index.html`, so `/web/index.html` returns 404 and the health check reports `WebClientAssetsPresent: false`.

The issue is therefore the runtime self-update in the compatibility container. It is not a typo in the Jellyfin URL, and the configured repository/branch is still your compatibility project.

## Fix in this patch

1. `start.sh` skips `update.py` whenever `JELLYFIN_COMPAT_ENABLED` is true.
2. Before starting FastAPI, `start.sh` checks that the Docker-generated `Backend/fastapi/jellyfin_web/index.html` exists. If not, it exits with a clear error rather than running an API-only build.
3. `update.py` independently skips upstream reset unconditionally whenever Jellyfin compatibility is enabled. The former `JELLYFIN_ALLOW_UPSTREAM_AUTO_UPDATE` escape hatch is no longer honored in this mode.
4. The deployment safety test now deliberately sets the old override to `true` and proves that the update is still skipped. It also tests that the process entrypoint runs only `uv run -m Backend` in compatibility mode and refuses to boot if the frontend is absent.

## What you should do

- Keep your existing `UPSTREAM_REPO` and `UPSTREAM_BRANCH` values; do not delete your `jellyfin-test` branch or the saved repo settings just to address this issue.
- Keep `JELLYFIN_COMPAT_ENABLED=true` in Heroku Config Vars.
- Commit/deploy this patch through the Heroku Container workflow so the image is rebuilt from the Dockerfile.
- On the next startup, expect this log:

  ```text
  [START] Jellyfin compatibility is enabled; skipping runtime upstream self-update.
  ```

- You should no longer see `Successfully updated with latest commits!!` or `Latest commit ID` from the startup updater on that compatibility dyno.
- Then verify `/web/index.html` returns the Jellyfin Web app (HTTP 200), `WebClientAssetsPresent` is true, and finally configure `JELLYFIN_SOURCE_TOKEN` if the health response still says it is missing.

## Validation

The local API contract and deployment-safety suites pass. The deployment-safety test now covers this exact runtime reset scenario. A real Docker build and the next Heroku release still need to run in GitHub Actions/Heroku because Docker is not available in the local editing environment.
