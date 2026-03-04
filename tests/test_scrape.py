"""Unit tests for scrape.py."""
import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import scrape

UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _mock_resp(data):
    m = MagicMock()
    m.json.return_value = data
    return m


# ---------------------------------------------------------------------------
# parse_touched
# ---------------------------------------------------------------------------
class TestParseTouched:
    @pytest.mark.parametrize("ts,expected", [
        ("2024-01-15T10:30:00Z", datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)),
        ("2000-12-31T23:59:59Z", datetime(2000, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
        ("2024-06-15T00:00:00Z", datetime(2024, 6, 15, tzinfo=timezone.utc)),
        ("1970-01-01T00:00:00Z", datetime(1970, 1, 1, tzinfo=timezone.utc)),
    ])
    def test_valid(self, ts, expected):
        assert scrape.parse_touched(ts) == expected

    @pytest.mark.parametrize("ts", [
        "", None, "not-a-date", "2024-13-01T00:00:00Z", "garbage123",
        "12345", "2024-01-15T25:00:00Z", "   ", "2024-02-30T00:00:00Z",
    ])
    def test_invalid_returns_min(self, ts):
        assert scrape.parse_touched(ts) == UTC_MIN


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
        ("   ", ""),
        ("<br><br><br>", ""),
        ("<p>a</p><p>b</p><p>c</p>", "a b c"),
        ("<hr/>text", "text"),
        ("<!-- comment -->visible", "visible"),
        ("<div><div><div>deep</div></div></div>", "deep"),
        ("<p>tab\there</p>", "tab here"),
        ("<p>new\nline</p>", "new line"),
        ("<p>\t\n  mixed  \t\n</p>", "mixed"),
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

    @pytest.mark.parametrize("src,local,expected_path", [
        ("https://static.wikia.nocookie.net/img.png", "img.png", "/static/testwiki/images/img.png"),
        ("https://a.com/1.png", "1.png", "/static/testwiki/images/1.png"),
    ])
    def test_replaces_image_urls(self, src, local, expected_path):
        html = f'<img src="{src}">'
        assert expected_path in scrape.rewrite_html(html, {src: local})

    def test_multiple_images(self):
        html = '<img src="https://a.com/1.png"><img src="https://a.com/2.png">'
        result = scrape.rewrite_html(html, {"https://a.com/1.png": "1.png", "https://a.com/2.png": "2.png"})
        assert "/static/testwiki/images/1.png" in result
        assert "/static/testwiki/images/2.png" in result

    def test_empty_image_map_leaves_html_unchanged(self):
        html = '<img src="https://x.com/pic.png">'
        assert scrape.rewrite_html(html, {}) == html

    def test_data_src_promoted(self):
        html = '<img data-src="https://example.com/lazy.png">'
        result = scrape.rewrite_html(html, {})
        assert 'src="https://example.com/lazy.png"' in result
        assert "data-src" not in result

    def test_data_src_with_existing_src(self):
        html = '<img src="placeholder.gif" data-src="https://example.com/real.png">'
        result = scrape.rewrite_html(html, {})
        assert 'src="https://example.com/real.png"' in result

    @pytest.mark.parametrize("href,expected_href", [
        ("https://testwiki.fandom.com/wiki/SomePage", "/wiki/SomePage"),
        ("https://otherwiki.fandom.com/wiki/Page", "/wiki/Page"),
        ("https://testwiki.fandom.com/wiki/Page#Section", "/wiki/Page#Section"),
        ("https://testwiki.fandom.com/wiki/Page?action=edit", "/wiki/Page?action=edit"),
        ("https://community.fandom.com/wiki/Help", "/wiki/Help"),
    ])
    def test_fandom_links_rewritten(self, href, expected_href):
        html = f'<a href="{href}">link</a>'
        assert f'href="{expected_href}"' in scrape.rewrite_html(html, {})

    def test_non_fandom_links_preserved(self):
        html = '<a href="https://example.com/page">ext</a>'
        assert 'href="https://example.com/page"' in scrape.rewrite_html(html, {})

    def test_non_wiki_fandom_links_not_rewritten(self):
        html = '<a href="https://testwiki.fandom.com/f/p/123">forum</a>'
        assert 'href="https://testwiki.fandom.com/f/p/123"' in scrape.rewrite_html(html, {})

    def test_empty_html(self):
        assert scrape.rewrite_html("", {}) == ""

    def test_image_map_and_links_combined(self):
        html = '<a href="https://w.fandom.com/wiki/X"><img src="https://a.com/i.png"></a>'
        result = scrape.rewrite_html(html, {"https://a.com/i.png": "i.png"})
        assert 'href="/wiki/X"' in result
        assert "/static/testwiki/images/i.png" in result

    def test_duplicate_image_urls_all_replaced(self):
        html = '<img src="https://a.com/x.png"><img src="https://a.com/x.png">'
        result = scrape.rewrite_html(html, {"https://a.com/x.png": "x.png"})
        assert result.count("/static/testwiki/images/x.png") == 2

    def test_multiple_data_src_tags(self):
        html = '<img data-src="https://a.com/1.png"><img data-src="https://a.com/2.png">'
        result = scrape.rewrite_html(html, {})
        assert result.count("data-src") == 0
        assert 'src="https://a.com/1.png"' in result
        assert 'src="https://a.com/2.png"' in result




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
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "pages" in tables
        conn.close()

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

    def test_insert_or_replace_updates_fts(self, db):
        db.execute("INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','old text','[]','')")
        db.commit()
        db.execute("INSERT OR REPLACE INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','new text','[]','')")
        db.commit()
        assert db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'new'").fetchall()
        assert len(db.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'T'").fetchall()) == 1


# ---------------------------------------------------------------------------
# init_wiki
# ---------------------------------------------------------------------------
class TestInitWiki:
    @pytest.mark.parametrize("name,expected_api", [
        ("hollowknight", "https://hollowknight.fandom.com/api.php"),
        ("blue-prince", "https://blue-prince.fandom.com/api.php"),
        ("stardew-valley", "https://stardew-valley.fandom.com/api.php"),
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
        mock_get.return_value = _mock_resp({})
        scrape.api_get({"action": "query"})
        assert mock_get.call_args[1]["params"]["format"] == "json"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_passes_params(self, mock_get):
        mock_get.return_value = _mock_resp({})
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

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_overwrites_caller_format(self, mock_get):
        mock_get.return_value = _mock_resp({})
        scrape.api_get({"action": "query", "format": "xml"})
        assert mock_get.call_args[1]["params"]["format"] == "json"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_returns_parsed_json(self, mock_get):
        mock_get.return_value = _mock_resp({"query": {"pages": {}}})
        result = scrape.api_get({"action": "query"})
        assert result == {"query": {"pages": {}}}


# ---------------------------------------------------------------------------
# get_all_pages
# ---------------------------------------------------------------------------
class TestGetAllPages:
    @pytest.fixture(autouse=True)
    def _setup(self):
        scrape.API = "https://test.fandom.com/api.php"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_single_batch(self, mock_get):
        mock_get.return_value = _mock_resp({
            "query": {"pages": {
                "1": {"pageid": 1, "title": "A", "touched": "2024-01-01T00:00:00Z"},
                "2": {"pageid": 2, "title": "B", "touched": "2024-01-02T00:00:00Z"},
            }}
        })
        pages = scrape.get_all_pages()
        assert len(pages) == 2
        assert {p["title"] for p in pages} == {"A", "B"}

    @pytest.mark.parametrize("num_pages", [2, 3])
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_pagination(self, mock_get, num_pages):
        responses = []
        for i in range(num_pages):
            resp = {"query": {"pages": {str(i): {"pageid": i, "title": chr(65 + i), "touched": ""}}}}
            if i < num_pages - 1:
                resp["continue"] = {"apcontinue": chr(66 + i), "continue": "-||"}
            responses.append(_mock_resp(resp))
        mock_get.side_effect = responses
        assert len(scrape.get_all_pages()) == num_pages

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_empty_wiki(self, mock_get):
        mock_get.return_value = _mock_resp({"query": {"pages": {}}})
        assert scrape.get_all_pages() == []

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_missing_touched_defaults_empty(self, mock_get):
        mock_get.return_value = _mock_resp({
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
        mock_get.return_value = _mock_resp({
            "parse": {
                "text": {"*": "<p>Content</p>"},
                "categories": [{"*": "Cat1"}],
                "images": ["File1.png"],
            }
        })
        result = scrape.get_parsed_page("Test")
        assert result["html"] == "<p>Content</p>"
        assert result["categories"] == ["Cat1"]
        assert result["images"] == ["File1.png"]

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_error_returns_none(self, mock_get):
        mock_get.return_value = _mock_resp({"error": {"code": "missingtitle"}})
        assert scrape.get_parsed_page("Nonexistent") is None

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_no_categories_or_images(self, mock_get):
        mock_get.return_value = _mock_resp({"parse": {"text": {"*": "<p>bare</p>"}}})
        result = scrape.get_parsed_page("Bare")
        assert result["categories"] == []
        assert result["images"] == []

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_multiple_categories(self, mock_get):
        mock_get.return_value = _mock_resp({
            "parse": {
                "text": {"*": "<p>x</p>"},
                "categories": [{"*": "A"}, {"*": "B"}, {"*": "C"}],
                "images": [],
            }
        })
        assert scrape.get_parsed_page("X")["categories"] == ["A", "B", "C"]


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
        mock_get.return_value = _mock_resp({
            "query": {"pages": {"1": {
                "title": "File:Icon.png",
                "imageinfo": [{"url": "https://static.wikia.nocookie.net/icon.png"}],
            }}}
        })
        assert scrape.get_image_urls(["Icon.png"])["Icon.png"] == "https://static.wikia.nocookie.net/icon.png"

    @pytest.mark.parametrize("count,expected_calls", [
        (50, 1), (51, 2), (100, 2), (101, 3),
    ])
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_batching(self, mock_get, count, expected_calls):
        mock_get.return_value = _mock_resp({"query": {"pages": {}}})
        scrape.get_image_urls([f"img{i}.png" for i in range(count)])
        assert mock_get.call_count == expected_calls

    def test_empty_list(self):
        assert scrape.get_image_urls([]) == {}

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_skips_missing_imageinfo(self, mock_get):
        mock_get.return_value = _mock_resp({
            "query": {"pages": {"-1": {"title": "File:Missing.png", "missing": ""}}}
        })
        assert scrape.get_image_urls(["Missing.png"]) == {}


# ---------------------------------------------------------------------------
# download_image
# ---------------------------------------------------------------------------
class TestDownloadImage:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        scrape.WIKI_NAME = "tw"
        self.img_dir = tmp_path / "static" / "tw" / "images"
        self.img_dir.mkdir(parents=True)
        monkeypatch.setattr(scrape.os.path, "dirname",
                            lambda f, _orig=os.path.dirname: str(tmp_path) if f == scrape.__file__ else _orig(f))

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
        ("/leading.png", "_leading.png"),
        ("trailing/.png", "trailing_.png"),
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

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_empty_chunks(self, mock_get):
        mock_get.return_value = MagicMock(iter_content=MagicMock(return_value=[b"", b"data", b""]))
        scrape.download_image("https://x.com/empty.png", "empty.png")
        assert (self.img_dir / "empty.png").read_bytes() == b"data"


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------
class TestMainIntegration:
    """Test the main scrape flow with mocked HTTP."""

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_scrape_stores_and_rewrites(self, mock_get, tmp_path):
        db_path = str(tmp_path / "test.db")
        img_dir = tmp_path / "static" / "mywiki" / "images"
        img_dir.mkdir(parents=True)
        theme_dir = tmp_path / "static" / "mywiki"

        # Mock responses in order: theme, allpages, parse, imageinfo, image download
        theme_resp = MagicMock()
        theme_resp.text = ":root { --color: red; }"

        allpages_resp = _mock_resp({
            "query": {"pages": {"1": {"pageid": 1, "title": "TestPage", "touched": "2024-06-01T00:00:00Z"}}}
        })

        parse_resp = _mock_resp({
            "parse": {
                "text": {"*": '<p>Hello</p><img src="https://static.wikia.nocookie.net/mywiki/pic.png">'},
                "categories": [{"*": "TestCat"}],
                "images": ["pic.png"],
            }
        })

        imageinfo_resp = _mock_resp({
            "query": {"pages": {"1": {
                "title": "File:pic.png",
                "imageinfo": [{"url": "https://static.wikia.nocookie.net/mywiki/pic.png"}],
            }}}
        })

        img_download_resp = MagicMock()
        img_download_resp.iter_content = MagicMock(return_value=[b"PNG_DATA"])

        mock_get.side_effect = [theme_resp, allpages_resp, parse_resp, imageinfo_resp, img_download_resp]

        # Patch dirname to redirect file writes to tmp_path
        orig_dirname = os.path.dirname
        def fake_dirname(p):
            if p == scrape.__file__:
                return str(tmp_path)
            return orig_dirname(p)

        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()

        # Verify DB has the page with rewritten HTML
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT html, categories FROM pages WHERE title='TestPage'").fetchone()
        conn.close()
        assert row is not None
        assert "/static/mywiki/images/pic.png" in row[0]
        assert "TestCat" in row[1]

        # Verify image was downloaded
        assert (img_dir / "pic.png").exists()

        # Verify theme was saved
        assert (theme_dir / "theme.css").exists()

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_rescrape_skips_existing_pages(self, mock_get, tmp_path):
        """Second run with same touched timestamp should not re-parse pages."""
        db_path = str(tmp_path / "test.db")
        (tmp_path / "static" / "mywiki" / "images").mkdir(parents=True)

        theme_resp = MagicMock(text=":root{}")
        allpages_resp = _mock_resp({
            "query": {"pages": {"1": {"pageid": 1, "title": "P", "touched": "2024-01-01T00:00:00Z"}}}
        })
        parse_resp = _mock_resp({"parse": {"text": {"*": "<p>hi</p>"}, "categories": [], "images": []}})

        orig_dirname = os.path.dirname
        fake_dirname = lambda p: str(tmp_path) if p == scrape.__file__ else orig_dirname(p)

        # First run: scrapes the page
        mock_get.side_effect = [theme_resp, allpages_resp, parse_resp]
        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()

        # Second run: same touched → no parse call expected (only theme + allpages)
        mock_get.reset_mock()
        mock_get.side_effect = [theme_resp, allpages_resp]
        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()
        # Only 2 calls: theme download + allpages. No parse call.
        assert mock_get.call_count == 2

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_rescrape_updates_when_touched_newer(self, mock_get, tmp_path):
        """Second run with newer touched timestamp should re-parse the page."""
        db_path = str(tmp_path / "test.db")
        (tmp_path / "static" / "mywiki" / "images").mkdir(parents=True)

        theme_resp = MagicMock(text=":root{}")
        allpages_v1 = _mock_resp({
            "query": {"pages": {"1": {"pageid": 1, "title": "P", "touched": "2024-01-01T00:00:00Z"}}}
        })
        parse_v1 = _mock_resp({"parse": {"text": {"*": "<p>old</p>"}, "categories": [], "images": []}})

        orig_dirname = os.path.dirname
        fake_dirname = lambda p: str(tmp_path) if p == scrape.__file__ else orig_dirname(p)

        # First run
        mock_get.side_effect = [theme_resp, allpages_v1, parse_v1]
        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()

        # Second run: newer touched → should re-parse
        allpages_v2 = _mock_resp({
            "query": {"pages": {"1": {"pageid": 1, "title": "P", "touched": "2024-06-01T00:00:00Z"}}}
        })
        parse_v2 = _mock_resp({"parse": {"text": {"*": "<p>new</p>"}, "categories": [], "images": []}})
        mock_get.reset_mock()
        mock_get.side_effect = [theme_resp, allpages_v2, parse_v2]
        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()

        import sqlite3
        conn = sqlite3.connect(db_path)
        html = conn.execute("SELECT html FROM pages WHERE pageid=1").fetchone()[0]
        conn.close()
        assert "new" in html
