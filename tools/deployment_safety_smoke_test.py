#!/usr/bin/env python3
"""Smoke tests for Jellyfin-mode deployment safety (no network or credentials)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE = ROOT / "update.py"
ASSET_VERIFIER = ROOT / "tools" / "verify_jellyfin_web_assets.py"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-heroku-container.yml"
DOCKERFILE = ROOT / "Dockerfile"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tgst-jellyfin-update-") as tmp:
        env = os.environ.copy()
        env.pop("DATABASE", None)
        env.pop("UPSTREAM_REPO", None)
        env.pop("UPSTREAM_BRANCH", None)
        # Even an old/accidental override must not re-enable a runtime reset
        # that deletes Docker-generated Jellyfin Web assets.
        env["JELLYFIN_ALLOW_UPSTREAM_AUTO_UPDATE"] = "true"
        env.update({
            "PORT": "12345",
            "DYNO": "web.1",
            "JELLYFIN_COMPAT_ENABLED": "true",
            "UPSTREAM_REPO": "https://github.com/example/legacy-upstream.git",
            "UPSTREAM_BRANCH": "master",
        })
        fake_git = Path(tmp) / ".git"
        fake_git.mkdir()
        (fake_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(UPDATE)],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "skipping the legacy upstream self-update" in output, output
        assert (Path(tmp) / ".git" / "HEAD").is_file(), "Updater unexpectedly deleted the repository metadata"
        assert (Path(tmp) / "log.txt").exists() is False, "Heroku log should not be written into app cwd"
        assert (Path("/tmp/telegram-stremio.log")).exists(), "Expected Heroku log file in writable /tmp"

    # Test the actual process entrypoint. Compatibility mode must skip the
    # updater entirely (irrespective of UPSTREAM_REPO/DB settings), and it must
    # refuse to boot a buildpack image with no generated web assets.
    with tempfile.TemporaryDirectory(prefix="tgst-jellyfin-start-") as tmp:
        work = Path(tmp)
        web_root = work / "Backend" / "fastapi" / "jellyfin_web"
        web_root.mkdir(parents=True)
        (web_root / "index.html").write_text("<!doctype html><title>web</title>", encoding="utf-8")
        fake_bin = work / "bin"
        fake_bin.mkdir()
        calls_file = work / "uv-calls.txt"
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env bash\necho \"$*\" >> \"$UV_CALLS_FILE\"\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "UV_CALLS_FILE": str(calls_file),
            "JELLYFIN_COMPAT_ENABLED": "true",
            "UPSTREAM_REPO": "https://github.com/dwnskhere/dwnsk-TGStremio.git",
            "UPSTREAM_BRANCH": "jellyfin-test",
        })
        result = subprocess.run(
            ["bash", str(ROOT / "start.sh")], cwd=work, env=env,
            capture_output=True, text=True, timeout=10, check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "skipping runtime upstream self-update" in output.lower(), output
        calls = calls_file.read_text(encoding="utf-8").splitlines()
        assert calls == ["run -m Backend"], calls

        # Missing assets must stop startup before Python/Backend is launched.
        (web_root / "index.html").unlink()
        calls_file.unlink()
        result = subprocess.run(
            ["bash", str(ROOT / "start.sh")], cwd=work, env=env,
            capture_output=True, text=True, timeout=10, check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode != 0, "Entrypoint should reject a missing Web frontend"
        assert "FATAL: Jellyfin Web assets are missing" in output, output
        assert not calls_file.exists(), "Backend must not launch when the Web frontend is missing"

    # The real Jellyfin Web distribution is inserted by the Docker build. This
    # fixture validates that the verifier checks index references and nested
    # CSS fonts/imports without needing Docker or network access in CI.
    with tempfile.TemporaryDirectory(prefix="tgst-jellyfin-web-") as tmp:
        web = Path(tmp)
        (web / "index.html").write_text(
            '<html><head><link rel="stylesheet" href="/web/site.css"></head>'
            '<body><script src="/web/app.js"></script></body></html>',
            encoding="utf-8",
        )
        (web / "app.js").write_text("// test bundle", encoding="utf-8")
        (web / "site.css").write_text('@import "nested.css"; body{background:url("bg.svg")}', encoding="utf-8")
        (web / "nested.css").write_text("@font-face{src:url(font.woff2)}", encoding="utf-8")
        (web / "bg.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
        (web / "font.woff2").write_bytes(b"font")
        env = os.environ.copy()
        env["JELLYFIN_WEB_ASSET_ROOT"] = str(web)
        result = subprocess.run(
            [sys.executable, str(ASSET_VERIFIER)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "total local dependencies" in result.stdout, result.stdout

    print("PASS: Jellyfin compatibility mode prevents legacy upstream source reset and preserves Git metadata, even with the old override set true")
    print("PASS: start.sh skips update.py in compatibility mode and checks generated Web assets before boot")
    print("PASS: Heroku updater logs use writable /tmp rather than application directory")
    print("PASS: Web asset verifier checks index, bundles, nested CSS imports, fonts and images")

    deploy_text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    for required_step in (
        "python -m compileall -q Backend tools update.py",
        "python tools/jellyfin_compat_contract_smoke_test.py",
        "python tools/deployment_safety_smoke_test.py",
        "heroku container:push web",
        "heroku container:release web",
    ):
        assert required_step in deploy_text, f"Deployment workflow is missing safety step: {required_step}"

    docker_text = DOCKERFILE.read_text(encoding="utf-8")
    assert "jellyfin/jellyfin:10.10.7 AS jellyfin-web-assets" in docker_text
    assert "jellyfin-web/ /app/Backend/fastapi/jellyfin_web/" in docker_text
    assert "uv sync --locked --no-dev" in docker_text
    print("PASS: Heroku deploy workflow runs compatibility checks before release")
    print("PASS: production Docker image uses pinned Jellyfin Web assets and excludes dev dependencies")


if __name__ == "__main__":
    main()
