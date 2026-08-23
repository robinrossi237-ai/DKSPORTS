"""Download Bootstrap, Bootstrap Icons, and Google Fonts for offline use."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
VENDOR_DIR = STATIC_DIR / "vendor"
BOOTSTRAP_DIR = VENDOR_DIR / "bootstrap"
BOOTSTRAP_ICONS_DIR = VENDOR_DIR / "bootstrap-icons"
FONTS_DIR = VENDOR_DIR / "fonts"

BOOTSTRAP_CSS_URL = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
BOOTSTRAP_JS_URL = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
BOOTSTRAP_ICONS_CSS_URL = "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css"
GOOGLE_FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Inter:wght@400;500;600;700;800&"
    "family=Space+Grotesk:wght@500;600;700;800&display=swap"
)
DEFAULT_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _download_text(url: str) -> str:
    resp = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def _download_binary(url: str, dest: Path) -> None:
    _ensure_dir(dest.parent)
    resp = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def _safe_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path) or "asset.bin"
    return re.sub(r"[^A-Za-z0-9._-]", "_", filename)


def _mirror_css_assets(css_text: str, css_url: str, asset_dir: Path, relative_prefix: str) -> str:
    pattern = re.compile(r"url\((['\"]?)([^)\"']+)\1\)")

    def _replace(match: re.Match[str]) -> str:
        raw_url = match.group(2).strip()
        if not raw_url or raw_url.startswith("data:"):
            return match.group(0)
        absolute_url = urljoin(css_url, raw_url)
        filename = _safe_name_from_url(absolute_url)
        dest = asset_dir / filename
        if not dest.exists():
            _download_binary(absolute_url, dest)
        return f"url('{relative_prefix}{filename}')"

    return pattern.sub(_replace, css_text)


def sync_bootstrap() -> None:
    _ensure_dir(BOOTSTRAP_DIR)
    (BOOTSTRAP_DIR / "bootstrap.min.css").write_text(_download_text(BOOTSTRAP_CSS_URL), encoding="utf-8")
    _download_binary(BOOTSTRAP_JS_URL, BOOTSTRAP_DIR / "bootstrap.bundle.min.js")


def sync_bootstrap_icons() -> None:
    _ensure_dir(BOOTSTRAP_ICONS_DIR)
    fonts_dir = BOOTSTRAP_ICONS_DIR / "fonts"
    _ensure_dir(fonts_dir)
    css_text = _download_text(BOOTSTRAP_ICONS_CSS_URL)
    mirrored = _mirror_css_assets(css_text, BOOTSTRAP_ICONS_CSS_URL, fonts_dir, "fonts/")
    (BOOTSTRAP_ICONS_DIR / "bootstrap-icons.css").write_text(mirrored, encoding="utf-8")


def sync_google_fonts() -> None:
    _ensure_dir(FONTS_DIR)
    files_dir = FONTS_DIR / "files"
    _ensure_dir(files_dir)
    css_text = _download_text(GOOGLE_FONTS_CSS_URL)
    mirrored = _mirror_css_assets(css_text, GOOGLE_FONTS_CSS_URL, files_dir, "files/")
    (FONTS_DIR / "google-fonts.css").write_text(mirrored, encoding="utf-8")


def main() -> int:
    sync_bootstrap()
    sync_bootstrap_icons()
    sync_google_fonts()
    print("Offline UI assets synced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
