"""Unit tests for scrape.py."""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import scrape


# ---------------------------------------------------------------------------
# parse_touched
# ---------------------------------------------------------------------------
class TestParseTouched:
    UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)

    @pytest.mark.parametrize("ts,expected", [
        ("2024-01-15T10:30:00Z", datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)),
        ("2000-12-31T23:59:59Z", datetime(2000, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
        ("2024-06-15T00:00:00Z", datetime(2024, 6, 15, tzinfo=timezone.utc)),
    ])
    def test_valid(self, ts, expected):
        assert scrape.parse_touched(ts) == expected

    @pytest.mark.parametrize("ts", ["", None, "not-a-date", "2024-13-01T00:00:00Z", "garbage123"])
    def test_invalid_returns_min(self, ts):
        assert scrape.parse_touched(ts) == self.UTC_MIN


# ---------------------------------------------------------------------------
# strip_text
# ---------------------------------------------------------------------------
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
        # edge: only whitespace / only tags
        ("   ", ""),
        ("<br><br><br>", ""),
        ("<p>a</p><p>b</p><p>c</p>", "a b c"),
    ])
    def test_strip(self, html, expected):
        assert scrape.strip_text(html) == expected


# ---------------------------------------------------------------------------
# rewrite_html
# ---------------------------------------------------------------------------
class TestRewriteHtml:
    @pytest.fixture(autouse=True)
    def _set_wiki(self):
        scrape.WIKI_NAME = "testwiki"

    # --- image rewriting ---
    @pytest.mark.parametrize("src,local,expect_in", [
        ("https://static.wikia.nocookie.net/img.png", "img.png", "/static/testwiki/images/img.png"),
        ("https://a.com/1.png", "1.png", "/static/testwiki/images/1.png"),
    ])
    def test_replaces_image_urls(self, src, local, expect_in):
        html = f'<img src="{src}">'
        assert expect_in in scrape.rewrite_html(html, {src: local})

    def test_multiple_images(self):
        html = '<img src="https://a.com/1.png"><img src="https://a.com/2.png">'
        result = scrape.rewrite_html(html, {"https://a.com/1.png": "1.png", "https://a.com/2.png": "2.png"})
        assert "/static/testwiki/images/1.png" in result
        assert "/static/testwiki/images/2.png" in result

    def test_empty_image_map_leaves_html_unchanged(self):
        html = '<img src="https://x.com/pic.png">'
        assert scrape.rewrite_html(html, {}) == html

    # --- data-src → src ---
    def test_data_src_promoted(self):
        html = '<img data-src="https://example.com/lazy.png">'
        result = scrape.rewrite_html(html, {})
        assert 'src="https://example.com/lazy.png"' in result
        assert "data-src" not in result

    def test_data_src_with_existing_src(self):
        html = '<img src="placeholder.gif" data-src="https://example.com/real.png">'
        result = scrape.rewrite_html(html, {})
        assert 'src="https://example.com/real.png"' in result

    # --- link rewriting ---
    @pytest.mark.parametrize("href,expected_href", [
        ("https://testwiki.fandom.com/wiki/SomePage", "/wiki/SomePage"),
        ("https://otherwiki.fandom.com/wiki/Page", "/wiki/Page"),
        ("https://testwiki.fandom.com/wiki/Page#Section", "/wiki/Page#Section"),
        ("https://testwiki.fandom.com/wiki/Page?action=edit", "/wiki/Page?action=edit"),
    ])
    def test_fandom_links_rewritten(self, href, expected_href):
        html = f'<a href="{href}">link</a>'
        assert f'href="{expected_href}"' in scrape.rewrite_html(html, {})

    def test_non_fandom_links_preserved(self):
        html = '<a href="https://example.com/page">ext</a>'
        assert 'href="https://example.com/page"' in scrape.rewrite_html(html, {})

    def test_empty_html(self):
        assert scrape.rewrite_html("", {}) == ""

    def test_image_map_and_links_combined(self):
        html = '<a href="https://w.fandom.com/wiki/X"><img src="https://a.com/i.png"></a>'
        result = scrape.rewrite_html(html, {"https://a.com/i.png": "i.png"})
        assert 'href="/wiki/X"' in result
        assert "/static/testwiki/images/i.png" in result


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------
class TestInitDb:
    def test_creates_tables(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"pages", "pages_fts"} <= tables

    def test_idempotent(self, tmp_path):
        p = str(tmp_path / "test.db")
        scrape.init_db(p).close()
        conn = scrape.init_db(p)
        assert "pages" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()

    # --- FTS triggers ---
    def test_fts_insert(self, db):
        db.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'Test','','hello','[]','')")
        db.commit()
        assert db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'hello'").fetchall()

    def test_fts_update(self, db):
        db.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','old','[]','')")
        db.commit()
        db.execute("UPDATE pages SET plaintext='new' WHERE pageid=1")
        db.commit()
        assert not db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'old'").fetchall()
        assert db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'new'").fetchall()

    def test_fts_delete(self, db):
        db.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','gone','[]','')")
        db.commit()
        db.execute("DELETE FROM pages WHERE pageid=1")
        db.commit()
        assert not db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'gone'").fetchall()

    def test_fts_searches_title_and_plaintext(self, db):
        db.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'UniqueTitle','','body text','[]','')")
        db.commit()
        assert db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'UniqueTitle'").fetchall()
        assert db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'body'").fetchall()

    def test_fts_prefix_search(self, db):
        db.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','xylophone','[]','')")
        db.commit()
        assert db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'xylo*'").fetchall()


# ---------------------------------------------------------------------------
# init_wiki
# ---------------------------------------------------------------------------
class TestInitWiki:
    @pytest.mark.parametrize("name,expected_api", [
        ("hollowknight", "https://hollowknight.fandom.com/api.php"),
        ("blue-prince", "https://blue-prince.fandom.com/api.php"),
    ])
    def test_sets_globals(self, name, expected_api):
        scrape.init_wiki(name)
        assert scrape.WIKI_NAME == name
        assert scrape.API == expected_api
        assert name in scrape.SESSION.headers["User-Agent"]


# ---------------------------------------------------------------------------
# api_get
# ---------------------------------------------------------------------------
class TestApiGet:
    @pytest.fixture(autouse=True)
    def _setup(self):
        scrape.API = "https://test.fandom.com/api.php"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_adds_format_json(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={}))
        scrape.api_get({"action": "query"})
        assert mock_get.call_args[1]["params"]["format"] == "json"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_passes_params(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={}))
        scrape.api_get({"action": "parse", "page": "Test"})
        p = mock_get.call_args[1]["params"]
        assert p["action"] == "parse"
        assert p["page"] == "Test"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_raises_on_http_error(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("500")
        mock_get.return_value = resp
        with pytest.raises(Exception, match="500"):
            scrape.api_get({"action": "query"})


# ---------------------------------------------------------------------------
# get_all_pages
# ---------------------------------------------------------------------------
class TestGetAllPages:
    @pytest.fixture(autouse=True)
    def _setup(self):
        scrape.API = "https://test.fandom.com/api.php"

    def _mock_resp(self, data):
        m = MagicMock()
        m.json.return_value = data
        return m

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_single_batch(self, mock_get):
        mock_get.return_value = self._mock_resp({
            "query": {"pages": {
                "1": {"pageid": 1, "title": "A", "touched": "2024-01-01T00:00:00Z"},
                "2": {"pageid": 2, "title": "B", "touched": "2024-01-02T00:00:00Z"},
            }}
        })
        pages = scrape.get_all_pages()
        assert len(pages) == 2
        assert {p["title"] for p in pages} == {"A", "B"}

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_pagination(self, mock_get):
        mock_get.side_effect = [
            self._mock_resp({
                "query": {"pages": {"1": {"pageid": 1, "title": "A", "touched": ""}}},
                "continue": {"apcontinue": "B", "continue": "-||"},
            }),
            self._mock_resp({
                "query": {"pages": {"2": {"pageid": 2, "title": "B", "touched": ""}}},
            }),
        ]
        assert len(scrape.get_all_pages()) == 2

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_empty_wiki(self, mock_get):
        mock_get.return_value = self._mock_resp({"query": {"pages": {}}})
        assert scrape.get_all_pages() == []

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_missing_touched_defaults_empty(self, mock_get):
        mock_get.return_value = self._mock_resp({
            "query": {"pages": {"1": {"pageid": 1, "title": "A"}}}
        })
        assert scrape.get_all_pages()[0]["touched"] == ""


# ---------------------------------------------------------------------------
# get_parsed_page
# ---------------------------------------------------------------------------
class TestGetParsedPage:
    @pytest.fixture(autouse=True)
    def _setup(self):
        scrape.API = "https://test.fandom.com/api.php"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_success(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={
            "parse": {
                "text": {"*": "<p>Content</p>"},
                "categories": [{"*": "Cat1"}],
                "images": ["File1.png"],
            }
        }))
        result = scrape.get_parsed_page("Test")
        assert result["html"] == "<p>Content</p>"
        assert result["categories"] == ["Cat1"]
        assert result["images"] == ["File1.png"]

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_error_returns_none(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={"error": {"code": "missingtitle"}}))
        assert scrape.get_parsed_page("Nonexistent") is None

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_no_categories_or_images(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={
            "parse": {"text": {"*": "<p>bare</p>"}}
        }))
        result = scrape.get_parsed_page("Bare")
        assert result["categories"] == []
        assert result["images"] == []


# ---------------------------------------------------------------------------
# get_image_urls
# ---------------------------------------------------------------------------
class TestGetImageUrls:
    @pytest.fixture(autouse=True)
    def _setup(self):
        scrape.API = "https://test.fandom.com/api.php"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_resolves_urls(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={
            "query": {"pages": {"1": {
                "title": "File:Icon.png",
                "imageinfo": [{"url": "https://static.wikia.nocookie.net/icon.png"}],
            }}}
        }))
        assert scrape.get_image_urls(["Icon.png"])["Icon.png"] == "https://static.wikia.nocookie.net/icon.png"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_batches_at_50(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={"query": {"pages": {}}}))
        scrape.get_image_urls([f"img{i}.png" for i in range(75)])
        assert mock_get.call_count == 2

    def test_empty_list(self):
        assert scrape.get_image_urls([]) == {}

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_skips_missing_imageinfo(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={
            "query": {"pages": {"-1": {"title": "File:Missing.png", "missing": ""}}}
        }))
        assert scrape.get_image_urls(["Missing.png"]) == {}

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_exactly_50_is_one_batch(self, mock_get):
        mock_get.return_value = MagicMock(json=MagicMock(return_value={"query": {"pages": {}}}))
        scrape.get_image_urls([f"img{i}.png" for i in range(50)])
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# download_image
# ---------------------------------------------------------------------------
class TestDownloadImage:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        scrape.WIKI_NAME = "tw"
        self.img_dir = tmp_path / "static" / "tw" / "images"
        self.img_dir.mkdir(parents=True)
        with patch("scrape.os.path.dirname", return_value=str(tmp_path)):
            yield

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_downloads_new_file(self, mock_get):
        mock_get.return_value = MagicMock(iter_content=MagicMock(return_value=[b"imgdata"]))
        result = scrape.download_image("https://x.com/pic.png", "pic.png")
        assert result == "pic.png"
        assert (self.img_dir / "pic.png").read_bytes() == b"imgdata"

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

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_writes_chunked_content(self, mock_get):
        mock_get.return_value = MagicMock(iter_content=MagicMock(return_value=[b"aa", b"bb"]))
        scrape.download_image("https://x.com/multi.png", "multi.png")
        assert (self.img_dir / "multi.png").read_bytes() == b"aabb"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_raises_on_http_error(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("404")
        mock_get.return_value = resp
        with pytest.raises(Exception, match="404"):
            scrape.download_image("https://x.com/bad.png", "bad.png")
