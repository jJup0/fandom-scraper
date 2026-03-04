#!/usr/bin/env python3
"""Scrape any Fandom wiki via MediaWiki API into SQLite with FTS5."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse

import requests

from datetime import datetime, timezone


def parse_touched(s):
    """Parse a MediaWiki touched timestamp to a datetime."""
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)

SESSION = requests.Session()
RATE_LIMIT = 0.5  # seconds between requests
WIKI_NAME = None
API = None


def init_wiki(name):
    global WIKI_NAME, API
    WIKI_NAME = name
    API = f"https://{name}.fandom.com/api.php"
    SESSION.headers["User-Agent"] = f"FandomWikiMirror/1.0 ({name}; personal offline use; polite)"


def api_get(params):
    params["format"] = "json"
    time.sleep(RATE_LIMIT)
    r = SESSION.get(API, params=params)
    r.raise_for_status()
    return r.json()


def get_all_pages():
    """Return list of {pageid, title, touched} for all content pages (ns=0)."""
    pages = []
    params = {
        "action": "query", "list": "allpages", "aplimit": "500", "apnamespace": "0",
        "generator": "allpages", "gaplimit": "500", "gapnamespace": "0",
        "prop": "info",
    }
    while True:
        data = api_get(params)
        if "query" in data and "pages" in data["query"]:
            for p in data["query"]["pages"].values():
                pages.append({"pageid": p["pageid"], "title": p["title"], "touched": p.get("touched", "")})
        print(f"  enumerated {len(pages)} pages...", end="\r")
        if "continue" not in data:
            break
        params.update(data["continue"])
    print(f"  enumerated {len(pages)} pages total")
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
    local_path = os.path.join(os.path.dirname(__file__), "static", WIKI_NAME, "images", safe_name)
    if os.path.exists(local_path):
        return safe_name
    print(f"  downloading {filename}")
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
        html = html.replace(orig_url, f"/static/{WIKI_NAME}/images/{local_name}")
    # Rewrite data-src (lazy loaded images on fandom)
    html = re.sub(r' data-src="([^"]*)"', lambda m: f' src="{m.group(1)}"', html)
    # Rewrite internal wiki links to local routes
    html = re.sub(r'href="https://[^"]*\.fandom\.com/wiki/([^"]*)"',
                  r'href="/wiki/\1"', html)
    return html


def strip_text(html):
    """Rough plaintext extraction for FTS indexing."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def init_db(db_path):
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


def main():
    parser = argparse.ArgumentParser(description="Scrape a Fandom wiki into SQLite")
    parser.add_argument("wiki", help="Wiki subdomain (e.g. spiritfarer, hollowknight, stardewvalley)")
    parser.add_argument("--db", default=None, help="Database path (default: <wiki>.db)")
    args = parser.parse_args()

    init_wiki(args.wiki)
    db_path = args.db or os.path.join(os.path.dirname(__file__), f"{args.wiki}.db")
    img_dir = os.path.join(os.path.dirname(__file__), "static", args.wiki, "images")
    os.makedirs(img_dir, exist_ok=True)

    # Download theme variables (not behind Cloudflare)
    theme_url = f"https://{args.wiki}.fandom.com/wikia.php?controller=ThemeApi&method=themeVariables"
    print(f"Downloading theme variables from {theme_url}...")
    theme_css = SESSION.get(theme_url).text
    theme_path = os.path.join(os.path.dirname(__file__), "static", args.wiki, "theme.css")
    os.makedirs(os.path.dirname(theme_path), exist_ok=True)
    with open(theme_path, "w") as f:
        f.write(theme_css)
    print(f"  Saved to static/{args.wiki}/theme.css")

    conn = init_db(db_path)
    c = conn.cursor()

    # Track what we already have
    existing = {r[0]: r[1] for r in c.execute("SELECT pageid, touched FROM pages").fetchall()}

    pages = get_all_pages()
    new = [p for p in pages if p["pageid"] not in existing]
    updated = [p for p in pages if p["pageid"] in existing and parse_touched(p["touched"]) > parse_touched(existing[p["pageid"]])]
    stale = new + updated  # new pages first
    print(f"Found {len(pages)} pages, {len(existing)} in DB, {len(new)} new, {len(updated)} updated")
    if updated[:3]:
        for p in updated[:3]:
            print(f"  e.g. {p['title']}: db={existing[p['pageid']]!r} api={p['touched']!r}")

    all_image_filenames = set()

    for i, page in enumerate(stale):
        print(f"[{i+1}/{len(stale)}] {page['title']}")
        parsed = get_parsed_page(page["title"])
        if not parsed:
            print(f"  SKIP (error)")
            continue
        all_image_filenames.update(parsed["images"])
        c.execute(
            "INSERT OR REPLACE INTO pages (pageid, title, html, plaintext, categories, touched) VALUES (?,?,?,?,?,?)",
            (page["pageid"], page["title"], parsed["html"],
             strip_text(parsed["html"]), json.dumps(parsed["categories"]), page["touched"]),
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
        try:
            local = download_image(url, fname)
            image_map[url] = local
        except Exception as e:
            print(f"  FAILED {fname}: {e}")

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
