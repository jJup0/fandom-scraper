# Spiritfarer Fandom Wiki Mirror

A local, searchable mirror of the [Spiritfarer Fandom Wiki](https://spiritfarer.fandom.com/) with full-text search, local images, and faithful visual styling.

## Why

Scrapes an entire wiki via the MediaWiki API, stores it in SQLite with FTS5 full-text search, and serves it locally in a browser — looking almost identical to the original.

## What You Get

- **861 pages** scraped via the MediaWiki API (not HTML scraping)
- **~679 images** downloaded locally
- **Full-text search** with ranked results and highlighted snippets
- **Faithful rendering** — Fandom's own CSS, theme colors, background image, infobox styling
- **Internal links work** — wiki links rewritten to point to local routes
- **Resumable scraper** — safe to interrupt and re-run
- **LAN accessible** — browse from any device on your network

## Quick Start

```bash
pip install -r requirements.txt

# Scrape (takes ~10-15 min with rate limiting)
python scrape.py

# Serve
python server.py --host 0.0.0.0 --port 5000
# Open http://localhost:5000
```

## Architecture

```
scrape.py          MediaWiki API scraper → SQLite + local images
server.py          Flask web server with FTS5 search
wiki.db            SQLite database (pages table + FTS5 virtual table)
static/
  fandom-all.css   Fandom's complete CSS bundle (extracted from browser)
  images/          All wiki images, named by MD5 hash
    site-background.png
templates/
  index.html       Search/browse page
  page.html        Wiki page viewer with Fandom styling
```

## How It Works

### Scraping

The scraper uses the MediaWiki API exclusively — no HTML scraping or browser automation.

1. `action=query&list=allpages` — enumerate all content pages (namespace 0)
2. `action=parse&prop=text|categories|images` — get rendered HTML, categories, and image filenames for each page
3. `action=query&prop=imageinfo&iiprop=url` — batch-resolve image filenames to download URLs (50 at a time)
4. Download images to `static/images/` with MD5-hashed filenames
5. Rewrite HTML: replace remote image URLs with local paths, rewrite internal wiki links to local routes
6. Store in SQLite with FTS5 triggers for automatic search index updates

Rate limited to 0.5s between API requests with a polite User-Agent header.

The scraper is idempotent — it checks existing `pageid`s in the DB and skips already-scraped pages. Images are skipped if the file already exists on disk. Safe to Ctrl+C and re-run.

### Search

SQLite FTS5 virtual table with content-sync triggers. The `pages_fts` table indexes `title` and `plaintext` (HTML tags stripped). Search uses FTS5's `MATCH` with `rank` ordering and `snippet()` for highlighted excerpts.

### Serving

Flask app with two routes:
- `/` — lists all pages alphabetically, or searches if `?q=` is provided
- `/wiki/<title>` — renders a wiki page with the stored HTML

## The CSS Journey

Getting the wiki to look right locally was the hardest part. Here's what we learned:

### Attempt 1: Custom dark theme CSS
Wrote our own dark navy blue CSS. Looked okay but nothing like the original wiki.

### Attempt 2: Link to Fandom's CDN stylesheets
Added `<link>` tags pointing to Fandom's `load.php` CSS bundles. The browser loaded them fine, but our custom color overrides were fighting with Fandom's styles. Removing our overrides made it white/unstyled because Fandom's CSS relies on CSS custom properties (variables) that are set by their theme system.

### Attempt 3: Download CSS locally
Cloudflare blocks `curl` and headless browsers from fetching Fandom's CSS directly — you get a "Just a moment..." challenge page instead of CSS. The solution was to extract all CSS from a real browser session:

```js
// Run in browser console on any spiritfarer.fandom.com page:
await Promise.all(
  [...document.querySelectorAll('link[rel="stylesheet"]')].map(async l => {
    const r = await fetch(l.href);
    return {href: l.href, css: await r.text()};
  })
).then(sheets => {
  const blob = new Blob([sheets.map(s => `/* ${s.href} */\n${s.css}`).join('\n\n')], {type:'text/css'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'fandom-all.css'; a.click();
});
```

This gave us a single 290KB CSS file with everything.

### Attempt 4: Theme variables
The CSS loaded but everything was white — no colors. Fandom's CSS uses CSS custom properties like `--theme-page-background-color`, `--theme-accent-color`, etc. These are served by a separate endpoint that is NOT behind Cloudflare:

```
https://spiritfarer.fandom.com/wikia.php?controller=ThemeApi&method=themeVariables
```

This returns a `:root{}` block with all the wiki's theme colors. We prepended this to our local CSS file.

### Attempt 5: JS-equivalent CSS rules
The infobox header was unstyled because Fandom's JavaScript dynamically applies accent colors to elements with classes like `pi-secondary-background` and `pi-border-color`. Since we don't run their JS, we added equivalent CSS rules:

```css
.pi-secondary-background { background: var(--theme-accent-color); color: var(--theme-accent-label-color); }
.pi-border-color { border-color: var(--theme-border-color); }
```

### Attempt 6: Background image
The theme variables CSS contained the background image URL in `--theme-body-background-image-full`. Downloaded it directly (not behind Cloudflare) and referenced it locally. Used `background-size: cover; background-attachment: fixed;` to fill the full page.

### Attempt 7: Page wrapper
Added a `.page-wrapper` div with `background: var(--theme-page-background-color)` to get the cream/beige content area sitting on top of the salmon/pink background — matching the original's layered look.

## Headless Screenshot Trick

For verifying rendering without a real browser window, we used Chrome's headless mode:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --screenshot=/tmp/screenshot.png \
  --window-size=1200,1200 --disable-gpu \
  --virtual-time-budget=5000 \
  "http://localhost:5000/wiki/Air%20Draft"
```

Note: `--headless=new` is Chrome's newer headless mode. `--virtual-time-budget=5000` gives the page 5 seconds of virtual time to render. This works for local CSS but Cloudflare blocks headless browsers from loading external resources.

## Key Fandom/MediaWiki API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `api.php?action=query&list=allpages` | List all pages |
| `api.php?action=query&meta=siteinfo&siprop=statistics` | Site stats |
| `api.php?action=parse&page=TITLE&prop=text\|categories\|images` | Get rendered page HTML |
| `api.php?action=query&titles=File:X&prop=imageinfo&iiprop=url` | Resolve image URLs |
| `api.php?action=parse&page=TITLE&prop=headhtml` | Get full `<head>` HTML (useful for finding CSS links) |
| `wikia.php?controller=ThemeApi&method=themeVariables` | Theme CSS variables (not behind Cloudflare!) |

## Cloudflare Gotchas

Fandom uses Cloudflare protection. Key findings:

- **API endpoints (`api.php`, `wikia.php`)** — accessible from `curl`/`requests`, no Cloudflare challenge
- **Static assets on `static.wikia.nocookie.net`** — accessible from `curl`, images download fine
- **`load.php` CSS bundles** — blocked by Cloudflare, returns JS challenge page
- **Wiki HTML pages** — blocked by Cloudflare for non-browser user agents
- **Headless Chrome** — also blocked by Cloudflare (both old and new headless modes)
- **Real browsers** — pass Cloudflare fine, which is why the `<link>` approach works in practice but not for automated screenshots

## Dependencies

- Python 3
- `requests` — HTTP client for API calls
- `flask` — web server
- SQLite with FTS5 (included in Python's `sqlite3` module)

## License

This is a personal tool for offline wiki browsing. All wiki content belongs to its respective authors under [CC-BY-SA](https://creativecommons.org/licenses/by-sa/3.0/).
