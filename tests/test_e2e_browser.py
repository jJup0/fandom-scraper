"""E2E tests using Playwright - starts the server and tests in a real browser."""
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import expect


@pytest.fixture(scope="module")
def server_url():
    """Start the actual server with the gorogoa wiki (must exist)."""
    project_dir = os.path.dirname(os.path.dirname(__file__))
    db_path = os.path.join(project_dir, "gorogoa.db")

    if not os.path.exists(db_path):
        pytest.skip("gorogoa.db not found - run 'python scrape.py gorogoa' first")

    port = 5099
    proc = subprocess.Popen(
        [sys.executable, "server.py", "gorogoa", "--no-scrape", "--port", str(port)],
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    time.sleep(2)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def page(server_url):
    """Fresh browser page per test — no state leakage."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        yield pg, server_url
        browser.close()


def test_index_loads(page):
    pg, url = page
    pg.goto(url)
    expect(pg).to_have_title("Gorogoa Wiki")
    expect(pg.locator("a[href*='/wiki/']").first).to_be_visible()


def test_search_works(page):
    pg, url = page
    pg.goto(url)
    pg.fill("input[name='q']", "puzzle")
    pg.press("input[name='q']", "Enter")
    expect(pg.locator("body")).to_contain_text("puzzle")


def test_wiki_page_loads(page):
    pg, url = page
    pg.goto(f"{url}/wiki/Gorogoa")
    expect(pg.locator(".wiki-content")).to_be_visible()


def test_navigation_between_pages(page):
    pg, url = page
    pg.goto(url)
    pg.locator("a[href*='/wiki/']").first.click()
    assert "/wiki/" in pg.url


def test_static_images_load(page):
    pg, url = page
    pg.goto(f"{url}/wiki/Gorogoa")
    images = pg.locator("img").all()
    for img in images[:3]:
        assert img.evaluate("el => el.naturalWidth") > 0 or img.get_attribute("src", "").startswith("data:")
