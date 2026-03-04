"""Unit tests for scrape.py."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import scrape

UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _mock_resp(data: dict[str, Any]) -> MagicMock:
    m = MagicMock()
    m.json.return_value = data
    return m


# ---------------------------------------------------------------------------
# parse_touched
# ---------------------------------------------------------------------------
class TestParseTouched:
    @pytest.mark.parametrize(
        "ts,expected",
        [
            (
                "2024-01-15T10:30:00Z",
                datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc),
            ),
            (
                "2000-12-31T23:59:59Z",
                datetime(2000, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            ),
            ("2024-06-15T00:00:00Z", datetime(2024, 6, 15, tzinfo=timezone.utc)),
            ("1970-01-01T00:00:00Z", datetime(1970, 1, 1, tzinfo=timezone.utc)),
        ],
    )
    def test_valid(self, ts: str, expected: datetime) -> None:
        assert scrape.parse_touched(ts) == expected

    @pytest.mark.parametrize(
        "ts",
        [
            "",
            None,
            "not-a-date",
            "2024-13-01T00:00:00Z",
            "garbage123",
            "12345",
            "2024-01-15T25:00:00Z",
            "   ",
            "2024-02-30T00:00:00Z",
        ],
    )
    def test_invalid_returns_min(self, ts: str | None) -> None:
        assert scrape.parse_touched(ts) == UTC_MIN


# ---------------------------------------------------------------------------
# strip_text
# ---------------------------------------------------------------------------
class TestStripText:
    @pytest.mark.parametrize(
        "html,expected",
        [
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
        ],
    )
    def test_strip(self, html: str, expected: str) -> None:
        assert scrape.strip_text(html) == expected


# ---------------------------------------------------------------------------
# rewrite_html
# ---------------------------------------------------------------------------
class TestRewriteHtml:
    @pytest.fixture(autouse=True)
    def _set_wiki(self) -> None:
        scrape._wiki_name = "testwiki"

    @pytest.mark.parametrize(
        "src,local,expected_path",
        [
            (
                "https://static.wikia.nocookie.net/img.png",
                "img.png",
                "/static/testwiki/images/img.png",
            ),
            ("https://a.com/1.png", "1.png", "/static/testwiki/images/1.png"),
        ],
    )
    def test_replaces_image_urls(
        self, src: str, local: str, expected_path: str
    ) -> None:
        html = f'<img src="{src}">'
        assert expected_path in scrape.rewrite_html(html, {src: local})

    def test_multiple_images(self) -> None:
        html = '<img src="https://a.com/1.png"><img src="https://a.com/2.png">'
        result = scrape.rewrite_html(
            html, {"https://a.com/1.png": "1.png", "https://a.com/2.png": "2.png"}
        )
        assert "/static/testwiki/images/1.png" in result
        assert "/static/testwiki/images/2.png" in result

    def test_empty_image_map_leaves_html_unchanged(self) -> None:
        html = '<img src="https://x.com/pic.png">'
        assert scrape.rewrite_html(html, {}) == html

    def test_data_src_promoted(self) -> None:
        html = '<img data-src="https://example.com/lazy.png">'
        result = scrape.rewrite_html(html, {})
        assert 'src="https://example.com/lazy.png"' in result
        assert "data-src" not in result

    def test_data_src_with_existing_src(self) -> None:
        html = '<img src="placeholder.gif" data-src="https://example.com/real.png">'
        result = scrape.rewrite_html(html, {})
        assert 'src="https://example.com/real.png"' in result

    @pytest.mark.parametrize(
        "href,expected_href",
        [
            ("https://testwiki.fandom.com/wiki/SomePage", "/wiki/SomePage"),
            ("https://otherwiki.fandom.com/wiki/Page", "/wiki/Page"),
            ("https://testwiki.fandom.com/wiki/Page#Section", "/wiki/Page#Section"),
            (
                "https://testwiki.fandom.com/wiki/Page?action=edit",
                "/wiki/Page?action=edit",
            ),
            ("https://community.fandom.com/wiki/Help", "/wiki/Help"),
        ],
    )
    def test_fandom_links_rewritten(self, href: str, expected_href: str) -> None:
        html = f'<a href="{href}">link</a>'
        assert f'href="{expected_href}"' in scrape.rewrite_html(html, {})

    def test_non_fandom_links_preserved(self) -> None:
        html = '<a href="https://example.com/page">ext</a>'
        assert 'href="https://example.com/page"' in scrape.rewrite_html(html, {})

    def test_non_wiki_fandom_links_not_rewritten(self) -> None:
        html = '<a href="https://testwiki.fandom.com/f/p/123">forum</a>'
        assert 'href="https://testwiki.fandom.com/f/p/123"' in scrape.rewrite_html(
            html, {}
        )

    def test_empty_html(self) -> None:
        assert scrape.rewrite_html("", {}) == ""

    def test_image_map_and_links_combined(self) -> None:
        html = (
            '<a href="https://w.fandom.com/wiki/X"><img src="https://a.com/i.png"></a>'
        )
        result = scrape.rewrite_html(html, {"https://a.com/i.png": "i.png"})
        assert 'href="/wiki/X"' in result
        assert "/static/testwiki/images/i.png" in result

    def test_duplicate_image_urls_all_replaced(self) -> None:
        html = '<img src="https://a.com/x.png"><img src="https://a.com/x.png">'
        result = scrape.rewrite_html(html, {"https://a.com/x.png": "x.png"})
        assert result.count("/static/testwiki/images/x.png") == 2

    def test_multiple_data_src_tags(self) -> None:
        html = (
            '<img data-src="https://a.com/1.png"><img data-src="https://a.com/2.png">'
        )
        result = scrape.rewrite_html(html, {})
        assert result.count("data-src") == 0
        assert 'src="https://a.com/1.png"' in result
        assert 'src="https://a.com/2.png"' in result


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------
class TestInitDb:
    def test_creates_tables(self, db: sqlite3.Connection) -> None:
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"pages", "pages_fts"} <= tables

    def test_idempotent(self, tmp_path: Path) -> None:
        p = str(tmp_path / "test.db")
        scrape.init_db(p).close()
        conn = scrape.init_db(p)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pages" in tables
        conn.close()

    def test_fts_insert(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'Test','','hello','[]','')"
        )
        db.commit()
        assert db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'hello'"
        ).fetchall()

    def test_fts_update(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','old','[]','')"
        )
        db.commit()
        db.execute("UPDATE pages SET plaintext='new' WHERE pageid=1")
        db.commit()
        assert not db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'old'"
        ).fetchall()
        assert db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'new'"
        ).fetchall()

    def test_fts_delete(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','gone','[]','')"
        )
        db.commit()
        db.execute("DELETE FROM pages WHERE pageid=1")
        db.commit()
        assert not db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'gone'"
        ).fetchall()

    def test_fts_searches_title_and_plaintext(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'UniqueTitle','','body text','[]','')"
        )
        db.commit()
        assert db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'UniqueTitle'"
        ).fetchall()
        assert db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'body'"
        ).fetchall()

    def test_fts_prefix_search(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','xylophone','[]','')"
        )
        db.commit()
        assert db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'xylo*'"
        ).fetchall()

    def test_insert_or_replace_updates_fts(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','old text','[]','')"
        )
        db.commit()
        db.execute(
            "INSERT OR REPLACE INTO pages (pageid,title,html,plaintext,categories,touched) VALUES (1,'T','','new text','[]','')"
        )
        db.commit()
        assert db.execute(
            "SELECT * FROM pages_fts WHERE pages_fts MATCH 'new'"
        ).fetchall()
        assert (
            len(
                db.execute(
                    "SELECT * FROM pages_fts WHERE pages_fts MATCH 'T'"
                ).fetchall()
            )
            == 1
        )


# ---------------------------------------------------------------------------
# init_wiki
# ---------------------------------------------------------------------------
class TestInitWiki:
    @pytest.mark.parametrize(
        "name,expected_api",
        [
            ("hollowknight", "https://hollowknight.fandom.com/api.php"),
            ("blue-prince", "https://blue-prince.fandom.com/api.php"),
            ("stardew-valley", "https://stardew-valley.fandom.com/api.php"),
        ],
    )
    def test_sets_globals(self, name: str, expected_api: str) -> None:
        scrape.init_wiki(name)
        assert scrape._wiki_name == name
        assert scrape._api_url == expected_api
        assert name in str(scrape.SESSION.headers["User-Agent"])


# ---------------------------------------------------------------------------
# verify_wiki_exists
# ---------------------------------------------------------------------------
class TestVerifyWikiExists:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        scrape._api_url = "https://test.fandom.com/api.php"

    @patch.object(scrape.SESSION, "get")
    def test_returns_false_on_network_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = Exception("connection refused")
        assert scrape.verify_wiki_exists() is False

    @patch.object(scrape.SESSION, "get")
    def test_returns_false_on_non_json_response(self, mock_get: MagicMock) -> None:
        resp = MagicMock(status_code=200)
        resp.json.side_effect = ValueError("not json")
        mock_get.return_value = resp
        assert scrape.verify_wiki_exists() is False


# ---------------------------------------------------------------------------
# get_all_pages
# ---------------------------------------------------------------------------
class TestGetAllPages:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        scrape._api_url = "https://test.fandom.com/api.php"

    @pytest.mark.parametrize("num_pages", [2, 3])
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_pagination(self, mock_get: MagicMock, num_pages: int) -> None:
        responses = []
        for i in range(num_pages):
            resp: dict[str, Any] = {
                "query": {
                    "pages": {
                        str(i): {"pageid": i, "title": chr(65 + i), "touched": ""}
                    }
                }
            }
            if i < num_pages - 1:
                resp["continue"] = {"apcontinue": chr(66 + i), "continue": "-||"}
            responses.append(_mock_resp(resp))
        mock_get.side_effect = responses
        assert len(scrape.get_all_pages()) == num_pages


# ---------------------------------------------------------------------------
# get_parsed_page
# ---------------------------------------------------------------------------
class TestGetParsedPage:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        scrape._api_url = "https://test.fandom.com/api.php"

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_error_returns_none(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_resp({"error": {"code": "missingtitle"}})
        assert scrape.get_parsed_page("Nonexistent") is None


# ---------------------------------------------------------------------------
# get_image_urls
# ---------------------------------------------------------------------------
class TestGetImageUrls:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        scrape._api_url = "https://test.fandom.com/api.php"

    @pytest.mark.parametrize(
        "count,expected_calls",
        [
            (50, 1),
            (51, 2),
            (100, 2),
            (101, 3),
        ],
    )
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_batching(
        self, mock_get: MagicMock, count: int, expected_calls: int
    ) -> None:
        mock_get.return_value = _mock_resp({"query": {"pages": {}}})
        scrape.get_image_urls([f"img{i}.png" for i in range(count)])
        assert mock_get.call_count == expected_calls

    def test_empty_list(self) -> None:
        assert scrape.get_image_urls([]) == {}

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_skips_missing_imageinfo(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_resp(
            {"query": {"pages": {"-1": {"title": "File:Missing.png", "missing": ""}}}}
        )
        assert scrape.get_image_urls(["Missing.png"]) == {}


# ---------------------------------------------------------------------------
# download_image
# ---------------------------------------------------------------------------
class TestDownloadImage:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scrape._wiki_name = "tw"
        self.img_dir = tmp_path / "static" / "tw" / "images"
        self.img_dir.mkdir(parents=True)
        monkeypatch.setattr(
            scrape.os.path,
            "dirname",
            lambda f, _orig=os.path.dirname: (
                str(tmp_path) if f == scrape.__file__ else _orig(f)
            ),
        )

    def test_skips_existing(self) -> None:
        (self.img_dir / "existing.png").write_bytes(b"old")
        assert (
            scrape.download_image("https://x.com/existing.png", "existing.png")
            == "existing.png"
        )

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("a/b/c.png", "a_b_c.png"),
            ("my image.png", "my_image.png"),
            ("a\\b.png", "a_b.png"),
            ("a/b c\\d.png", "a_b_c_d.png"),
            ("/leading.png", "_leading.png"),
            ("trailing/.png", "trailing_.png"),
        ],
    )
    def test_sanitizes_filename(self, filename: str, expected: str) -> None:
        (self.img_dir / expected).write_bytes(b"x")
        assert scrape.download_image("https://x.com/x", filename) == expected


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------
class TestMainIntegration:
    """Test the main scrape flow with mocked HTTP."""

    @patch("scrape.verify_wiki_exists", return_value=True)
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_scrape_stores_and_rewrites(
        self, mock_get: MagicMock, mock_verify: MagicMock, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "test.db")
        img_dir = tmp_path / "static" / "mywiki" / "images"
        img_dir.mkdir(parents=True)
        theme_dir = tmp_path / "static" / "mywiki"

        # Mock responses in order: theme, allpages, parse, imageinfo, image download
        theme_resp = MagicMock()
        theme_resp.text = ":root { --color: red; }"

        allpages_resp = _mock_resp(
            {
                "query": {
                    "pages": {
                        "1": {
                            "pageid": 1,
                            "title": "TestPage",
                            "touched": "2024-06-01T00:00:00Z",
                        }
                    }
                }
            }
        )

        parse_resp = _mock_resp(
            {
                "parse": {
                    "text": {
                        "*": '<p>Hello</p><img src="https://static.wikia.nocookie.net/mywiki/pic.png">'
                    },
                    "categories": [{"*": "TestCat"}],
                    "images": ["pic.png"],
                }
            }
        )

        imageinfo_resp = _mock_resp(
            {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:pic.png",
                            "imageinfo": [
                                {
                                    "url": "https://static.wikia.nocookie.net/mywiki/pic.png"
                                }
                            ],
                        }
                    }
                }
            }
        )

        img_download_resp = MagicMock()
        img_download_resp.iter_content = MagicMock(return_value=[b"PNG_DATA"])

        mock_get.side_effect = [
            theme_resp,
            allpages_resp,
            parse_resp,
            imageinfo_resp,
            img_download_resp,
        ]

        # Patch dirname to redirect file writes to tmp_path
        orig_dirname = os.path.dirname

        def fake_dirname(p: str) -> str:
            if p == scrape.__file__:
                return str(tmp_path)
            return orig_dirname(p)

        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()

        # Verify DB has the page with rewritten HTML
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT html, categories FROM pages WHERE title='TestPage'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert "/static/mywiki/images/pic.png" in row[0]
        assert "TestCat" in row[1]

        # Verify image was downloaded
        assert (img_dir / "pic.png").exists()

        # Verify theme was saved
        assert (theme_dir / "theme.css").exists()

    @patch("scrape.verify_wiki_exists", return_value=True)
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_rescrape_skips_existing_pages(
        self, mock_get: MagicMock, mock_verify: MagicMock, tmp_path: Path
    ) -> None:
        """Second run with same touched timestamp should not re-parse pages."""
        db_path = str(tmp_path / "test.db")
        (tmp_path / "static" / "mywiki" / "images").mkdir(parents=True)

        theme_resp = MagicMock(text=":root{}")
        allpages_resp = _mock_resp(
            {
                "query": {
                    "pages": {
                        "1": {
                            "pageid": 1,
                            "title": "P",
                            "touched": "2024-01-01T00:00:00Z",
                        }
                    }
                }
            }
        )
        parse_resp = _mock_resp(
            {"parse": {"text": {"*": "<p>hi</p>"}, "categories": [], "images": []}}
        )

        orig_dirname = os.path.dirname
        fake_dirname = lambda p: (
            str(tmp_path) if p == scrape.__file__ else orig_dirname(p)
        )

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

    @patch("scrape.verify_wiki_exists", return_value=True)
    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_rescrape_updates_when_touched_newer(
        self, mock_get: MagicMock, mock_verify: MagicMock, tmp_path: Path
    ) -> None:
        """Second run with newer touched timestamp should re-parse the page."""
        db_path = str(tmp_path / "test.db")
        (tmp_path / "static" / "mywiki" / "images").mkdir(parents=True)

        theme_resp = MagicMock(text=":root{}")
        allpages_v1 = _mock_resp(
            {
                "query": {
                    "pages": {
                        "1": {
                            "pageid": 1,
                            "title": "P",
                            "touched": "2024-01-01T00:00:00Z",
                        }
                    }
                }
            }
        )
        parse_v1 = _mock_resp(
            {"parse": {"text": {"*": "<p>old</p>"}, "categories": [], "images": []}}
        )

        orig_dirname = os.path.dirname
        fake_dirname = lambda p: (
            str(tmp_path) if p == scrape.__file__ else orig_dirname(p)
        )

        # First run
        mock_get.side_effect = [theme_resp, allpages_v1, parse_v1]
        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()

        # Second run: newer touched → should re-parse
        allpages_v2 = _mock_resp(
            {
                "query": {
                    "pages": {
                        "1": {
                            "pageid": 1,
                            "title": "P",
                            "touched": "2024-06-01T00:00:00Z",
                        }
                    }
                }
            }
        )
        parse_v2 = _mock_resp(
            {"parse": {"text": {"*": "<p>new</p>"}, "categories": [], "images": []}}
        )
        mock_get.reset_mock()
        mock_get.side_effect = [theme_resp, allpages_v2, parse_v2]
        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "mywiki", "--db", db_path]):
                scrape.main()

        conn = sqlite3.connect(db_path)
        html = conn.execute("SELECT html FROM pages WHERE pageid=1").fetchone()[0]
        conn.close()
        assert "new" in html

    @patch.object(scrape, "RATE_LIMIT", 0)
    @patch.object(scrape.SESSION, "get")
    def test_nonexistent_wiki_creates_no_files(
        self, mock_get: MagicMock, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "test.db")
        resp = MagicMock(status_code=404)
        resp.json.return_value = {}
        mock_get.return_value = resp

        orig_dirname = os.path.dirname
        fake_dirname = lambda p: (
            str(tmp_path) if p == scrape.__file__ else orig_dirname(p)
        )

        with patch("scrape.os.path.dirname", side_effect=fake_dirname):
            with patch("sys.argv", ["scrape.py", "fakewiki", "--db", db_path]):
                with pytest.raises(SystemExit):
                    scrape.main()

        assert not os.path.exists(db_path)
        assert not os.path.exists(tmp_path / "static" / "fakewiki")
