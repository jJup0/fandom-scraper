"""Unit tests for scrape.py."""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import scrape


class TestParseTouched:
    @pytest.mark.parametrize("input_val,expected", [
        ("2024-01-15T10:30:00Z", datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)),
        ("2000-12-31T23:59:59Z", datetime(2000, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
        ("2024-06-15T00:00:00Z", datetime(2024, 6, 15, tzinfo=timezone.utc)),
    ])
    def test_valid(self, input_val, expected):
        assert scrape.parse_touched(input_val) == expected

    @pytest.mark.parametrize("input_val", ["", None, "not-a-date", "2024-13-01T00:00:00Z", "garbage123"])
    def test_returns_min_for_invalid(self, input_val):
        assert scrape.parse_touched(input_val) == datetime.min.replace(tzinfo=timezone.utc)


class TestStripText:
    @pytest.mark.parametrize("html,expected", [
        ("<p>Hello <b>world</b></p>", "Hello world"),
        ("<p>  lots   of   space  </p>", "lots of space"),
        ("", ""),
        ("plain text no tags", "plain text no tags"),
        ("<div><p>nested</p></div>", "nested"),
        ("<script>alert('x')</script>visible", "alert('x') visible"),
        ("<img src='x'>", ""),
        ("&lt;not a tag&gt;", "&lt;not a tag&gt;"),
    ])
    def test_strip(self, html, expected):
        assert scrape.strip_text(html) == expected


class TestRewriteHtml:
    @pytest.fixture(autouse=True)
    def set_wiki(self):
        scrape.WIKI_NAME = "testwiki"

    def test_replaces_image_urls(self):
        html = '<img src="https://static.wikia.nocookie.net/img.png">'
        result = scrape.rewrite_html(html, {"https://static.wikia.nocookie.net/img.png": "img.png"})
        assert 'src="/static/testwiki/images/img.png"' in result

    def test_data_src_to_src(self):
        html = '<img data-src="https://example.com/lazy.png">'
        result = scrape.rewrite_html(html, {})
        assert 'src="https://example.com/lazy.png"' in result
        assert "data-src" not in result

    def test_wiki_links(self):
        html = '<a href="https://testwiki.fandom.com/wiki/SomePage">link</a>'
        result = scrape.rewrite_html(html, {})
        assert 'href="/wiki/SomePage"' in result

    def test_multiple_images(self):
        html = '<img src="https://a.com/1.png"><img src="https://a.com/2.png">'
        result = scrape.rewrite_html(html, {"https://a.com/1.png": "1.png", "https://a.com/2.png": "2.png"})
        assert "/static/testwiki/images/1.png" in result
        assert "/static/testwiki/images/2.png" in result

    def test_empty_image_map(self):
        html = '<img src="https://x.com/pic.png">'
        result = scrape.rewrite_html(html, {})
        assert result == html  # unchanged

    def test_preserves_non_fandom_links(self):
        html = '<a href="https://example.com/page">ext</a>'
        result = scrape.rewrite_html(html, {})
        assert 'href="https://example.com/page"' in result

    def test_rewrites_links_from_different_wiki(self):
        html = '<a href="https://otherwiki.fandom.com/wiki/Page">link</a>'
        result = scrape.rewrite_html(html, {})
        assert 'href="/wiki/Page"' in result

    def test_link_with_fragment(self):
        html = '<a href="https://testwiki.fandom.com/wiki/Page#Section">link</a>'
        result = scrape.rewrite_html(html, {})
        assert 'href="/wiki/Page#Section"' in result

    def test_link_with_query_params(self):
        html = '<a href="https://testwiki.fandom.com/wiki/Page?action=edit">link</a>'
        result = scrape.rewrite_html(html, {})
        assert 'href="/wiki/Page?action=edit"' in result


class TestInitDb:
    def test_creates_tables(self, tmp_path):
        conn = scrape.init_db(str(tmp_path / "test.db"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"pages", "pages_fts"} <= tables
        conn.close()

    def test_fts_trigger_insert(self, tmp_path):
        conn = scrape.init_db(str(tmp_path / "test.db"))
        conn.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'Test','<p>hello</p>','hello','[]','')")
        conn.commit()
        assert conn.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'hello'").fetchall()
        conn.close()

    def test_fts_trigger_update(self, tmp_path):
        conn = scrape.init_db(str(tmp_path / "test.db"))
        conn.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','<p>old</p>','old','[]','')")
        conn.commit()
        conn.execute("UPDATE pages SET plaintext='new' WHERE pageid=1")
        conn.commit()
        assert not conn.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'old'").fetchall()
        assert conn.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'new'").fetchall()
        conn.close()

    def test_fts_trigger_delete(self, tmp_path):
        conn = scrape.init_db(str(tmp_path / "test.db"))
        conn.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','<p>x</p>','gone','[]','')")
        conn.commit()
        conn.execute("DELETE FROM pages WHERE pageid=1")
        conn.commit()
        assert not conn.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'gone'").fetchall()
        conn.close()

    def test_idempotent(self, tmp_path):
        """Calling init_db twice on the same path should not error."""
        db = str(tmp_path / "test.db")
        conn1 = scrape.init_db(db)
        conn1.close()
        conn2 = scrape.init_db(db)
        tables = {r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "pages" in tables
        conn2.close()

    def test_fts_searches_title_and_plaintext(self, tmp_path):
        conn = scrape.init_db(str(tmp_path / "test.db"))
        conn.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'Unique Title','<p>body</p>','body text','[]','')")
        conn.commit()
        assert conn.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'Unique'").fetchall()
        assert conn.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'body'").fetchall()
        conn.close()


class TestInitWiki:
    def test_sets_globals(self):
        scrape.init_wiki("hollowknight")
        assert scrape.WIKI_NAME == "hollowknight"
        assert scrape.API == "https://hollowknight.fandom.com/api.php"
        assert "hollowknight" in scrape.SESSION.headers["User-Agent"]

    def test_hyphenated_wiki(self):
        scrape.init_wiki("blue-prince")
        assert scrape.API == "https://blue-prince.fandom.com/api.php"


class TestApiGet:
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_adds_format_json(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"query": {}}
        mock_get.return_value = mock_resp
        scrape.API = "https://test.fandom.com/api.php"
        scrape.api_get({"action": "query"})
        assert mock_get.call_args[1]["params"]["format"] == "json"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_passes_params_through(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp
        scrape.API = "https://test.fandom.com/api.php"
        scrape.api_get({"action": "parse", "page": "Test"})
        params = mock_get.call_args[1]["params"]
        assert params["action"] == "parse"
        assert params["page"] == "Test"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_raises_on_http_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500")
        mock_get.return_value = mock_resp
        scrape.API = "https://test.fandom.com/api.php"
        with pytest.raises(Exception, match="500"):
            scrape.api_get({"action": "query"})


class TestGetAllPages:
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_single_batch(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "query": {"pages": {
                "1": {"pageid": 1, "title": "A", "touched": "2024-01-01T00:00:00Z"},
                "2": {"pageid": 2, "title": "B", "touched": "2024-01-02T00:00:00Z"},
            }}
        }
        mock_get.return_value = mock_resp
        pages = scrape.get_all_pages()
        assert len(pages) == 2
        assert {p["title"] for p in pages} == {"A", "B"}

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_pagination(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        r1 = MagicMock()
        r1.json.return_value = {
            "query": {"pages": {"1": {"pageid": 1, "title": "A", "touched": ""}}},
            "continue": {"apcontinue": "B", "continue": "-||"},
        }
        r2 = MagicMock()
        r2.json.return_value = {
            "query": {"pages": {"2": {"pageid": 2, "title": "B", "touched": ""}}},
        }
        mock_get.side_effect = [r1, r2]
        assert len(scrape.get_all_pages()) == 2

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_empty_wiki(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"query": {"pages": {}}}
        mock_get.return_value = mock_resp
        assert scrape.get_all_pages() == []

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_missing_touched_field(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "query": {"pages": {"1": {"pageid": 1, "title": "A"}}}
        }
        mock_get.return_value = mock_resp
        pages = scrape.get_all_pages()
        assert pages[0]["touched"] == ""


class TestGetParsedPage:
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_success(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "parse": {
                "text": {"*": "<p>Content</p>"},
                "categories": [{"*": "Cat1"}],
                "images": ["File1.png"],
            }
        }
        mock_get.return_value = mock_resp
        result = scrape.get_parsed_page("Test")
        assert result["html"] == "<p>Content</p>"
        assert result["categories"] == ["Cat1"]
        assert result["images"] == ["File1.png"]

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_error_returns_none(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": {"code": "missingtitle"}}
        mock_get.return_value = mock_resp
        assert scrape.get_parsed_page("Nonexistent") is None

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_no_categories_or_images(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "parse": {"text": {"*": "<p>bare</p>"}}
        }
        mock_get.return_value = mock_resp
        result = scrape.get_parsed_page("Bare")
        assert result["categories"] == []
        assert result["images"] == []


class TestGetImageUrls:
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_resolves_urls(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "query": {"pages": {"1": {
                "title": "File:Icon.png",
                "imageinfo": [{"url": "https://static.wikia.nocookie.net/icon.png"}],
            }}}
        }
        mock_get.return_value = mock_resp
        urls = scrape.get_image_urls(["Icon.png"])
        assert urls["Icon.png"] == "https://static.wikia.nocookie.net/icon.png"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_batches_at_50(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"query": {"pages": {}}}
        mock_get.return_value = mock_resp
        scrape.get_image_urls([f"img{i}.png" for i in range(75)])
        assert mock_get.call_count == 2

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_empty_list(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        assert scrape.get_image_urls([]) == {}
        mock_get.assert_not_called()

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_skips_pages_without_imageinfo(self, mock_get):
        scrape.API = "https://test.fandom.com/api.php"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "query": {"pages": {"-1": {"title": "File:Missing.png", "missing": ""}}}
        }
        mock_get.return_value = mock_resp
        assert scrape.get_image_urls(["Missing.png"]) == {}


class TestDownloadImage:
    @pytest.fixture(autouse=True)
    def setup_dirs(self, tmp_path):
        scrape.WIKI_NAME = "tw"
        self.img_dir = tmp_path / "static" / "tw" / "images"
        self.img_dir.mkdir(parents=True)
        self._patch = patch("scrape.os.path.dirname", return_value=str(tmp_path))
        self._patch.start()
        yield
        self._patch.stop()

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_downloads_new_file(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"imgdata"]
        mock_get.return_value = mock_resp
        result = scrape.download_image("https://x.com/pic.png", "pic.png")
        assert result == "pic.png"
        assert (self.img_dir / "pic.png").read_bytes() == b"imgdata"

    @patch.object(scrape, "RATE_LIMIT", 0)
    def test_skips_existing(self):
        (self.img_dir / "existing.png").write_bytes(b"old")
        assert scrape.download_image("https://x.com/existing.png", "existing.png") == "existing.png"

    @pytest.mark.parametrize("filename,expected", [
        ("a/b/c.png", "a_b_c.png"),
        ("my image.png", "my_image.png"),
        ("a\\b.png", "a_b.png"),
        ("a/b c\\d.png", "a_b_c_d.png"),
    ])
    def test_sanitizes_filename(self, filename, expected):
        (self.img_dir / expected).write_bytes(b"x")
        assert scrape.download_image("https://x.com/x", filename) == expected
