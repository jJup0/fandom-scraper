"""Unit tests for server.py Flask routes."""
import json

import pytest


class TestIndex:
    def test_lists_pages(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data

    def test_shows_page_count(self, client):
        resp = client.get("/")
        assert b"8 pages" in resp.data

    def test_search(self, client):
        resp = client.get("/?q=puzzle")
        assert resp.status_code == 200
        assert b"Gorogoa" in resp.data

    def test_search_no_results(self, client):
        resp = client.get("/?q=zzzznonexistent")
        assert resp.status_code == 200
        assert b"0 results" in resp.data

    @pytest.mark.parametrize("q", ["", "   "])
    def test_empty_or_whitespace_shows_browse(self, client, q):
        resp = client.get(f"/?q={q}")
        assert resp.status_code == 200
        assert b"Main Page" in resp.data

    def test_browse_pages_sorted_alphabetically(self, client):
        resp = client.get("/")
        data = resp.data.decode()
        titles = ["Empty Content", "FTS Test", "Gorogoa", "Main Page"]
        positions = [data.index(t) for t in titles]
        assert positions == sorted(positions)

    def test_search_result_count_label(self, client):
        resp = client.get("/?q=puzzle")
        assert b"1 result" in resp.data


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

    def test_redirect_via_underscores(self, client):
        resp = client.get("/wiki/Old_Name")
        assert resp.status_code == 302

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
        import server
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
        resp = client.get("/wiki/Main_Page")
        assert b'class="categories"' not in resp.data

    @pytest.mark.parametrize("path", ["/wiki/", "/wiki/" + "a" * 500])
    def test_edge_case_paths_404(self, client, path):
        assert client.get(path).status_code == 404

    def test_empty_content_page(self, client):
        resp = client.get("/wiki/Empty_Content")
        assert resp.status_code == 200

    def test_page_title_in_html_title(self, client):
        resp = client.get("/wiki/Gorogoa")
        assert b"<title>Gorogoa" in resp.data

    def test_back_link_present(self, client):
        resp = client.get("/wiki/Gorogoa")
        assert b'href="/"' in resp.data


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

    def test_results_have_expected_keys(self, client):
        data = json.loads(client.get("/api/search?q=Welcome").data)
        assert isinstance(data, list)
        for item in data:
            assert "title" in item
            assert "snip" in item

    @pytest.mark.parametrize("q", ["test*", '"quoted"'])
    def test_search_special_chars_handled(self, client, q):
        resp = client.get(f"/api/search?q={q}")
        assert resp.status_code == 200

    @pytest.mark.xfail(reason="BUG: FTS5 special chars ({}, (), :) passed raw cause OperationalError")
    @pytest.mark.parametrize("q", ["{}", "()", "a:b"])
    def test_search_special_chars_crash(self, client, q):
        resp = client.get(f"/api/search?q={q}")
        assert resp.status_code == 200

    def test_case_insensitive(self, client):
        data = json.loads(client.get("/api/search?q=PUZZLE").data)
        assert any(r["title"] == "Gorogoa" for r in data)

    def test_nonexistent_route_404(self, client):
        assert client.get("/nonexistent").status_code == 404
