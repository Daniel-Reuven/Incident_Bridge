"""
Tests for the public pages (Guide, About, Terms), the site footer, the
screenshots they share with the README, the app version and the LICENSE.

Two kinds of tests:
- served by the app: the pages open WITHOUT logging in, /screenshots/ serves
  docs/screenshots/, /health reports the version from pyproject.toml;
- static checks of the files themselves, so a renamed page, image or section
  breaks a test instead of leaving a dead link: every local link and image in
  frontend/*.html points at a file that exists, every "#section" link points
  at an id on its page, every page loads the footer, and the README's
  screenshots exist.

Run from backend/:  python -m pytest tests/test_public_pages.py -v
"""

import re
from pathlib import Path

import pytest

from app import version as version_module
from app.version import app_version

ROOT = Path(__file__).resolve().parents[2]          # the repository root
FRONTEND = ROOT / "frontend"
SCREENSHOTS = ROOT / "docs" / "screenshots"
PUBLIC_PAGES = ["guide.html", "about.html", "terms.html"]
ALL_PAGES = sorted(p.name for p in FRONTEND.glob("*.html"))

_LINK = re.compile(r'(?:href|src)="([^"]+)"')
_ID = re.compile(r'\bid="([^"]+)"')


def local_links(page: str):
    """The site-relative links and images of a page ("/x.html#y", "/screenshots/a.png", "#z")."""
    text = (FRONTEND / page).read_text(encoding="utf-8")
    return [link for link in _LINK.findall(text) if link.startswith(("/", "#"))]


def ids_of(page: str):
    return set(_ID.findall((FRONTEND / page).read_text(encoding="utf-8")))


def file_for(path: str) -> Path:
    """Where the app serves a site-relative path from (see backend/app/api/app.py)."""
    if path.startswith("/screenshots/"):
        return SCREENSHOTS / path[len("/screenshots/"):]
    if path == "/":
        return FRONTEND / "index.html"
    return FRONTEND / path.lstrip("/")


# --- served by the app ---------------------------------------------------------

@pytest.mark.parametrize("page", PUBLIC_PAGES)
def test_public_pages_open_without_logging_in(app_client, page):
    response = app_client.get(f"/{page}")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert 'src="/js/public-page.js"' in response.text


@pytest.mark.parametrize("name", sorted(p.name for p in SCREENSHOTS.glob("*.png")))
def test_screenshots_are_served_from_docs(app_client, name):
    response = app_client.get(f"/screenshots/{name}")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content == (SCREENSHOTS / name).read_bytes()


def test_health_reports_the_version_from_pyproject(app_client):
    pyproject = (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    expected = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1)
    assert app_client.get("/health").json() == {"status": "ok", "version": expected}
    assert app_version() == expected


def test_the_version_falls_back_to_the_installed_package(monkeypatch, tmp_path):
    """Without pyproject.toml next to the code, the installed metadata is used (or "unknown")."""
    monkeypatch.setattr(version_module, "_PYPROJECT", tmp_path / "missing.toml")
    monkeypatch.setattr(version_module.metadata, "version", lambda name: "9.9.9")
    version_module.app_version.cache_clear()
    try:
        assert app_version() == "9.9.9"

        def not_installed(name):
            raise version_module.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(version_module.metadata, "version", not_installed)
        version_module.app_version.cache_clear()
        assert app_version() == "unknown"
    finally:
        version_module.app_version.cache_clear()


# --- static checks of the files -------------------------------------------------

@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_loads_the_site_footer(page):
    assert '<script type="module" src="/js/footer.js"></script>' in (FRONTEND / page).read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_local_link_and_image_exists(page):
    for link in local_links(page):
        path, _, anchor = link.partition("#")
        if path:
            assert file_for(path).is_file(), f"{page}: {link} points at a missing file"
        if anchor:
            target = path.lstrip("/") or page
            assert anchor in ids_of(target), f"{page}: {link} - no id={anchor!r} on {target}"


def test_the_public_pages_in_site_info_exist():
    """The header and footer list SITE.pages (frontend/js/site-info.js)."""
    text = (FRONTEND / "js" / "site-info.js").read_text(encoding="utf-8")
    pages = re.findall(r'href: "(/[^"]+\.html)"', text)
    assert sorted(p.lstrip("/") for p in pages) == sorted(PUBLIC_PAGES)


def test_the_guide_uses_every_readme_screenshot():
    guide = (FRONTEND / "guide.html").read_text(encoding="utf-8")
    for image in SCREENSHOTS.glob("*.png"):
        assert f"/screenshots/{image.name}" in guide, f"the Guide does not show {image.name}"


def test_the_readme_screenshots_exist():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = set(re.findall(r"\((docs/screenshots/[^)]+)\)", readme))
    assert len(links) == 5
    for link in links:
        assert (ROOT / link).is_file(), link


def test_the_license_is_mit_and_names_the_team():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    team = re.findall(r'\{ github: "([^"]+)" \}', (FRONTEND / "js" / "site-info.js").read_text(encoding="utf-8"))
    assert team, "no team members found in site-info.js"
    for member in team:
        assert member in license_text, f"{member} is on the About page but not in LICENSE"
