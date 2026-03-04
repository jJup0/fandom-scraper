#!/usr/bin/env python3
"""Local web server to browse and search a scraped Fandom wiki."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading

from flask import Flask, Response, g, jsonify, redirect, render_template, request

app: Flask = Flask(__name__)
_db_path: str | None = None
_wiki_name: str | None = None
scraping_in_progress: bool = False


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        assert _db_path is not None, "_db_path must be set before serving"
        g.db = sqlite3.connect(_db_path)
        g.db.row_factory = sqlite3.Row
    return g.db  # type: ignore[no-any-return]


@app.teardown_appcontext
def close_db(exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db:
        db.close()


def _search(db: sqlite3.Connection, q: str, limit: int = 100) -> list[sqlite3.Row]:
    """Search with prefix matching, title matches sorted first."""
    # Strip FTS5 syntax characters that cause OperationalError
    cleaned = re.sub(r"[{}()\:\"*]", " ", q)
    words = cleaned.split()
    if not words:
        return []
    fts_q = " ".join(w + "*" for w in words)
    rows = db.execute(
        """SELECT p.pageid, p.title,
                  snippet(pages_fts, 1, '<mark>', '</mark>', '...', 40) as snip
           FROM pages_fts JOIN pages p ON p.pageid = pages_fts.rowid
           WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?""",
        (fts_q, limit),
    ).fetchall()
    ql = q.lower()
    return sorted(
        rows, key=lambda r: (ql not in r["title"].lower(), r["title"].lower() != ql)
    )


@app.route("/")
def index() -> str:
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        rows = _search(db, q)
        return render_template(
            "index.html",
            pages=rows,
            query=q,
            search=True,
            wiki_name=_wiki_name,
            scraping=scraping_in_progress,
        )
    rows = db.execute("SELECT pageid, title FROM pages ORDER BY title").fetchall()
    return render_template(
        "index.html",
        pages=rows,
        query="",
        search=False,
        wiki_name=_wiki_name,
        scraping=scraping_in_progress,
    )


@app.route("/api/search")
def api_search() -> Response:
    db = get_db()
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    rows = _search(db, q, limit=20)
    return jsonify([{"title": r["title"], "snip": r["snip"]} for r in rows])


@app.route("/wiki/<path:title>")
def page(title: str) -> str | tuple[str, int] | Response:
    db = get_db()
    row = db.execute(
        "SELECT * FROM pages WHERE title = ?", (title.replace("_", " "),)
    ).fetchone()
    if not row:
        return "Page not found", 404
    # Follow MediaWiki redirects
    if '<div class="redirectMsg">' in row["html"]:
        m = re.search(r'href="/wiki/([^"]+)"', row["html"])
        if m:
            return redirect("/wiki/" + m.group(1))
    categories: list[str] = json.loads(row["categories"])
    has_full_css: bool = app.config.get("HAS_FULL_CSS", True)  # type: ignore[assignment]
    wiki_slug: str = app.config.get("WIKI_SLUG", "")  # type: ignore[assignment]
    return render_template(
        "page.html",
        page=row,
        categories=categories,
        has_full_css=has_full_css,
        wiki_slug=wiki_slug,
    )


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )
    log = logging.getLogger("server")

    p = argparse.ArgumentParser()
    p.add_argument("wiki", help="Wiki name (e.g. spiritfarer)")
    p.add_argument("--db", default=None, help="Database path (default: <wiki>.db)")
    p.add_argument("--no-scrape", action="store_true", help="Skip scraping, just serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()

    _db_path = args.db or os.path.join(os.path.dirname(__file__), f"{args.wiki}.db")

    if not args.no_scrape:
        from scrape import init_wiki, verify_wiki_exists

        init_wiki(args.wiki)
        if not verify_wiki_exists():
            log.error("Wiki '%s' does not exist on Fandom.", args.wiki)
            sys.exit(1)

        def _scrape() -> None:
            global scraping_in_progress
            cmd = [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "scrape.py"),
                args.wiki,
            ]
            if args.db:
                cmd += ["--db", args.db]
            subprocess.run(cmd)
            scraping_in_progress = False

        scraping_in_progress = True
        log.info("Scraping %s in background...", args.wiki)
        threading.Thread(target=_scrape, daemon=True).start()

    _wiki_name = args.wiki.replace("-", " ").title()
    css_path = os.path.join(os.path.dirname(__file__), "static", "fandom-all.css")
    has_full_css = os.path.exists(css_path) and os.path.getsize(css_path) > 5000
    if not has_full_css:
        log.warning("Full Fandom CSS not found — using fallback styles.")
        log.warning(
            "For best results, see README for browser CSS extraction instructions."
        )
    app.config["HAS_FULL_CSS"] = has_full_css
    app.config["WIKI_SLUG"] = args.wiki
    app.run(host=args.host, port=args.port, debug=True, use_reloader=False)
