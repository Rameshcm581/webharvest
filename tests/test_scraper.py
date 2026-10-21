"""Offline end-to-end tests against a throwaway local HTTP server."""

import json
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scraper  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
LOREM = "Welcome to our company. " * 20

PAGES = {
    "/": f"""<html lang="en"><head><title>Home</title><meta name="description" content="Demo site">
        <script type="application/ld+json">{{"@type":"Organization","address":{{"streetAddress":"1 Main St","addressLocality":"Springfield"}}}}</script></head>
        <body><nav><a href="/about">About</a> <a href="/products?page=2">P2</a></nav>
        <main><h1>Home</h1><p>{LOREM}</p><img src="/logo.png" alt="logo"><a href="/files/brochure.pdf">Brochure</a>
        <a href="/private/secret">secret</a><a href="https://twitter.com/demo">tw</a><a href="https://ext.example/x">ext</a>
        <a href="/about/?utm_source=x">about again</a></main>
        <footer>Mail sales [at] demo [dot] com or <a href="mailto:info@demo.com">info</a>. Call +1 415 555 2671.</footer></body></html>""",
    "/about": f"<html><head><title>About</title></head><body><main><h1>About</h1><p>{LOREM}</p></main></body></html>",
    "/products": f"<html><head><title>Products</title></head><body><main><p>{LOREM}</p><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></main></body></html>",
    "/spa": '<html><head><title>SPA</title></head><body><div id="root"></div><script>1</script></body></html>',
}
ROBOTS = "User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap.xml\n"
SITEMAP = '<urlset><url><loc>{base}/spa</loc></url><url><loc>{base}/about</loc></url></urlset>'


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        path = self.path.split("?")[0]
        Handler.hits.append(self.path)
        if path == "/robots.txt":
            return self._send(200, ROBOTS.format(base=base), "text/plain")
        if path == "/sitemap.xml":
            return self._send(200, SITEMAP.format(base=base), "application/xml")
        if path == "/logo.png":
            return self._send(200, PNG, "image/png")
        if path == "/files/brochure.pdf":
            return self._send(200, b"%PDF-1.4 demo", "application/pdf")
        if path in PAGES:
            return self._send(200, PAGES[path], "text/html; charset=utf-8")
        self._send(404, "nope", "text/plain")

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def server():
    Handler.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def run(base, tmp_path, *extra):
    rc = scraper.main([base, "-o", str(tmp_path), "--delay-min", "0", "--delay-max", "0", "--render", "never", *extra])
    return rc, tmp_path / base.split("//")[1].replace(":", "_")


def test_crawl_extracts_everything(server, tmp_path):
    rc, out = run(server, tmp_path, "--assets", "--region", "US")
    assert rc == 0
    pages = {json.loads(l)["url"]: json.loads(l) for l in (out / "pages.jsonl").read_text().splitlines()}
    home = pages[server + "/"]
    assert home["title"] == "Home" and home["description"] == "Demo site" and home["language"] == "en"
    assert home["headings"]["h1"] == ["Home"] and "Welcome" in home["text"]
    assert "info@demo.com" in home["emails"] and "sales@demo.com" in home["emails"]
    assert home["phones"][0]["e164"] == "+14155552671"
    assert home["socials"] == ["https://twitter.com/demo"]
    assert "1 Main St, Springfield" in home["addresses"]
    # nav text must not leak into main text, but links still get followed
    assert "About" not in home["text"].split("Welcome")[0]
    assert server + "/about" in pages and server + "/products?page=2" in pages
    assert pages[server + "/products?page=2"]["tables"] == [[["A", "B"], ["1", "2"]]]
    # sitemap-only page discovered; robots-disallowed page never requested
    assert server + "/spa" in pages
    assert not any(h.startswith("/private") for h in Handler.hits)
    errors = json.loads((out / "errors.json").read_text())
    assert any(e["stage"] == "robots" for e in errors)
    # tracking-param duplicate of /about is not crawled twice
    assert sum(1 for h in Handler.hits if h.startswith("/about")) == 1


def test_assets_downloaded_and_manifested(server, tmp_path):
    _, out = run(server, tmp_path, "--assets")
    manifest = {m["url"]: m for m in json.loads((out / "assets_manifest.json").read_text())}
    png = manifest[server + "/logo.png"]
    assert png["kind"] == "images" and (out / png["local_path"]).read_bytes() == PNG
    pdf = manifest[server + "/files/brochure.pdf"]
    assert pdf["kind"] == "documents" and (out / pdf["local_path"]).exists()
    assert len(png["sha256"]) == 64


def test_no_assets_by_default(server, tmp_path):
    _, out = run(server, tmp_path)
    assert not (out / "assets").exists()
    assert "/logo.png" not in Handler.hits


def test_max_pages_and_contacts(server, tmp_path):
    _, out = run(server, tmp_path, "--max-pages", "2")
    assert json.loads((out / "site.json").read_text())["pages"] == 2
    contacts = json.loads((out / "contacts.json").read_text())
    assert {"email", "phone", "social"} <= {c["type"] for c in contacts}


def test_spa_hint_detected():
    page = scraper.extract_page(PAGES["/spa"], "http://x.test/spa", None)
    assert page["_spa_hint"] and page["full_text_words"] < 60


def test_unreachable_host_reports_failure(tmp_path):
    rc = scraper.main(["http://127.0.0.1:9", "-o", str(tmp_path), "--retries", "1", "--delay-min", "0", "--delay-max", "0"])
    assert rc == 1


def test_url_helpers():
    assert scraper.url_key("https://www.a.com/x/?b=1&a=2") == scraper.url_key("http://a.com/x?a=2&b=1")
    assert scraper.resolve_href("/p?utm_source=z&id=3", "http://a.com/") == "http://a.com/p?id=3"
    assert scraper.resolve_href("javascript:void(0)", "http://a.com/") is None
