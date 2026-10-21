#!/usr/bin/env python3
"""Automated, site-agnostic polite scraper.

Give it one or more URLs; it discovers pages (links + sitemaps), extracts text, metadata, structured
data, contacts and assets, and writes JSON into ``out/<host>/``. No per-site configuration is needed.

    python scraper.py https://example.com
    python scraper.py https://example.com --assets --max-pages 100
    python scraper.py --urls-file sites.txt --render auto

Static pages use ``requests`` + BeautifulSoup. Pages that look JavaScript-rendered (almost no text, SPA
markers) are re-fetched with Playwright when it is installed (``--render auto``, the default).
robots.txt and Crawl-delay are honoured; CAPTCHAs / blocks are never bypassed; only GET is issued.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import phonenumbers
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("scraper")

ROBOTS_TOKEN = "AutoScraper"
DEFAULT_UA = "AutoScraper/1.0 (polite research crawler{contact})"

IMAGE_EXTS = {"jpg", "jpeg", "jpe", "png", "gif", "webp", "svg", "ico", "avif", "bmp", "tif", "tiff"}
VIDEO_EXTS = {"mp4", "webm", "mov", "m3u8", "mkv", "avi", "m4v"}
AUDIO_EXTS = {"mp3", "wav", "ogg", "m4a", "aac", "flac"}
DOC_EXTS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "odt", "ods", "rtf"}
ARCHIVE_EXTS = {"zip", "rar", "7z", "gz", "tar", "tgz"}
ASSET_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS | DOC_EXTS | ARCHIVE_EXTS
NON_PAGE_EXTS = ASSET_EXTS | {"css", "js", "mjs", "json", "xml", "map", "rss", "atom", "woff", "woff2", "ttf", "otf", "eot", "exe", "dmg", "apk"}

TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "sessionid", "phpsessid"}
BAD_HREF = ("javascript:", "mailto:", "tel:", "data:", "sms:", "whatsapp:", "blob:", "#")
BLOCK_MARKERS = ("captcha", "cf-chl", "attention required", "just a moment", "access denied", "are you a robot", "unusual traffic")
SPA_MARKERS = ('id="root"', 'id="app"', 'id="__next"', "__NEXT_DATA__", "__NUXT__", "ng-version", "data-reactroot", "window.__INITIAL_STATE__")
SOCIAL_HOSTS = ("facebook.com", "fb.com", "twitter.com", "x.com", "instagram.com", "linkedin.com", "youtube.com",
                "youtu.be", "wa.me", "t.me", "pinterest.com", "tiktok.com", "github.com", "threads.net")
SHARE_HINTS = ("sharer", "share?", "/share", "intent/tweet", "shareArticle", "watch?", "/embed/")
TLD_REGION = {"in": "IN", "uk": "GB", "au": "AU", "ca": "CA", "de": "DE", "fr": "FR", "us": "US", "nz": "NZ",
              "sg": "SG", "ae": "AE", "za": "ZA", "ie": "IE", "es": "ES", "it": "IT", "nl": "NL", "br": "BR"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- URL helpers


def quote_url(url: str) -> str:
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme, p.netloc, quote(p.path, safe="/%+:@!$&'()*,;="), quote(p.query, safe="=&%+/,:;"), ""))


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def path_ext(url: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def resolve_href(href: str | None, base: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href in ("<>", "#") or href.lower().startswith(BAD_HREF):
        return None
    try:
        absolute = urljoin(base, href)
    except ValueError:
        return None
    p = urlsplit(absolute)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS])
    return quote_url(urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", query, "")))


def url_key(url: str) -> str:
    """De-dupe key: ignores scheme, www, trailing slash, index files and query order."""
    p = urlsplit(url)
    path = re.sub(r"/{2,}", "/", p.path)
    path = re.sub(r"/index\.(php|html?|aspx?)$", "/", path, flags=re.IGNORECASE)
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(p.query, keep_blank_values=True)))
    return f"{host_of(url)}{path or '/'}" + (f"?{query}" if query else "")


def asset_kind(url: str, content_type: str = "") -> str:
    ext = path_ext(url)
    for kind, exts in (("images", IMAGE_EXTS), ("videos", VIDEO_EXTS), ("audio", AUDIO_EXTS), ("documents", DOC_EXTS)):
        if ext in exts:
            return kind
    ct = content_type.lower()
    for kind, prefix in (("images", "image/"), ("videos", "video/"), ("audio", "audio/")):
        if ct.startswith(prefix):
            return kind
    if "pdf" in ct or "officedocument" in ct or "msword" in ct:
        return "documents"
    return "other"


def sanitize_filename(name: str, max_len: int = 150) -> str:
    name = re.sub(r"\s+", "_", unquote(name).strip())
    name = re.sub(r"[^\w.\-+()]", "", name).strip("._") or "file"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        name = stem[: max_len - len(ext) - 1] + "." + ext if dot and len(ext) <= 8 else name[:max_len]
    return name


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=list)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- fetching


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{url}: {reason}")
        self.url, self.reason, self.status = url, reason, status


class BlockedError(FetchError):
    """The server is blocking us (CAPTCHA / 429 / repeated 403). We stop crawling that host."""


class Fetcher:
    def __init__(self, user_agent: str, timeout: float, retries: int, delay_min: float, delay_max: float):
        self.user_agent, self.timeout, self.retries = user_agent, timeout, max(1, retries)
        self.delay_min, self.delay_max = delay_min, delay_max
        self.crawl_delay: dict[str, float] = {}
        self.blocked: set[str] = set()
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self._denied = 0

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": self.user_agent, "Accept-Language": "en;q=0.9,*;q=0.5",
                              "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            self._local.s = s
        return s

    def _wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(host, 0.0))
            gap = max(random.uniform(self.delay_min, self.delay_max), self.crawl_delay.get(host, 0.0))
            self._next[host] = start + gap
        if start > now:
            time.sleep(start - now)

    def get(self, url: str, *, stream: bool = False, referer: str | None = None) -> requests.Response:
        url = quote_url(url)
        host = host_of(url)
        if host in self.blocked:
            raise BlockedError(url, "host previously blocked; skipped")
        headers = {"Referer": referer} if referer else None
        last = "unknown error"
        for attempt in range(1, self.retries + 1):
            self._wait(host)
            try:
                resp = self._session().get(url, timeout=self.timeout, stream=stream, headers=headers)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = f"{type(exc).__name__}: {exc}"[:200]
            except requests.RequestException as exc:
                raise FetchError(url, f"{type(exc).__name__}: {exc}"[:200]) from exc
            else:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {resp.status_code}"
                    sample = "" if stream else resp.text[:2000].lower()
                    if resp.status_code != 429 and any(m in sample for m in BLOCK_MARKERS):
                        self.blocked.add(host)
                        raise BlockedError(url, f"challenge page (HTTP {resp.status_code})", resp.status_code)
                    retry_after = resp.headers.get("Retry-After", "")
                    resp.close()
                    if attempt == self.retries:
                        if resp.status_code == 429:
                            self.blocked.add(host)
                            raise BlockedError(url, "HTTP 429 after retries", 429)
                        raise FetchError(url, f"{last} after retries", resp.status_code)
                    time.sleep(min(float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0, 120))
                else:
                    self._note_status(url, resp, stream)
                    return resp
            if attempt == self.retries:
                raise FetchError(url, f"{last} after retries")
            time.sleep(min(2 ** attempt, 30))
        raise FetchError(url, last)

    def _note_status(self, url: str, resp: requests.Response, stream: bool) -> None:
        if resp.status_code == 403 and not stream:
            sample = resp.text[:2000].lower()
            with self._lock:
                self._denied += 1
                denied = self._denied
            if any(m in sample for m in BLOCK_MARKERS) or denied >= 5:
                self.blocked.add(host_of(url))
                raise BlockedError(url, "blocked by server (HTTP 403)", 403)
        elif resp.status_code < 400 and not stream:
            self._denied = 0


class Renderer:
    """Optional Playwright fallback for JavaScript-rendered pages. One browser, serialised use."""

    def __init__(self, user_agent: str, timeout: float):
        self.user_agent, self.timeout = user_agent, timeout
        self._lock = threading.Lock()
        self._pw = self._browser = None
        self._unavailable = False
        self.count = 0

    @property
    def available(self) -> bool:
        if self._unavailable:
            return False
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except ImportError:
            self._unavailable = True
            log.warning("Page looks JavaScript-rendered but Playwright is not installed "
                        "(pip install playwright && playwright install chromium); using static HTML")
            return False

    def render(self, url: str) -> str | None:
        if not self.available:
            return None
        with self._lock:
            try:
                if self._browser is None:
                    from playwright.sync_api import sync_playwright
                    self._pw = sync_playwright().start()
                    self._browser = self._pw.chromium.launch()
                ctx = self._browser.new_context(user_agent=self.user_agent)
                page = ctx.new_page()
                page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("font", "media") else r.continue_())
                try:
                    page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000 * 2)
                    page.evaluate("async()=>{for(let y=0;y<document.body.scrollHeight;y+=800){window.scrollTo(0,y);"
                                  "await new Promise(r=>setTimeout(r,120));}}")
                    page.wait_for_timeout(500)
                    html = page.content()
                finally:
                    ctx.close()
                self.count += 1
                return html
            except Exception as exc:  # noqa: BLE001 - rendering is best-effort
                log.warning("render failed for %s: %s", url, str(exc)[:200])
                self._unavailable = "executable" in str(exc).lower()
                return None

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("renderer close failed: %s", exc)


# --------------------------------------------------------------------------- robots + sitemaps


class Robots:
    def __init__(self, fetcher: Fetcher, origin: str):
        self.origin, self.fetcher = origin, fetcher
        self.parser = urllib.robotparser.RobotFileParser()
        self.policy = "allow_all"
        self.sitemaps: list[str] = []

    def load(self) -> None:
        url = f"{self.origin}/robots.txt"
        try:
            resp = self.fetcher.get(url)
        except FetchError as exc:
            self.policy = "disallow_all"
            log.error("robots.txt unreachable (%s): treating site as disallowed", exc.reason)
            return
        if resp.status_code == 404 or resp.status_code == 410:
            return
        if resp.status_code >= 400:
            self.policy = "disallow_all"
            return
        self.policy = "robots.txt"
        self.parser.parse(resp.text.splitlines())
        delay = self.parser.crawl_delay(ROBOTS_TOKEN) or self.parser.crawl_delay("*")
        if delay:
            self.fetcher.crawl_delay[host_of(self.origin)] = float(delay)
        self.sitemaps = list(self.parser.site_maps() or [])

    def allowed(self, url: str) -> bool:
        if self.policy == "disallow_all":
            return False
        if self.policy == "allow_all":
            return True
        return self.parser.can_fetch(ROBOTS_TOKEN, url)


def fetch_sitemap_urls(fetcher: Fetcher, robots: Robots, origin: str, limit: int) -> list[str]:
    queue = robots.sitemaps or [f"{origin}/sitemap.xml"]
    seen, urls = set(), []
    while queue and len(urls) < limit and len(seen) < 50:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            resp = fetcher.get(sm)
            if resp.status_code != 200:
                continue
            raw = resp.content
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            soup = BeautifulSoup(raw, "xml")
        except (FetchError, OSError, EOFError):
            continue
        for loc in soup.find_all("loc"):
            target = clean(loc.get_text())
            if soup.find("sitemapindex") is not None or path_ext(target) in ("xml", "gz"):
                queue.append(target)
            else:
                urls.append(target)
    return urls[:limit]


# --------------------------------------------------------------------------- extraction

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_AT_RE = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.IGNORECASE)
_DOT_RE = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.IGNORECASE)
BOILERPLATE_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "nav", "footer", "header", "aside", "form"]


def extract_emails(text: str) -> list[str]:
    text = _DOT_RE.sub(".", _AT_RE.sub("@", text))
    out: list[str] = []
    for m in EMAIL_RE.findall(text):
        email = m.strip(".").lower()
        if email.rsplit(".", 1)[-1] in ASSET_EXTS | {"js", "css"}:  # logo@2x.png
            continue
        if email not in out:
            out.append(email)
    return out


def decode_cfemail(encoded: str) -> str | None:
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except (ValueError, IndexError):
        return None


def region_for(url: str, override: str | None) -> str | None:
    return override.upper() if override else TLD_REGION.get(urlsplit(url).netloc.lower().rsplit(".", 1)[-1])


def extract_phones(text: str, region: str | None) -> list[dict]:
    """Numbers in visible text. Without a region only +country-code numbers are recognised."""
    out: dict[str, str] = {}
    try:
        for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
            e164 = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
            out.setdefault(e164, clean(m.raw_string))
    except Exception:  # noqa: BLE001 - malformed region etc.
        return []
    return [{"raw": raw, "e164": e164} for e164, raw in out.items()]


def is_social(url: str) -> bool:
    host = host_of(url)
    if not any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
        return False
    return urlsplit(url).path.strip("/") != "" and not any(h in url for h in SHARE_HINTS)


def pick_main(soup: BeautifulSoup):
    for sel in ("main", "article", "[role=main]", "#content", "#main", ".content", ".main-content", "#main-content"):
        node = soup.select_one(sel)
        if node and len(clean(node.get_text(" "))) > 200:
            return node
    return soup.body or soup


def biggest_srcset(srcset: str) -> str | None:
    best, best_w = None, -1.0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0.0
        if len(bits) > 1 and bits[1][:-1].replace(".", "", 1).isdigit():
            w = float(bits[1][:-1])
        if w > best_w:
            best, best_w = bits[0], w
    return best


def extract_page(html: str, url: str, region: str | None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    base_tag = soup.find("base", href=True)
    base = urljoin(url, base_tag["href"]) if base_tag else url
    site_host = host_of(url)

    def meta(*names: str) -> str | None:
        for n in names:
            tag = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
            if tag and tag.get("content"):
                return clean(tag["content"])
        return None

    canonical = soup.find("link", rel="canonical")
    page: dict = {
        "url": url,
        "canonical_url": resolve_href(canonical.get("href"), base) if canonical else None,
        "title": clean(soup.title.get_text()) if soup.title else None,
        "description": meta("description", "og:description"),
        "language": (soup.html.get("lang") if soup.html else None),
        "og": {t["property"][3:]: t.get("content") for t in soup.find_all("meta", property=re.compile(r"^og:")) if t.get("content")},
        "keywords": [k.strip() for k in (meta("keywords") or "").split(",") if k.strip()],
    }

    # Structured data first (it lives in <script>, which is stripped below).
    json_ld, embedded = [], {}
    for tag in soup.find_all("script"):
        stype, body = (tag.get("type") or "").lower(), tag.string or tag.get_text()
        if not body or not body.strip():
            continue
        if stype == "application/ld+json":
            try:
                json_ld.append(json.loads(body))
            except ValueError:
                pass
        elif tag.get("id") == "__NEXT_DATA__" or stype == "application/json" and len(body) < 500_000:
            try:
                embedded[tag.get("id") or f"json_{len(embedded)}"] = json.loads(body)
            except ValueError:
                pass
    page["json_ld"], page["embedded_json"] = json_ld, embedded

    # Links / assets (before boilerplate is removed so navigation links are still discovered).
    internal, external, images, videos, documents, other_assets = [], [], [], [], [], []
    socials: list[str] = []
    seen_links: set[str] = set()
    emails: list[str] = []

    def add_email(e: str) -> None:
        e = e.lower()
        if e not in emails:
            emails.append(e)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            for e in extract_emails(re.sub(r"[;,\s]+", " ", unquote(href[7:]).split("?", 1)[0])):
                add_email(e)
            continue
        target = resolve_href(href, base)
        if not target or target in seen_links:
            continue
        seen_links.add(target)
        ext = path_ext(target)
        if ext in ASSET_EXTS:
            kind = asset_kind(target)
            entry = {"url": target, "link_text": clean(a.get_text(" "))[:200]}
            (documents if kind == "documents" else images if kind == "images" else videos if kind == "videos" else other_assets).append(entry)
        elif host_of(target) == site_host:
            internal.append(target)
        else:
            external.append(target)
            if is_social(target) and target not in socials:
                socials.append(target)
    for el in soup.select("[data-cfemail]"):
        decoded = decode_cfemail(el["data-cfemail"])
        if decoded:
            add_email(decoded)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if img.get("srcset") or img.get("data-srcset"):
            src = biggest_srcset(img.get("srcset") or img.get("data-srcset")) or src
        u = resolve_href(src, base)
        if u and u not in {i["url"] for i in images}:
            images.append({"url": u, "alt": clean(img.get("alt"))})
    icon = soup.find("link", rel=re.compile("icon"))
    for val in (page["og"].get("image"), icon.get("href") if icon else None):
        u = resolve_href(val, base)
        if u and u not in {i["url"] for i in images}:
            images.append({"url": u, "alt": ""})
    for vid in soup.find_all(["video", "audio"]):
        for src in [vid.get("src")] + [s.get("src") for s in vid.find_all("source")]:
            u = resolve_href(src, base)
            if u:
                videos.append({"url": u, "type": "file"})
    for frame in soup.find_all("iframe", src=True):
        u = resolve_href(frame["src"], base)
        if u and re.search(r"(youtube|youtu\.be|vimeo|dailymotion)", host_of(u)):
            videos.append({"url": u, "type": "embed"})

    forms = [{"action": resolve_href(f.get("action") or url, base), "method": (f.get("method") or "get").lower(),
              "fields": [i.get("name") for i in f.find_all(["input", "select", "textarea"]) if i.get("name")]}
             for f in soup.find_all("form")]
    tables = []
    for t in soup.find_all("table")[:20]:
        rows = [[clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if len(rows) > 1:
            tables.append(rows)
    addresses = [clean(a.get_text(" ")) for a in soup.find_all("address") if clean(a.get_text(" "))]

    # Text, with navigation/footers/scripts removed.
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    full_text = clean(soup.get_text(" "))
    contact_text = full_text  # footers/headers often hold the contact details
    for tag in soup.find_all(BOILERPLATE_TAGS):
        tag.decompose()
    main = pick_main(soup)
    text = clean(main.get_text(" "))
    for e in extract_emails(contact_text):
        add_email(e)

    headings = {f"h{n}": [clean(h.get_text(" ")) for h in soup.find_all(f"h{n}") if clean(h.get_text(" "))] for n in (1, 2, 3)}
    page.update({
        "headings": headings, "text": text, "word_count": len(text.split()), "full_text_words": len(full_text.split()),
        "tables": tables, "forms": forms, "emails": emails, "phones": extract_phones(contact_text, region),
        "socials": socials, "addresses": addresses, "images": images, "videos": videos,
        "documents": documents, "other_assets": other_assets, "internal_links": internal, "external_links": external,
    })
    for ld in json_ld:  # postal addresses from structured data
        for node in (ld if isinstance(ld, list) else ld.get("@graph", [ld]) if isinstance(ld, dict) else []):
            addr = node.get("address") if isinstance(node, dict) else None
            if isinstance(addr, dict):
                line = clean(", ".join(str(addr.get(k, "")) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry") if addr.get(k)))
                if line and line not in addresses:
                    addresses.append(line)
    page["addresses"] = addresses
    page["_spa_hint"] = any(m in html for m in SPA_MARKERS)
    return page


# --------------------------------------------------------------------------- crawler


class Crawler:
    def __init__(self, start_url: str, args: argparse.Namespace, fetcher: Fetcher, renderer: Renderer):
        p = urlsplit(start_url if "://" in start_url else f"https://{start_url}")
        self.start = quote_url(urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, "")))
        self.host = host_of(self.start)
        self.args, self.fetcher, self.renderer = args, fetcher, renderer
        self.out = Path(args.out) / re.sub(r"[^\w.\-]", "_", self.host)
        self.robots = Robots(fetcher, f"{p.scheme}://{p.netloc.lower()}")
        self.errors: list[dict] = []
        self.assets: dict[str, dict] = {}
        self.contacts: dict[tuple[str, str], dict] = {}
        self.page_count = self.rendered_pages = 0
        self.query_variants: dict[str, int] = {}
        self.path_prefix = args.same_path and urlsplit(self.start).path.rsplit("/", 1)[0] or ""

    def in_scope(self, url: str) -> bool:
        h = host_of(url)
        ok = h == self.host or (self.args.subdomains and h.endswith("." + self.host))
        return ok and urlsplit(url).path.startswith(self.path_prefix or "/")

    def record_error(self, url: str, reason: str, status: int | None, stage: str) -> None:
        self.errors.append({"url": url, "reason": reason, "status": status, "stage": stage, "at": utcnow()})

    def fetch_page(self, item: tuple[str, int]) -> tuple[str, int, dict | None, str | None, int | None]:
        url, depth = item
        try:
            resp = self.fetcher.get(url)
        except BlockedError as exc:
            return url, depth, None, exc.reason, exc.status
        except FetchError as exc:
            return url, depth, None, exc.reason, exc.status
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if resp.status_code >= 400:
            return url, depth, None, f"HTTP {resp.status_code}", resp.status_code
        if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return url, depth, {"_asset": True, "content_type": ctype, "final_url": resp.url}, None, resp.status_code
        if "charset" not in resp.headers.get("content-type", "").lower():
            resp.encoding = resp.apparent_encoding or "utf-8"
        html, rendered = resp.text, False
        region = region_for(url, self.args.region)
        page = extract_page(html, resp.url, region)
        mode = self.args.render
        thin = page["full_text_words"] < self.args.min_words and (page["_spa_hint"] or len(page["internal_links"]) < 3)
        if mode == "always" or (mode == "auto" and thin):
            rhtml = self.renderer.render(resp.url)
            if rhtml:
                rpage = extract_page(rhtml, resp.url, region)
                if rpage["full_text_words"] >= page["full_text_words"]:
                    page, rendered = rpage, True
        page.update({"status": resp.status_code, "final_url": resp.url, "depth": depth, "rendered": rendered, "crawled_at": utcnow()})
        page.pop("_spa_hint", None)
        return url, depth, page, None, resp.status_code

    def note_contacts(self, page: dict) -> None:
        def add(kind: str, value: str, normalized: str | None = None) -> None:
            key = (kind, normalized or value.lower())
            entry = self.contacts.setdefault(key, {"type": kind, "value": value, "normalized": normalized or value.lower(),
                                                   "source_pages": [], "first_seen": utcnow()})
            if page["url"] not in entry["source_pages"]:
                entry["source_pages"].append(page["url"])
        for e in page["emails"]:
            add("email", e)
        for ph in page["phones"]:
            add("phone", ph["raw"], ph["e164"])
        for s in page["socials"]:
            add("social", s, re.sub(r"^https?://(www\.)?", "", s).rstrip("/").lower())
        for a in page["addresses"]:
            add("address", a)

    def note_assets(self, page: dict) -> None:
        if not self.args.assets:
            return
        groups = [("images", page["images"]), ("videos", [v for v in page["videos"] if v["type"] == "file"]),
                  ("documents", page["documents"]), ("other", page["other_assets"])]
        for _, items in groups:
            for it in items:
                kind = asset_kind(it["url"])
                if kind == "images" or path_ext(it["url"]) or kind != "other":
                    entry = self.assets.setdefault(it["url"], {"url": it["url"], "kind": kind, "source_pages": []})
                    if page["url"] not in entry["source_pages"] and len(entry["source_pages"]) < 20:
                        entry["source_pages"].append(page["url"])

    def run(self) -> dict:
        a = self.args
        started = utcnow()
        self.out.mkdir(parents=True, exist_ok=True)
        self.robots.load()
        log.info("[%s] robots: %s, sitemaps: %d", self.host, self.robots.policy, len(self.robots.sitemaps))

        queue: list[tuple[str, int]] = [(self.start, 0)]
        seen = {url_key(self.start)}
        if not a.no_sitemap and self.robots.policy != "disallow_all":
            for u in fetch_sitemap_urls(self.fetcher, self.robots, self.robots.origin, a.max_pages * 2):
                u = resolve_href(u, self.start)
                if u and self.in_scope(u) and url_key(u) not in seen and path_ext(u) not in NON_PAGE_EXTS:
                    seen.add(url_key(u))
                    queue.append((u, 1))
            log.info("[%s] %d URLs queued after sitemap discovery", self.host, len(queue))

        pages_path = self.out / "pages.jsonl"
        blocked = False
        with open(pages_path, "w", encoding="utf-8") as pages_fh, ThreadPoolExecutor(max_workers=a.workers) as pool:
            while queue and self.page_count < a.max_pages and not blocked:
                batch: list[tuple[str, int]] = []
                while queue and len(batch) < a.workers * 3 and self.page_count + len(batch) < a.max_pages:
                    url, depth = queue.pop(0)
                    if not self.robots.allowed(url):
                        self.record_error(url, "disallowed by robots.txt", None, "robots")
                        continue
                    batch.append((url, depth))
                for url, depth, page, err, status in pool.map(self.fetch_page, batch):
                    if err or page is None:
                        self.record_error(url, err or "unknown", status, "crawl")
                        if self.host in self.fetcher.blocked:
                            blocked = True
                        continue
                    if page.get("_asset"):
                        self.assets.setdefault(url, {"url": url, "kind": asset_kind(url, page["content_type"]), "source_pages": []})
                        continue
                    self.page_count += 1
                    self.rendered_pages += page["rendered"]
                    pages_fh.write(json.dumps(page, ensure_ascii=False, default=list) + "\n")
                    self.note_contacts(page)
                    self.note_assets(page)
                    log.info("[%s] %d/%d %s (%d words%s)", self.host, self.page_count, a.max_pages, url, page["word_count"],
                             ", rendered" if page["rendered"] else "")
                    if depth < a.max_depth:
                        for link in page["internal_links"]:
                            if not self.in_scope(link) or path_ext(link) in NON_PAGE_EXTS:
                                continue
                            key = url_key(link)
                            if key in seen:
                                continue
                            qs = urlsplit(link)
                            if qs.query:  # guard against calendar / filter crawl traps
                                n = self.query_variants.get(qs.path, 0) + 1
                                self.query_variants[qs.path] = n
                                if n > a.max_query_variants:
                                    continue
                            seen.add(key)
                            queue.append((link, depth + 1))

        manifest = self.download_assets() if a.assets and not blocked else []
        write_json(self.out / "contacts.json", sorted(self.contacts.values(), key=lambda c: (c["type"], c["normalized"])))
        write_json(self.out / "assets_manifest.json", manifest or list(self.assets.values()))
        write_json(self.out / "errors.json", self.errors)
        site = {"start_url": self.start, "host": self.host, "started": started, "finished": utcnow(), "pages": self.page_count,
                "rendered_pages": self.rendered_pages, "robots": {"policy": self.robots.policy, "sitemaps": self.robots.sitemaps},
                "blocked": blocked or self.host in self.fetcher.blocked, "errors": len(self.errors), "assets": len(self.assets),
                "contacts": len(self.contacts)}
        write_json(self.out / "site.json", site)
        self.write_summary(site)
        return site

    # ------------------------------------------------------------------ assets

    def download_assets(self) -> list[dict]:
        a = self.args
        by_hash: dict[str, str] = {}
        names: dict[str, str] = {}
        manifest: list[dict] = []
        lock = threading.Lock()

        def one(entry: dict) -> dict:
            url, referer = entry["url"], (entry["source_pages"] or [self.start])[0]
            entry = dict(entry, local_path=None)
            try:
                if host_of(url) == self.host and not self.robots.allowed(url):
                    raise FetchError(url, "disallowed by robots.txt")
                resp = self.fetcher.get(url, stream=True, referer=referer)
                if resp.status_code >= 400:
                    resp.close()
                    raise FetchError(url, f"HTTP {resp.status_code}", resp.status_code)
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype.startswith("text/html"):
                    resp.close()
                    raise FetchError(url, "not a file (HTML response)")
                kind = asset_kind(url, ctype)
                limit = a.max_asset_mb * 1024 * 1024
                if int(resp.headers.get("content-length") or 0) > limit:
                    resp.close()
                    raise FetchError(url, f"larger than {a.max_asset_mb} MB")
                digest, size = hashlib.sha256(), 0
                tmp = Path(tempfile.mkstemp(dir=self.out, suffix=".part")[1])
                try:
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(65536):
                            size += len(chunk)
                            if size > limit:
                                raise FetchError(url, f"larger than {a.max_asset_mb} MB")
                            digest.update(chunk)
                            fh.write(chunk)
                    sha = digest.hexdigest()
                    with lock:
                        if sha in by_hash:
                            entry.update(local_path=by_hash[sha], duplicate=True)
                            tmp.unlink()
                        else:
                            name = sanitize_filename(urlsplit(url).path.rsplit("/", 1)[-1] or "file")
                            if "." not in name and kind == "images":
                                name += "." + (ctype.split("/")[-1].split("+")[0] or "bin")
                            rel = f"assets/{kind}/{name}"
                            if names.get(rel, sha) != sha or (self.out / rel).exists() and rel not in names:
                                stem, dot, ext = name.rpartition(".")
                                rel = f"assets/{kind}/{(stem + '_' if dot else name + '_') + sha[:8]}{dot}{ext}"
                            names[rel] = sha
                            dest = self.out / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(tmp, dest)
                            by_hash[sha] = rel
                            entry["local_path"] = rel
                    entry.update(kind=kind, sha256=sha, size=size, content_type=ctype, downloaded_at=utcnow())
                finally:
                    tmp.unlink(missing_ok=True)
            except BlockedError as exc:
                self.record_error(url, exc.reason, exc.status, "asset")
            except FetchError as exc:
                self.record_error(url, exc.reason, exc.status, "asset")
            return entry

        todo = list(self.assets.values())
        log.info("[%s] downloading %d assets", self.host, len(todo))
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            manifest.extend(pool.map(one, todo))
        return manifest

    def write_summary(self, site: dict) -> None:
        by_type: dict[str, int] = {}
        for c in self.contacts.values():
            by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        lines = [f"# Scrape summary: {self.host}", "", f"- Start URL: {self.start}", f"- Pages: {site['pages']} (JS-rendered: {site['rendered_pages']})",
                 f"- robots.txt policy: {site['robots']['policy']}", f"- Blocked: {site['blocked']}", f"- Errors: {site['errors']}",
                 f"- Assets found: {site['assets']}", "- Contacts: " + (", ".join(f"{n} {k}" for k, n in sorted(by_type.items())) or "none"), ""]
        if self.errors:
            lines += ["## First errors", ""] + [f"- `{e['url']}` [{e['stage']}] {e['reason']}" for e in self.errors[:20]]
        (self.out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Automated polite scraper for any website.")
    p.add_argument("urls", nargs="*", help="start URL(s); scheme optional")
    p.add_argument("--urls-file", help="file with one URL per line")
    p.add_argument("-o", "--out", default="out", help="output directory (default: out)")
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--max-depth", type=int, default=5)
    p.add_argument("--max-query-variants", type=int, default=20, help="max URLs with ?query per path (crawl-trap guard)")
    p.add_argument("--assets", action="store_true", help="download images, videos, documents and other files")
    p.add_argument("--max-asset-mb", type=int, default=100)
    p.add_argument("--render", choices=["auto", "never", "always"], default="auto", help="Playwright rendering for JS pages")
    p.add_argument("--min-words", type=int, default=60, help="static pages with fewer words trigger --render auto")
    p.add_argument("--subdomains", action="store_true", help="also crawl subdomains of the start host")
    p.add_argument("--same-path", action="store_true", help="stay under the start URL's directory")
    p.add_argument("--no-sitemap", action="store_true")
    p.add_argument("--region", help="default region for phone numbers (e.g. IN, US); guessed from the TLD otherwise")
    p.add_argument("--workers", type=int, default=2, help="concurrent requests (spaced by the delay)")
    p.add_argument("--delay-min", type=float, default=1.0)
    p.add_argument("--delay-max", type=float, default=2.0)
    p.add_argument("--timeout", type=float, default=20)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--contact", help="email/URL added to the User-Agent so site owners can reach you")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    urls = list(args.urls)
    if args.urls_file:
        urls += [l.strip() for l in Path(args.urls_file).read_text().splitlines() if l.strip() and not l.startswith("#")]
    if not urls and sys.stdin.isatty():
        urls = input("URL(s) to scrape (space separated): ").split()
    if not urls:
        print("No URL given.", file=sys.stderr)
        return 2
    ua = DEFAULT_UA.format(contact=f"; contact: {args.contact}" if args.contact else "")
    fetcher = Fetcher(ua, args.timeout, args.retries, args.delay_min, args.delay_max)
    renderer = Renderer(ua, args.timeout)
    rc = 0
    try:
        for url in urls:
            site = Crawler(url, args, fetcher, renderer).run()
            log.info("[%s] done: %d pages, %d errors -> %s", site["host"], site["pages"], site["errors"], Path(args.out) / site["host"])
            rc = rc or (1 if site["blocked"] or site["pages"] == 0 else 0)
    finally:
        renderer.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
