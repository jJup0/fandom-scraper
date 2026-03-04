"""Unit tests for server.py Flask routes."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import server


class TestIndex:
    def test_lists_pages(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data

    def test_shows_page_count(self, client):
        resp = client.get("/")
        assert b"7 pages" in resp.data

    def test_search(self, client):
        resp = client.get("/?q=puzzle")
        assert resp.status_code == 200
        assert b"Gorogoa" in resp.data

    def test_search_no_results(self, client):
        resp = client.get("/?q=zzzznonexistent")
        assert resp.status_code == 200
        assert b"0 results" in resp.data

    @pytest.mark.parametrize("q", ["", "   "])
    def test_search_empty_or_whitespace_shows_browse(self, client, q):
        resp = client.get(f"/?q={q}")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data

    def test_browse_pages_sorted_alphabetically(self, client):
        """Browse mode should list pages in alphabetical order."""
        resp = client.get("/")
        data = resp.data.decode()
        titles = ["FTS Test", "Gorogoa", "Main Page", "Old Name"]
        positions = [data.index(t) for t in titles]
        assert positions == sorted(positions)


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

    def test_multiple_categories(self, client):
        resp = client.get("/wiki/FTS_Test")
        assert b"Music" in resp.data
        assert b"Animals" in resp.data

    def test_css_warning_shown_without_full_css(self, client):
        resp = client.get("/wiki/Gorogoa")
        assert b"fallback CSS" in resp.data

    def test_css_warning_hidden_with_full_css(self, client):
        server.app.config["HAS_FULL_CSS"] = True
        resp = client.get("/wiki/Gorogoa")
        assert b"fallback CSS" not in resp.data

    def test_unicode_page(self, client):
        resp = client.get("/wiki/Über_Page")
        assert resp.status_code == 200
        assert b"unicode content" in resp.data

    def test_special_chars_in_title(self, client):
        resp = client.get("/wiki/Special_&_Characters")
        assert resp.status_code == 200
        assert b"ampersand test" in resp.data

    def test_page_no_categories_has_no_categories_div(self, client):
        """Pages with empty categories list shouldn't render the categories section."""
        resp = client.get("/wiki/Main_Page")
        assert b'class="categories"' not in resp.data

    def test_redirect_does_not_follow(self, client):
        """Redirect pages should 302, not render the redirect HTML."""
        resp = client.get("/wiki/Old_Name")
        assert resp.status_code == 302

    @pytest.mark.parametrize("path,expected_status", [
        ("/wiki/", 404),
        ("/wiki/a" * 500, 404),  # very long title
    ])
    def test_edge_case_paths(self, client, path, expected_status):
        assert client.get(path).status_code == expected_status


class TestApiSearch:
    def test_returns_json(self, client):
        resp = client.get("/api/search?q=puzzle")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data[0]["title"] == "Gorogoa"

    @pytest.mark.parametrize("qs", ["/api/search?q=", "/api/search"])
    def test_empty_or_missing_q(self, client, qs):
        assert json.loads(client.get(qs).data) == []

    def test_prefix_matching(self, client):
        data = json.loads(client.get("/api/search?q=puzz").data)
        assert any(r["title"] == "Gorogoa" for r in data)

    def test_result_has_snip(self, client):
        data = json.loads(client.get("/api/search?q=puzzle").data)
        assert "snip" in data[0]

    def test_title_match_sorted_first(self, client):
        data = json.loads(client.get("/api/search?q=Gorogoa").data)
        assert data[0]["title"] == "Gorogoa"

    def test_unicode_search(self, client):
        resp = client.get("/api/search?q=unicode")
        assert resp.status_code == 200

    def test_search_xylophone(self, client):
        data = json.loads(client.get("/api/search?q=xylophone").data)
        assert any(r["title"] == "FTS Test" for r in data)

    def test_response_content_type_is_json(self, client):
        resp = client.get("/api/search?q=puzzle")
        assert resp.content_type == "application/json"

    def test_results_are_list(self, client):
        data = json.loads(client.get("/api/search?q=Welcome").data)
        assert isinstance(data, list)
        for item in data:
            assert "title" in item
            assert "snip" in item

    @pytest.mark.xfail(reason="BUG: FTS5 special chars ({}, (), :) in raw queries cause OperationalError")
    def test_search_special_chars_no_crash(self, client):
        """FTS special chars like * {} () : shouldn't crash the search."""
        for q in ["test*", "{}", "()", '"quoted"', "a:b"]:
            resp = client.get(f"/api/search?q={q}")
            assert resp.status_code == 200
