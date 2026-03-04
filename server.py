#!/usr/bin/env python3
"""Local web server to browse and search the scraped Spiritfarer wiki."""
import json
import os
import sqlite3

from flask import Flask, g, render_template, request

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "wiki.db")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def _search(db, q, limit=100):
    """Search with prefix matching, title matches sorted first."""
    if q and not any(c in q for c in '"*:{}()'):
        fts_q = " ".join(w + "*" for w in q.split())
    else:
        fts_q = q
    rows = db.execute(
        """SELECT p.pageid, p.title,
                  snippet(pages_fts, 1, '<mark>', '</mark>', '...', 40) as snip
           FROM pages_fts JOIN pages p ON p.pageid = pages_fts.rowid
           WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?""",
        (fts_q, limit),
    ).fetchall()
    ql = q.lower()
    return sorted(rows, key=lambda r: (ql not in r["title"].lower(), r["title"].lower() != ql))


@app.route("/")
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        rows = _search(db, q)
        return render_template("index.html", pages=rows, query=q, search=True)
    rows = db.execute("SELECT pageid, title FROM pages ORDER BY title").fetchall()
    return render_template("index.html", pages=rows, query="", search=False)


@app.route("/api/search")
def api_search():
    from flask import jsonify
    db = get_db()
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    rows = _search(db, q, limit=20)
    return jsonify([{"title": r["title"], "snip": r["snip"]} for r in rows])


@app.route("/wiki/<path:title>")
def page(title):
    db = get_db()
    row = db.execute("SELECT * FROM pages WHERE title = ?", (title.replace("_", " "),)).fetchone()
    if not row:
        return "Page not found", 404
    # Follow MediaWiki redirects
    if '<div class="redirectMsg">' in row["html"]:
        import re
        m = re.search(r'href="/wiki/([^"]+)"', row["html"])
        if m:
            from flask import redirect
            return redirect("/wiki/" + m.group(1))
    categories = json.loads(row["categories"])
    return render_template("page.html", page=row, categories=categories)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()
    app.run(host=args.host, port=args.port, debug=True)
