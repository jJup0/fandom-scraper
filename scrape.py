#!/usr/bin/env python3
"""Scrape any Fandom wiki via MediaWiki API into SQLite with FTS5."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, TypedDict

import requests

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S", level=logging.INFO
)
log = logging.getLogger("scrape")


class PageInfo(TypedDict):
    pageid: int
    title: str
    touched: str


class ParsedPage(TypedDict):
    html: str
    categories: list[str]
    images: list[str]


def parse_touched(s: str | None) -> datetime:
    """Parse a MediaWiki touched timestamp to a datetime."""
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


SESSION: requests.Session = requests.Session()
RATE_LIMIT: float = 0.5  # seconds between requests
_wiki_name: str | None = None
_api_url: str | None = None


def _require_wiki_name() -> str:
    assert _wiki_name is not None, "init_wiki() must be called first"
    return _wiki_name


def _require_api_url() -> str:
    assert _api_url is not None, "init_wiki() must be called first"
    return _api_url


def init_wiki(name: str) -> None:
    global _wiki_name, _api_url
    _wiki_name = name
    _api_url = f"https://{name}.fandom.com/api.php"
    SESSION.headers["User-Agent"] = (
        f"FandomWikiMirror/1.0 ({name}; personal offline use; polite)"
    )


def verify_wiki_exists() -> bool:
    """Check that the wiki exists by making a lightweight API call."""
    try:
        r = SESSION.get(
            _require_api_url(),
            params={"action": "query", "meta": "siteinfo", "format": "json"},
        )
        return r.status_code == 200 and "query" in r.json()
    except Exception:
        return False


def api_get(params: dict[str, Any]) -> dict[str, Any]:
    params["format"] = "json"
    time.sleep(RATE_LIMIT)
    r = SESSION.get(_require_api_url(), params=params)
    r.raise_for_status()
    result: dict[str, Any] = r.json()
    return result


def get_all_pages() -> list[PageInfo]:
    """Return list of {pageid, title, touched} for all content pages (ns=0)."""
    pages: list[PageInfo] = []
    params: dict[str, Any] = {
        "action": "query",
        "list": "allpages",
        "aplimit": "500",
        "apnamespace": "0",
        "generator": "allpages",
        "gaplimit": "500",
        "gapnamespace": "0",
        "prop": "info",
    }
    while True:
        data = api_get(params)
        if "query" in data and "pages" in data["query"]:
            for p in data["query"]["pages"].values():
                pages.append(
                    PageInfo(
                        pageid=p["pageid"],
                        title=p["title"],
                        touched=p.get("touched", ""),
                    )
                )
        log.info("enumerated %d pages...", len(pages))
        if "continue" not in data:
            break
        params.update(data["continue"])
    log.info("enumerated %d pages total", len(pages))
    return pages


def get_parsed_page(title: str) -> ParsedPage | None:
    """Return parsed HTML and categories for a page."""
    data = api_get(
        {
            "action": "parse",
            "page": title,
            "prop": "text|categories|images",
            "disableeditsection": "true",
        }
    )
    if "error" in data:
        return None
    p = data["parse"]
    return ParsedPage(
        html=p["text"]["*"],
        categories=[c["*"] for c in p.get("categories", [])],
        images=[img for img in p.get("images", [])],
    )


def get_image_urls(filenames: list[str]) -> dict[str, str]:
    """Batch-resolve image filenames to URLs (up to 50 at a time)."""
    urls: dict[str, str] = {}
    for i in range(0, len(filenames), 50):
        batch = filenames[i : i + 50]
        titles = "|".join("File:" + f for f in batch)
        data = api_get(
            {
                "action": "query",
                "titles": titles,
                "prop": "imageinfo",
                "iiprop": "url",
            }
        )
        for page in data["query"]["pages"].values():
            if "imageinfo" in page:
                fname = page["title"].replace("File:", "", 1)
                urls[fname] = page["imageinfo"][0]["url"]
    return urls


def download_image(url: str, filename: str) -> str:
    """Download image to static/images/, return local relative path."""
    safe_name = filename.replace("/", "_").replace("\\", "_").replace(" ", "_")
    local_path = os.path.join(
        os.path.dirname(__file__), "static", _require_wiki_name(), "images", safe_name
    )
    if os.path.exists(local_path):
        return safe_name
    log.info("downloading %s", filename)
    time.sleep(RATE_LIMIT)
    r = SESSION.get(url, stream=True)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return safe_name


def rewrite_html(html: str, image_map: dict[str, str]) -> str:
    """Replace fandom image/link URLs with local paths."""
    for orig_url, local_name in image_map.items():
        html = html.replace(
            orig_url, f"/static/{_require_wiki_name()}/images/{local_name}"
        )
    # Rewrite data-src (lazy loaded images on fandom)
    html = re.sub(r' data-src="([^"]*)"', lambda m: f' src="{m.group(1)}"', html)
    # Rewrite internal wiki links to local routes
    html = re.sub(
        r'href="https://[^"]*\.fandom\.com/wiki/([^"]*)"', r'href="/wiki/\1"', html
    )
    return html


def strip_text(html: str) -> str:
    """Rough plaintext extraction for FTS indexing."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            pageid INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            html TEXT NOT NULL,
            plaintext TEXT NOT NULL,
            categories TEXT NOT NULL DEFAULT '[]',
            touched TEXT NOT NULL DEFAULT ''
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            title, plaintext, content=pages, content_rowid=pageid
        );
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, plaintext) VALUES (new.pageid, new.title, new.plaintext);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, plaintext) VALUES('delete', old.pageid, old.title, old.plaintext);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, plaintext) VALUES('delete', old.pageid, old.title, old.plaintext);
            INSERT INTO pages_fts(rowid, title, plaintext) VALUES (new.pageid, new.title, new.plaintext);
        END;
    """)
    conn.commit()
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape a Fandom wiki into SQLite")
    parser.add_argument(
        "wiki", help="Wiki subdomain (e.g. spiritfarer, hollowknight, stardewvalley)"
    )
    parser.add_argument("--db", default=None, help="Database path (default: <wiki>.db)")
    args = parser.parse_args()

    init_wiki(args.wiki)

    log.info("Verifying wiki '%s' exists...", args.wiki)
    if not verify_wiki_exists():
        log.error("Wiki '%s' does not exist on Fandom. Aborting.", args.wiki)
        sys.exit(1)

    db_path: str = args.db or os.path.join(os.path.dirname(__file__), f"{args.wiki}.db")
    img_dir = os.path.join(os.path.dirname(__file__), "static", args.wiki, "images")
    os.makedirs(img_dir, exist_ok=True)

    # Download theme variables (not behind Cloudflare)
    theme_url = f"https://{args.wiki}.fandom.com/wikia.php?controller=ThemeApi&method=themeVariables"
    log.info("Downloading theme variables from %s...", theme_url)
    theme_css = SESSION.get(theme_url).text
    theme_path = os.path.join(
        os.path.dirname(__file__), "static", args.wiki, "theme.css"
    )
    os.makedirs(os.path.dirname(theme_path), exist_ok=True)
    with open(theme_path, "w") as f:
        f.write(theme_css)
    log.info("Saved to static/%s/theme.css", args.wiki)

    conn = init_db(db_path)
    c = conn.cursor()

    # Track what we already have
    existing: dict[int, str] = {
        r[0]: r[1] for r in c.execute("SELECT pageid, touched FROM pages").fetchall()
    }

    pages = get_all_pages()
    new = [p for p in pages if p["pageid"] not in existing]
    updated = [
        p
        for p in pages
        if p["pageid"] in existing
        and parse_touched(p["touched"]) > parse_touched(existing[p["pageid"]])
    ]
    stale = new + updated  # new pages first
    log.info(
        "Found %d pages, %d in DB, %d new, %d updated",
        len(pages),
        len(existing),
        len(new),
        len(updated),
    )
    if updated[:3]:
        for p in updated[:3]:
            log.info(
                "  e.g. %s: db=%r api=%r",
                p["title"],
                existing[p["pageid"]],
                p["touched"],
            )

    local_files = set(os.listdir(img_dir))

    for i, page in enumerate(stale):
        log.info("[%d/%d] %s", i + 1, len(stale), page["title"])
        parsed = get_parsed_page(page["title"])
        if not parsed:
            log.warning("SKIP (error): %s", page["title"])
            continue

        # Download this page's images immediately (#6)
        needed = [
            f
            for f in parsed["images"]
            if f.replace("/", "_").replace("\\", "_").replace(" ", "_")
            not in local_files
        ]
        if needed:
            image_urls = get_image_urls(needed)
            image_map: dict[str, str] = {}
            for fname, url in image_urls.items():
                try:
                    local = download_image(url, fname)
                    image_map[url] = local
                    local_files.add(local)
                except Exception as e:
                    log.warning("FAILED %s: %s", fname, e)
            # Rewrite HTML with local image paths immediately (#4)
            html = rewrite_html(parsed["html"], image_map)
        else:
            html = rewrite_html(parsed["html"], {})

        c.execute(
            "INSERT OR REPLACE INTO pages (pageid, title, html, plaintext, categories, touched) VALUES (?,?,?,?,?,?)",
            (
                page["pageid"],
                page["title"],
                html,
                strip_text(html),
                json.dumps(parsed["categories"]),
                page["touched"],
            ),
        )
        if (i + 1) % 20 == 0:
            conn.commit()
            log.info("committed %d pages", i + 1)

    conn.commit()
    conn.close()
    log.info("Done!")


if __name__ == "__main__":
    main()
