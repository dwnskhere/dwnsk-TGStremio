#!/usr/bin/env python3
"""Fail a Docker build if the Jellyfin Web frontend bundle is missing/incomplete.

The default path is the directory populated by the Docker multi-stage COPY.
JELLYFIN_WEB_ASSET_ROOT is an override used only by isolated verifier tests.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "Backend" / "fastapi" / "jellyfin_web"
ROOT = Path(os.environ.get("JELLYFIN_WEB_ASSET_ROOT") or DEFAULT_ROOT).resolve()
INDEX = ROOT / "index.html"


def _resolve_local(reference: str, base_file: Path) -> Path | None:
    reference = reference.strip()
    if not reference or reference.startswith(("#", "data:", "javascript:", "mailto:", "//")):
        return None
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return None
    if raw_path.startswith("/web/"):
        raw_path = raw_path[len("/web/"):]
    elif raw_path.startswith("/"):
        raw_path = raw_path.lstrip("/")
    else:
        raw_path = str((base_file.parent.relative_to(ROOT) / raw_path).as_posix())
    candidate = (ROOT / raw_path).resolve()
    if ROOT != candidate and ROOT not in candidate.parents:
        raise ValueError(f"Frontend asset reference escapes web root: {reference}")
    return candidate


def main() -> int:
    if not INDEX.is_file() or INDEX.stat().st_size < 100:
        print(f"ERROR: Jellyfin Web index is missing or unexpectedly small: {INDEX}", file=sys.stderr)
        return 1

    html = INDEX.read_text(encoding="utf-8", errors="replace")
    html_refs = re.findall(r'''(?:src|href)\s*=\s*["']([^"']+)["']''', html, flags=re.IGNORECASE)
    checked: set[Path] = set()
    css_files: list[Path] = []
    js_count = 0

    try:
        for reference in html_refs:
            resolved = _resolve_local(reference, INDEX)
            if resolved is None:
                continue
            if not resolved.is_file():
                raise FileNotFoundError(f"index.html references missing asset: {reference}")
            checked.add(resolved)
            if resolved.suffix.lower() == ".js":
                js_count += 1
            elif resolved.suffix.lower() == ".css":
                css_files.append(resolved)

        if js_count == 0:
            raise ValueError("Jellyfin Web index does not reference a local JavaScript bundle")
        if not css_files:
            raise ValueError("Jellyfin Web index does not reference a local CSS asset")

        # CSS can reference fonts, images and imported stylesheets that are not
        # visible in index.html. Verify those local dependencies recursively.
        pending = list(css_files)
        visited_css: set[Path] = set()
        while pending:
            css_path = pending.pop()
            if css_path in visited_css:
                continue
            visited_css.add(css_path)
            css_text = css_path.read_text(encoding="utf-8", errors="replace")
            refs = re.findall(r'''url\(\s*["']?([^"')]+)["']?\s*\)''', css_text, flags=re.IGNORECASE)
            refs += re.findall(r'''@import\s+["']([^"']+)["']''', css_text, flags=re.IGNORECASE)
            for reference in refs:
                resolved = _resolve_local(reference, css_path)
                if resolved is None:
                    continue
                if not resolved.is_file():
                    raise FileNotFoundError(f"CSS asset {css_path.relative_to(ROOT)} references missing asset: {reference}")
                checked.add(resolved)
                if resolved.suffix.lower() == ".css":
                    pending.append(resolved)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        "Jellyfin Web assets verified: index.html + "
        f"{js_count} JS references + {len(css_files)} CSS references + "
        f"{len(checked)} total local dependencies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
