"""E2E tests using Playwright - actually starts the server and tests in a real browser."""
import os
import subprocess
import sys
import time

import pytest

# Skip all tests if playwright not installed
pytest.importorskip("playwright")

from playwright.sync_api import Page, expect


@pytest.fixture(scope="module")
def server_process(tmp_path_factory):
    """Start the actual server with the gorogoa wiki (must exist)."""
    project_dir = os.path.dirname(os.path.dirname(__file__))
    db_path = os.path.join(project_dir, "gorogoa.db")
    
    if not os.path.exists(db_path):
        pytest.skip("gorogoa.db not found - run 'python scrape.py gorogoa' first")
    
    # Start server on a random-ish port
    port = 5099
    proc = subprocess.Popen(
        [sys.executable, "server.py", "gorogoa", "--no-scrape", "--port", str(port)],
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    
    # Wait for server to be ready
    time.sleep(2)
    
    yield f"http://127.0.0.1:{port}"
    
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def browser_page(server_process):
    """Create a browser page."""
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.base_url = server_process
        yield page
        browser.close()


def test_index_loads(browser_page, server_process):
    browser_page.goto(server_process)
    expect(browser_page).to_have_title("Gorogoa Wiki")
    # Should have a list of pages
    expect(browser_page.locator("a[href*='/wiki/']").first).to_be_visible()


def test_search_works(browser_page, server_process):
    browser_page.goto(server_process)
    browser_page.fill("input[name='q']", "puzzle")
    browser_page.press("input[name='q']", "Enter")
    # Should show search results
    expect(browser_page.locator("body")).to_contain_text("puzzle")


def test_wiki_page_loads(browser_page, server_process):
    browser_page.goto(f"{server_process}/wiki/Gorogoa")
    expect(browser_page.locator(".mw-parser-output")).to_be_visible()


def test_navigation_between_pages(browser_page, server_process):
    browser_page.goto(server_process)
    # Click first wiki link
    browser_page.locator("a[href*='/wiki/']").first.click()
    # Should navigate to a wiki page
    assert "/wiki/" in browser_page.url


def test_static_images_load(browser_page, server_process):
    browser_page.goto(f"{server_process}/wiki/Gorogoa")
    # Check if any images on the page loaded (no broken images)
    images = browser_page.locator("img").all()
    for img in images[:3]:  # Check first 3 images
        # naturalWidth > 0 means image loaded successfully
        assert img.evaluate("el => el.naturalWidth") > 0 or img.get_attribute("src", "").startswith("data:")
