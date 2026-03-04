"""Live tests — run scrape.py and server.py as subprocesses, hitting real Fandom."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator

import pytest
import requests

pytestmark = pytest.mark.live
PROJECT_DIR = os.path.dirname(os.path.dirname(__file__))
WIKI = "gorogoa"  # tiny wiki, ~8 pages


@pytest.fixture(scope="module")
def scraped_wiki(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, str]:
    """Scrape a small real wiki into a temp dir."""
    tmp = tmp_path_factory.mktemp("live")
    db_path = str(tmp / f"{WIKI}.db")
    r = subprocess.run(
        [sys.executable, os.path.join(PROJECT_DIR, "scrape.py"), WIKI, "--db", db_path],
        cwd=str(tmp),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"scrape failed:\n{r.stderr}"
    assert os.path.exists(db_path)
    return tmp, db_path


@pytest.fixture(scope="module")
def live_server(scraped_wiki: tuple[Path, str]) -> Generator[str, None, None]:
    """Start server.py serving the scraped wiki."""
    tmp, db_path = scraped_wiki
    port = 5098
    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(PROJECT_DIR, "server.py"),
            WIKI,
            "--no-scrape",
            "--db",
            db_path,
            "--port",
            str(port),
        ],
        cwd=str(tmp),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    time.sleep(1)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


class TestScrape:
    def test_creates_db(self, scraped_wiki: tuple[Path, str]) -> None:
        _, db_path = scraped_wiki
        assert os.path.getsize(db_path) > 0

    def test_pages_scraped(self, scraped_wiki: tuple[Path, str]) -> None:
        import sqlite3

        _, db_path = scraped_wiki
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        conn.close()
        assert count >= 3


class TestServer:
    def test_index(self, live_server: str) -> None:
        r = requests.get(live_server)
        assert r.status_code == 200
        assert "Gorogoa" in r.text

    def test_wiki_page(self, live_server: str) -> None:
        r = requests.get(f"{live_server}/wiki/Gorogoa")
        assert r.status_code == 200
        assert "wiki-content" in r.text

    def test_search_api(self, live_server: str) -> None:
        r = requests.get(f"{live_server}/api/search", params={"q": "puzzle"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) > 0
        assert "title" in data[0]

    def test_404(self, live_server: str) -> None:
        r = requests.get(f"{live_server}/wiki/This_Page_Does_Not_Exist_12345")
        assert r.status_code == 404


class TestNonexistentWiki:
    def test_scrape_exits_with_error(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "bad.db")
        r = subprocess.run(
            [
                sys.executable,
                os.path.join(PROJECT_DIR, "scrape.py"),
                "zzznonexistentwiki999",
                "--db",
                db_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode != 0
        assert not os.path.exists(db_path)

    def test_server_exits_with_error(self) -> None:
        r = subprocess.run(
            [
                sys.executable,
                os.path.join(PROJECT_DIR, "server.py"),
                "zzznonexistentwiki999",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode != 0
        assert (
            "does not exist" in r.stdout.lower() or "does not exist" in r.stderr.lower()
        )
