# automated_general

A single-file scraper that works on any website with no per-site configuration. Give it a URL; it
discovers pages (links and sitemaps), extracts content, contacts and assets, and writes JSON.

## Setup

```bash
cd automated_general
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# optional, for JavaScript-heavy sites:
pip install playwright && playwright install chromium
```

## Usage

```bash
python scraper.py https://example.com                       # crawl, extract, no downloads
python scraper.py https://example.com --assets              # also download images/videos/documents
python scraper.py site1.com site2.org --max-pages 100       # several sites
python scraper.py --urls-file sites.txt --contact you@mail.com
python scraper.py                                           # prompts for URLs
```

| flag | default | meaning |
|---|---|---|
| `-o/--out` | `out` | output root (`out/<host>/`) |
| `--max-pages`, `--max-depth` | 200, 5 | crawl limits per site |
| `--assets`, `--max-asset-mb` | off, 100 | download files (de-duplicated by SHA-256) |
| `--render` | `auto` | `auto` re-fetches near-empty/SPA pages with Playwright; `never`; `always` |
| `--subdomains`, `--same-path` | off | widen to subdomains / restrict to the start URL's directory |
| `--no-sitemap` | off | skip sitemap.xml discovery |
| `--region` | from TLD | default country for phone parsing (`IN`, `US`, ...) |
| `--workers`, `--delay-min/max` | 2, 1.0/2.0 s | concurrency and per-host jittered spacing |
| `--contact` | none | added to the User-Agent so site owners can reach you |

## Output (`out/<host>/`)

* `pages.jsonl` – one record per page: url, canonical, title, description, language, Open Graph,
  headings, main text, word count, tables, forms (never submitted), emails, phones (raw + E.164),
  socials, addresses, JSON-LD, embedded JSON (`__NEXT_DATA__` etc.), images, videos, documents,
  internal/external links, `rendered`, `depth`, `crawled_at`.
* `contacts.json` – de-duplicated emails, phones, socials, addresses with `source_pages`.
* `assets_manifest.json` – with `--assets`: url, `local_path`, kind, sha256, size, content type;
  files are in `assets/<kind>/`. Without it, just the URLs found.
* `errors.json`, `site.json`, `summary.md` – failures, crawl metadata, human summary.

## How it adapts automatically

* Stays on the start host, de-dupes URL variants (trailing slash, `index.php`, tracking params,
  query order), guards against `?query` crawl traps, honours `<base href>`.
* Seeds the crawl from `robots.txt` sitemaps or `/sitemap.xml` (including indexes and `.gz`).
* Main text comes from `<main>`/`<article>`/common content containers with nav, header, footer and
  forms stripped; contacts are still read from the full page, including footers, `mailto:`, Cloudflare
  email protection and `name [at] domain [dot] com`.
* Pages with almost no text plus SPA markers are rendered with Playwright if installed.

## Politeness

Honours robots.txt and Crawl-delay (an unreachable/forbidden robots.txt means "disallow all"),
jittered per-host delay, retries with backoff and `Retry-After`. CAPTCHAs, challenge pages, 429 and
repeated 403s stop the host (`blocked: true`); nothing is bypassed. Only GET is used. Asset downloads
may come from other domains (CDNs) and send the referring page as `Referer`.

Scraped contact data is personal data: use it only for lawful, consented purposes, and respect each
site's terms and copyright.

## Tests

```bash
python -m pytest        # offline, uses a local HTTP server
```
