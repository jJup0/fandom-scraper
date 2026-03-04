import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import scrape
import server


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite DB with FTS5."""
    conn = scrape.init_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


# Shared test page data - single source of truth
TEST_PAGES = [
    (1, "Main Page", "<p>Welcome</p>", "Welcome", "[]", ""),
    (2, "Gorogoa", "<p>puzzle game</p>", "puzzle game", json.dumps(["Games"]), "2024-01-01T00:00:00Z"),
    (3, "Old Name", '<div class="redirectMsg"><a href="/wiki/Gorogoa">x</a></div>', "", "[]", ""),
    (4, "Special & Characters", "<p>ampersand test</p>", "ampersand test", "[]", ""),
    (5, "Page With Spaces", "<p>spaced</p>", "spaced", "[]", ""),
    (6, "Über Page", "<p>unicode content</p>", "unicode content", "[]", ""),
    (7, "FTS Test", "<p>xylophone zebra</p>", "xylophone zebra", json.dumps(["Music", "Animals"]), ""),
]


@pytest.fixture
def db_path(tmp_path):
    """DB pre-populated with test pages, returns path string."""
    path = str(tmp_path / "test.db")
    conn = scrape.init_db(path)
    conn.executemany(
        "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (?,?,?,?,?,?)",
        TEST_PAGES,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(db_path):
    """Flask test client wired to the test DB."""
    server.DB_PATH = db_path
    server.WIKI_NAME = "Test Wiki"
    server.app.config["TESTING"] = True
    server.app.config["HAS_FULL_CSS"] = False
    server.app.config["WIKI_SLUG"] = "testwiki"
    with server.app.test_client() as c:
        yield c
