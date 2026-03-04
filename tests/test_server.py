"""Unit tests for server.py Flask routes."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import scrape
import server


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = scrape.init_db(path)
    conn.executemany(
        "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (?,?,?,?,?,?)",
        [
            (1, "Main Page", "<p>Welcome</p>", "Welcome", "[]", ""),
            (2, "Gorogoa", "<p>puzzle game</p>", "puzzle game", json.dumps(["Games"]), "2024-01-01T00:00:00Z"),
            (3, "Old Name", '<div class="redirectMsg"><a href="/wiki/Gorogoa">x</a></div>', "", "[]", ""),
            (4, "Special & Characters", "<p>ampersand test</p>", "ampersand test", "[]", ""),
            (5, "Page With Spaces", "<p>spaced</p>", "spaced", "[]", ""),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(db_path):
    server.DB_PATH = db_path
    server.WIKI_NAME = "Test Wiki"
    server.app.config["TESTING"] = True
    server.app.config["HAS_FULL_CSS"] = False
    server.app.config["WIKI_SLUG"] = "testwiki"
    with server.app.test_client() as c:
        yield c


class TestIndex:
    def test_lists_pages(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data

    def test_search(self, client):
        resp = client.get("/?q=puzzle")
        assert resp.status_code == 200
        assert b"Gorogoa" in resp.data

    def test_search_no_results(self, client):
        resp = client.get("/?q=zzzznonexistent")
        assert resp.status_code == 200
        assert b"0 results" in resp.data

    def test_search_empty_query(self, client):
        resp = client.get("/?q=")
        assert resp.status_code == 200
        # Should show all pages (browse mode)
        assert b"Main Page" in resp.data

    def test_search_whitespace_only(self, client):
        resp = client.get("/?q=   ")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data


class TestWikiPage:
    def test_found(self, client):
        resp = client.get("/wiki/Gorogoa")
        assert resp.status_code == 200
        assert b"puzzle game" in resp.data

    def test_not_found(self, client):
        assert client.get("/wiki/DoesNotExist").status_code == 404

    def test_underscore_to_space(self, client):
        resp = client.get("/wiki/Main_Page")
        assert resp.status_code == 200
        assert b"Welcome" in resp.data

    def test_redirect(self, client):
        resp = client.get("/wiki/Old Name")
        assert resp.status_code == 302
        assert "/wiki/Gorogoa" in resp.headers["Location"]

    def test_page_with_spaces_via_underscores(self, client):
        resp = client.get("/wiki/Page_With_Spaces")
        assert resp.status_code == 200
        assert b"spaced" in resp.data

    def test_categories_rendered(self, client):
        resp = client.get("/wiki/Gorogoa")
        assert b"Games" in resp.data

    def test_shows_css_warning_when_no_full_css(self, client):
        resp = client.get("/wiki/Gorogoa")
        assert b"fallback CSS" in resp.data

    def test_hides_css_warning_when_full_css(self, client, db_path):
        server.app.config["HAS_FULL_CSS"] = True
        resp = client.get("/wiki/Gorogoa")
        assert b"fallback CSS" not in resp.data


class TestApiSearch:
    def test_returns_json(self, client):
        resp = client.get("/api/search?q=puzzle")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data[0]["title"] == "Gorogoa"

    def test_empty_query(self, client):
        resp = client.get("/api/search?q=")
        assert json.loads(resp.data) == []

    def test_missing_q_param(self, client):
        resp = client.get("/api/search")
        assert json.loads(resp.data) == []

    def test_prefix_matching(self, client):
        resp = client.get("/api/search?q=puzz")
        data = json.loads(resp.data)
        assert any(r["title"] == "Gorogoa" for r in data)

    def test_result_has_snip(self, client):
        resp = client.get("/api/search?q=puzzle")
        data = json.loads(resp.data)
        assert "snip" in data[0]

    def test_title_match_sorted_first(self, client):
        resp = client.get("/api/search?q=Gorogoa")
        data = json.loads(resp.data)
        assert data[0]["title"] == "Gorogoa"
