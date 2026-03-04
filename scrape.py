#!/usr/bin/env python3
"""Scrape Spiritfarer Fandom wiki via MediaWiki API into SQLite with FTS5."""
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse

import requests

API = "https://spiritfarer.fandom.com/api.php"
DB_PATH = os.path.join(os.path.dirname(__file__), "wiki.db")
IMG_DIR = os.path.join(os.path.dirname(__file__), "static", "images")
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "SpiritfarerWikiMirror/1.0 (personal offline use; polite)"

RATE_LIMIT = 0.5  # seconds between requests


def api_get(params):
    params["format"] = "json"
    time.sleep(RATE_LIMIT)
    r = SESSION.get(API, params=params)
    r.raise_for_status()
    return r.json()


def get_all_pages():
    """Return list of {pageid, title} for all content pages (ns=0)."""
    pages = []
    params = {"action": "query", "list": "allpages", "aplimit": "50", "apnamespace": "0"}
    while True:
        data = api_get(params)
        pages.extend(data["query"]["allpages"])
        if "continue" not in data:
            break
        params["apcontinue"] = data["continue"]["apcontinue"]
    return pages


def get_parsed_page(title):
    """Return parsed HTML and categories for a page."""
    data = api_get({
        "action": "parse",
        "page": title,
        "prop": "text|categories|images",
        "disableeditsection": "true",
    })
    if "error" in data:
        return None
    p = data["parse"]
    return {
        "html": p["text"]["*"],
        "categories": [c["*"] for c in p.get("categories", [])],
        "images": [img for img in p.get("images", [])],
    }


def get_image_urls(filenames):
    """Batch-resolve image filenames to URLs (up to 50 at a time)."""
    urls = {}
    for i in range(0, len(filenames), 50):
        batch = filenames[i:i + 50]
        titles = "|".join("File:" + f for f in batch)
        data = api_get({
            "action": "query",
            "titles": titles,
            "prop": "imageinfo",
            "iiprop": "url",
        })
        for page in data["query"]["pages"].values():
            if "imageinfo" in page:
                fname = page["title"].replace("File:", "", 1)
                urls[fname] = page["imageinfo"][0]["url"]
    return urls


def download_image(url, filename):
    """Download image to static/images/, return local relative path."""
    ext = os.path.splitext(filename)[1] or ".png"
    safe_name = hashlib.md5(filename.encode()).hexdigest() + ext
    local_path = os.path.join(IMG_DIR, safe_name)
    if os.path.exists(local_path):
        return safe_name
    time.sleep(RATE_LIMIT)
    r = SESSION.get(url, stream=True)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return safe_name


def rewrite_html(html, image_map):
    """Replace fandom image/link URLs with local paths."""
    for orig_url, local_name in image_map.items():
        html = html.replace(orig_url, f"/static/images/{local_name}")
    # Rewrite data-src (lazy loaded images on fandom)
    html = re.sub(r' data-src="([^"]*)"', lambda m: f' src="{m.group(1)}"', html)
    # Rewrite internal wiki links to local routes
    html = re.sub(r'href="https://spiritfarer\.fandom\.com/wiki/([^"]*)"',
                  r'href="/wiki/\1"', html)
    # Replace underscores with spaces in wiki link paths
    html = re.sub(r'href="/wiki/([^"]*)"',
                  lambda m: 'href="/wiki/' + m.group(1).replace('_', ' ') + '"', html)
    return html


def strip_text(html):
    """Rough plaintext extraction for FTS indexing."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            pageid INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            html TEXT NOT NULL,
            plaintext TEXT NOT NULL,
            categories TEXT NOT NULL DEFAULT '[]'
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


def main():
    os.makedirs(IMG_DIR, exist_ok=True)
    conn = init_db()
    c = conn.cursor()

    # Track what we already have
    existing = set(r[0] for r in c.execute("SELECT pageid FROM pages").fetchall())

    pages = get_all_pages()
    print(f"Found {len(pages)} pages, {len(existing)} already scraped")

    all_image_filenames = set()

    for i, page in enumerate(pages):
        if page["pageid"] in existing:
            continue
        print(f"[{i+1}/{len(pages)}] {page['title']}")
        parsed = get_parsed_page(page["title"])
        if not parsed:
            print(f"  SKIP (error)")
            continue
        all_image_filenames.update(parsed["images"])
        c.execute(
            "INSERT OR REPLACE INTO pages (pageid, title, html, plaintext, categories) VALUES (?,?,?,?,?)",
            (page["pageid"], page["title"], parsed["html"],
             strip_text(parsed["html"]), json.dumps(parsed["categories"])),
        )
        if (i + 1) % 20 == 0:
            conn.commit()
            print(f"  committed {i+1} pages")

    conn.commit()

    # Also gather image filenames from already-stored pages
    for row in c.execute("SELECT html FROM pages"):
        for m in re.findall(r'data-image-key="([^"]+)"', row[0]):
            all_image_filenames.add(urllib.parse.unquote(m))

    # Download images
    print(f"\nResolving {len(all_image_filenames)} image URLs...")
    image_urls = get_image_urls(list(all_image_filenames))
    image_map = {}  # remote_url -> local_filename
    for fname, url in image_urls.items():
        print(f"  downloading {fname}")
        try:
            local = download_image(url, fname)
            image_map[url] = local
        except Exception as e:
            print(f"  FAILED: {e}")

    # Rewrite HTML to use local images
    print("\nRewriting image URLs in stored pages...")
    for row in c.execute("SELECT pageid, html FROM pages").fetchall():
        new_html = rewrite_html(row[1], image_map)
        if new_html != row[1]:
            c.execute("UPDATE pages SET html=?, plaintext=? WHERE pageid=?",
                       (new_html, strip_text(new_html), row[0]))
    conn.commit()
    conn.close()
    print("Done!")


if __name__ == "__main__":
    main()
