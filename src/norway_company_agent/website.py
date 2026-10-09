from __future__ import annotations

import json
import ipaddress
import re
import socket
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence, utc_now

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "terms", "salgsbetingelser", "betingelser", "kjopsvilkar", "kjøpsvilkår", "vilkar", "vilkår", "impressum",
    "kontakt", "contact", "contact-information", "personvern", "privacy",
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "karriere", "career", "careers", "jobb", "stillinger", "jobs", "ledige-stillinger",
    "news", "press", "aktuelt", "nyheter",
    "contact-information", "vilkar", "vilkår", "terms", "impressum",
)


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())
_BROWSER_SLOTS = threading.BoundedSemaphore(2)


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority += f":{port}"
    if "@" in parsed.netloc:
        authority = parsed.netloc.rsplit("@", 1)[0] + "@" + authority
    path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunparse((parsed.scheme, authority, path, "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


def _robots_allowed(url: str, timeout: float) -> bool:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            parser.parse(response.read().decode("utf-8", errors="replace").splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        # An unavailable robots file is not permission to ignore explicit site terms; callers retain
        # the URL and can route uncertain domains to review. For this bounded homepage POC, allow one
        # ordinary GET when robots.txt is absent rather than crawl deeper.
        return True


def _social_captures(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = {**normalized, "claim_span": candidate, "extraction_method": "html_social_attribute_v1"}
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    return [{"platform": item["platform"], "url": item["url"]} for item in _social_captures(base_url, soup)]


def _page_social_captures(base_url: str, soup: BeautifulSoup, organizations: list[dict[str, Any]]) -> list[dict[str, str]]:
    captures = _social_captures(base_url, soup)
    for organization in organizations:
        same_as = organization.get("sameAs") or []
        for raw in same_as if isinstance(same_as, list) else [same_as]:
            if isinstance(raw, str) and (normalized := normalize_social_url(raw)):
                captures.append({**normalized, "claim_span": raw, "extraction_method": "organization_jsonld_sameAs_v1"})
    return captures


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", haystack)), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
    return [url for url, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _identity_text(soup: BeautifulSoup) -> str:
    nodes = soup.select(
        'footer, [role="contentinfo"], #footer, .footer, address, '
        '[itemprop="legalName"], [itemprop="address"], '
        '[itemprop="telephone"], [itemprop="email"], [itemprop="taxID"], [itemprop="vatID"], '
        '.address-data, .contact-address, .postal-address'
    )
    nodes.extend(soup.find_all(id=re.compile(r"^footer[-_]?\d+$", re.I)))
    # Keep distinct fragments; repeated nested footer selectors must not consume the byte budget.
    fragments = dict.fromkeys(" ".join(node.get_text(" ", strip=True).split()) for node in nodes)
    return " | ".join(fragment for fragment in fragments if fragment)[:3000]


ACTIVITY_PATH = re.compile(r"/(?:news|press|aktuelt|nyheter|artikler|blog|blogs|blogg|inspiration|inspirasjon|journal)(?:/|$)", re.I)


def _activity_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    links = []
    cards = soup.select(".wp-block-post-title a[href], article h2 a[href], article h3 a[href]") if ACTIVITY_PATH.search(base.path) else []
    for anchor in [*cards, *soup.select("a[href]")]:
        parsed = urllib.parse.urlparse(urllib.parse.urljoin(base_url, str(anchor.get("href") or "")))
        if parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc:
            continue
        is_blog_post = ACTIVITY_PATH.search(base.path) and parsed.path.startswith("/post/") and anchor.get_text(" ", strip=True)
        if not ACTIVITY_PATH.search(parsed.path) and anchor not in cards and not is_blog_post:
            continue
        url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, urllib.parse.quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"), "", "", ""))
        if url.rstrip("/") != base_url.rstrip("/"):
            links.append(url)
    return list(dict.fromkeys(links))[:20]


def _article_capture(url: str, soup: BeautifulSoup, metadata: dict[str, Any]) -> dict[str, Any] | None:
    articles = _jsonld_types(metadata, {"Article", "NewsArticle", "BlogPosting"})
    matching = []
    for article in articles:
        declared = article.get("url") or article.get("mainEntityOfPage")
        if isinstance(declared, dict):
            declared = declared.get("@id")
        if declared and urllib.parse.urljoin(url, str(declared)).split("#")[0].rstrip("/") != url.rstrip("/"):
            continue
        if article.get("datePublished") and isinstance(article.get("headline") or article.get("name"), str):
            matching.append(article)
    if len(matching) == 1:
        article = matching[0]
        return {"title": article.get("headline") or article["name"], "published_at": str(article["datePublished"]), "date_extraction_method": "article_jsonld_datePublished_v1"}
    if len(articles) > 1:
        return None
    published = soup.select_one('meta[property="article:published_time"]')
    heading = soup.select_one("h1")
    if published and published.get("content") and heading and heading.get_text(" ", strip=True):
        return {"title": heading.get_text(" ", strip=True), "published_at": str(published["content"]), "date_extraction_method": "article_published_time_meta_v1"}
    return None


def _publication_feed_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    for link in soup.find_all("link", type="application/rss+xml"):
        if "alternate" not in (link.get("rel") or []):
            continue
        url = urllib.parse.urljoin(base_url, str(link.get("href") or ""))
        parsed = urllib.parse.urlparse(url)
        label = (url + " " + str(link.get("title") or "")).casefold()
        if parsed.scheme in {"http", "https"} and parsed.netloc == base.netloc and not any(term in label for term in ("comment", "kommentar")):
            return [url]
    return []


def _parse_publication_feed(raw: bytes, url: str, *, limit: int = 2) -> list[dict[str, str]]:
    if b"<!DOCTYPE" in raw.replace(b"\x00", b"").upper():
        raise ValueError("RSS document types are not supported")
    root = ET.fromstring(raw)
    if root.tag != "rss":
        raise ValueError("Expected an RSS publication feed")
    host = urllib.parse.urlparse(url).netloc
    items = []
    seen = set()
    for node in root.findall("./channel/item"):
        title = str(node.findtext("title") or "").strip()
        declared_link = str(node.findtext("link") or "").strip()
        link = urllib.parse.urljoin(url, declared_link)
        original_date = str(node.findtext("pubDate") or "").strip()
        parsed = urllib.parse.urlparse(link)
        if not title or not declared_link or parsed.scheme not in {"http", "https"} or parsed.netloc != host or "comment" in parsed.fragment.casefold() or link in seen:
            continue
        try:
            date = parsedate_to_datetime(original_date)
            date = date.replace(tzinfo=date.tzinfo or timezone.utc)
        except (ValueError, TypeError):
            continue
        items.append({"title": title, "publication_url": link, "published_at": date.isoformat(), "date_evidence_span": original_date})
        seen.add(link)
        if len(items) >= limit:
            break
    return items


def fetch_site_activity_pages(website: dict[str, Any], *, max_pages: int = 2, timeout: float = 8) -> dict[str, Any]:
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    value = website.get("value") or {}
    if website.get("status") != "available" or not (value.get("identity_assessment") or {}).get("publishable"):
        return metrics
    pages = value.get("pages") or []
    visited = {page["url"].rstrip("/") for page in pages}
    homepage = value.get("final_url") or website.get("source_url")
    domain = _registered_domain(homepage)
    attempts = 0
    for _ in range(max_pages):
        queue = list(dict.fromkeys(url for page in pages for url in page.get("activity_links") or [] if url.rstrip("/") not in visited))
        if not queue:
            break
        attempts += 1
        url = queue[0]
        visited.add(url.rstrip("/"))
        discovered_from = next(page["url"] for page in pages if url in (page.get("activity_links") or []))
        page, _, requests, size, elapsed, error = _fetch_secondary_page(url, homepage_domain=domain, timeout=timeout, max_bytes=2_000_000)
        metrics["requests"] += requests
        metrics["bytes"] += size
        if elapsed:
            metrics["latencies_ms"].append(elapsed)
        if page and (urllib.parse.urlparse(page["url"]).hostname or "").removeprefix("www.") != (urllib.parse.urlparse(homepage).hostname or "").removeprefix("www."):
            page = None
            error = "activity redirected outside verified homepage host"
        if page:
            page["activity_discovered_from"] = discovered_from
            pages.append(page)
        elif error:
            value.setdefault("crawl_errors", []).append({"url": url, "error": error, "strategy": "dated_activity_v1"})
    value["pages"] = pages
    # Use an advertised publication feed only when no dated HTML article was captured.
    if attempts < max_pages and not any(page.get("article") for page in pages):
        feeds = list(dict.fromkeys(url for page in pages for url in page.get("publication_feed_links") or []))
        if feeds:
            url = feeds[0]
            value.pop("publication_feeds", None)
            started = time.monotonic()
            try:
                if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(homepage).netloc:
                    raise ValueError("Feed is outside verified host")
                assert_public_url(url)
                if not _robots_allowed(url, timeout):
                    metrics["requests"] += 1
                    raise ValueError("robots.txt disallows feed")
                metrics["requests"] += 2
                with SAFE_OPENER.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,application/xml,text/xml"}), timeout=timeout) as response:
                    raw = response.read(2_000_001)
                    final_url = response.geturl()
                    assert_public_url(final_url)
                metrics["bytes"] += len(raw)
                if len(raw) > 2_000_000 or urllib.parse.urlparse(final_url).netloc != urllib.parse.urlparse(homepage).netloc:
                    raise ValueError("Feed exceeds byte limit or redirects outside verified host")
                discovered_from = next(page["url"] for page in pages if url in (page.get("publication_feed_links") or []))
                value["publication_feeds"] = [{"url": final_url, "retrieved_at": utc_now(), "content_sha256": __import__("hashlib").sha256(raw).hexdigest(), "items": _parse_publication_feed(raw, final_url), "discovered_from": discovered_from}]
            except Exception as exc:
                value.setdefault("crawl_errors", []).append({"url": url, "error": f"{type(exc).__name__}: {str(exc)[:120]}", "strategy": "company_site_rss_v1"})
            finally:
                metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
    return metrics


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    if not _robots_allowed(url, timeout):
        return None, [], 1, 0, 0, "robots.txt disallows page"
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        identity_text = _identity_text(page_soup)
        metadata = extruct.extract(raw, base_url=final_url, syntaxes=["json-ld"])
        social_captures = _page_social_captures(final_url, page_soup, _jsonld_organisations(metadata))
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "identity_text_excerpt": identity_text,
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "structured_jobs": _jsonld_types(metadata, {"JobPosting"}),
            "social_link_evidence": social_captures,
            "retrieved_at": utc_now(),
            "activity_links": _activity_links(final_url, page_soup),
            "publication_feed_links": _publication_feed_links(final_url, page_soup),
            "article": _article_capture(final_url, page_soup, metadata),
        }
        return page, [{"platform": item["platform"], "url": item["url"]} for item in social_captures], 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return _jsonld_types(metadata, {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"})


def _jsonld_types(metadata: dict[str, Any], accepted: set[str]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & accepted:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    scripts = len(soup.select("script[src]"))
    body_text = soup.body.get_text(" ", strip=True) if soup.body else soup.get_text(" ", strip=True)
    incomplete_shell = scripts >= 1 and len(body_text.strip()) < 100
    return "js_fallback_candidate" if len(text.strip()) < 100 and (scripts >= 2 or incomplete_shell) else "static_complete"


def _rendered_website_record(url: str, markup: bytes, document: bytes, document_url: str) -> dict[str, Any]:
    if max(len(markup), len(document)) > 2_000_000:
        raise ValueError("Browser capture exceeds byte limit")
    soup = BeautifulSoup(markup, "lxml")
    structured = extruct.extract(markup, base_url=url, syntaxes=["json-ld", "microdata", "opengraph"])
    text = trafilatura.extract(markup, url=url, include_links=False, include_tables=False, favor_precision=True) or ""
    title = soup.title.get_text(" ", strip=True)[:500] if soup.title else ""
    description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = str(description_tag.get("content") or "").strip()[:2000] if description_tag else ""
    identity_text = _identity_text(soup)
    captured = utc_now()
    digest = __import__("hashlib").sha256(markup).hexdigest()
    captures = _page_social_captures(url, soup, _jsonld_organisations(structured))
    page = {"url": url, "title": title, "main_text_excerpt": text[:5000], "identity_text_excerpt": identity_text,
            "content_sha256": digest, "capture_kind": "rendered_dom", "retrieved_at": captured,
            "structured_jobs": _jsonld_types(structured, {"JobPosting"}), "social_link_evidence": captures,
            "activity_links": _activity_links(url, soup), "publication_feed_links": _publication_feed_links(url, soup),
            "article": _article_capture(url, soup, structured)}
    value = {"final_url": url, "registered_domain": _registered_domain(url), "title": title, "description": description,
             "main_text_excerpt": text[:5000], "identity_text_excerpt": identity_text,
             "structured_organisations": _jsonld_organisations(structured),
             "social_links": [{"platform": item["platform"], "url": item["url"]} for item in captures],
             "pages": [page], "content_sha256": digest, "capture_kind": "rendered_dom",
             "captured_html": markup.decode("utf-8"), "extraction_state": "browser_rendered",
             "http_document": {"source_url": document_url, "content_sha256": __import__("hashlib").sha256(document).hexdigest(), "capture_kind": "browser_http_response_body", "captured_at": captured}}
    return evidence("website", "available", "company_owned_browser_snapshot", url, value=value, retrieved_at=captured,
                    content_sha256=digest, note="Rendered DOM snapshot; its digest is distinct from the HTTP document body digest")


def _fetch_rendered_identity_pages(record: dict[str, Any], metrics: dict[str, Any], *, deadline: float, max_requests: int) -> None:
    value = record["value"]
    soup = BeautifulSoup(value["captured_html"], "lxml")
    errors = value.setdefault("crawl_errors", [])
    for url in _priority_links(value["final_url"], soup):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or metrics["requests"] + 2 > max_requests:
            break
        page, social, requests, size, _, error = _fetch_secondary_page(
            url, homepage_domain=value["registered_domain"], timeout=min(3, remaining / 2), max_bytes=2_000_000,
        )
        metrics["requests"] += requests
        metrics["bytes"] += size
        if page:
            page["capture_kind"] = "http_response_body"
            if page["url"].rstrip("/") not in {item["url"].rstrip("/") for item in value["pages"]}:
                value["pages"].append(page)
            value["social_links"].extend(social)
        elif error:
            errors.append({"url": url, "error": error})
    value["social_links"] = list({(item["platform"], item["url"]): item for item in value["social_links"]}.values())


def fetch_rendered_website(record: dict[str, Any], *, max_seconds: float = 20, max_requests: int = 40) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    value = record.get("value") or {}
    reasons = (value.get("identity_assessment") or {}).get("reasons") or []
    if record.get("status") != "ambiguous" or value.get("extraction_state") != "js_fallback_candidate" or any("different legal organisation number" in reason or "parked," in reason or "business sports-club" in reason for reason in reasons):
        return record, metrics
    started = time.monotonic()
    deadline = started + max_seconds
    acquired = False
    url = str(value.get("final_url") or record.get("source_url") or "")
    try:
        if min(max_seconds, max_requests) <= 0:
            raise ValueError("Browser limits must be positive")
        assert_public_url(url)
        metrics["requests"] += 1
        if not _robots_allowed(url, min(3, max_seconds)):
            raise ValueError("robots.txt disallows browser document")
        acquired = _BROWSER_SLOTS.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            raise TimeoutError("Browser concurrency budget exhausted")
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

        def remaining_ms(cap: int) -> int:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise TimeoutError("Browser time budget exhausted")
            return min(cap, remaining)

        with sync_playwright() as playwright:
            options = {"headless": True, "timeout": remaining_ms(5000)}
            channel = os.environ.get("SIGNALPOST_BROWSER_CHANNEL", "").strip()
            if channel:
                options["channel"] = channel
            browser = playwright.chromium.launch(**options)
            try:
                context = browser.new_context(user_agent=USER_AGENT)
                # Reserve at most four robots/page pairs for legal links revealed by rendering.
                browser_request_limit = max_requests - min(8, max(0, max_requests - 3))
                def guard(route: Any) -> None:
                    if route.request.resource_type in {"image", "media", "font"} or time.monotonic() >= deadline or metrics["requests"] >= browser_request_limit:
                        route.abort()
                        return
                    try:
                        assert_public_url(route.request.url)
                        if route.request.resource_type == "document":
                            if _registered_domain(route.request.url) != _registered_domain(url):
                                raise ValueError("Browser document left candidate domain")
                            metrics["requests"] += 1
                            if not _robots_allowed(route.request.url, max(0.1, min(3, deadline - time.monotonic()))):
                                raise ValueError("robots.txt disallows browser document")
                        if metrics["requests"] >= browser_request_limit or time.monotonic() >= deadline:
                            route.abort()
                            return
                        metrics["requests"] += 1
                        route.continue_()
                    except Exception:
                        route.abort()
                context.route("**/*", guard)
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms(8000))
                if response is None or not 200 <= response.status < 300:
                    raise ValueError("Browser document did not return success")
                try:
                    page.wait_for_function("document.body && document.body.innerText.trim().length > 100", timeout=remaining_ms(8000))
                except BrowserTimeout:
                    pass  # A short page still goes through exact-identity assessment.
                assert_public_url(page.url)
                if _registered_domain(page.url) != _registered_domain(url):
                    raise ValueError("Browser final URL left candidate domain")
                document = response.body()
                markup = page.content().encode("utf-8")
                result = _rendered_website_record(page.url, markup, document, response.url)
                result["value"]["requested_url"] = url
                result["value"]["static_candidate"] = {"source_url": record.get("source_url"), "content_sha256": record.get("content_sha256"), "identity_assessment": value.get("identity_assessment")}
                metrics["bytes"] = len(document) + len(markup)  # Captured artifacts, not all browser transfer bytes.
                _fetch_rendered_identity_pages(result, metrics, deadline=deadline, max_requests=max_requests)
                metrics["bytes_scope"] = "http_document_rendered_dom_and_secondary_http_captures"
                result["value"]["browser_operations"] = metrics
                return result, metrics
            finally:
                browser.close()
    except Exception as exc:
        value["browser_error"] = {"status": "source_error", "error": type(exc).__name__, "source_url": url}
        record["value"] = value
        return record, metrics
    finally:
        if acquired:
            _BROWSER_SLOTS.release()
        metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))


def fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not _robots_allowed(normalized, timeout):
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(raw, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        identity_text = _identity_text(soup)
        social_captures = _page_social_captures(final_url, soup, _jsonld_organisations(structured))
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "identity_text_excerpt": identity_text,
            "social_links": [{"platform": item["platform"], "url": item["url"]} for item in social_captures],
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "extraction_state": _extraction_state(text, soup),
        }
        pages = [{"url": final_url, "title": title[:500], "main_text_excerpt": text[:5000], "identity_text_excerpt": identity_text, "content_sha256": value["content_sha256"], "structured_jobs": _jsonld_types(structured, {"JobPosting"}), "social_link_evidence": social_captures, "retrieved_at": utc_now(), "activity_links": _activity_links(final_url, soup), "publication_feed_links": _publication_feed_links(final_url, soup), "article": _article_capture(final_url, soup, structured)}]
        social = value["social_links"]
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = value["registered_domain"]
        visited = {final_url.rstrip("/")}
        for page_url in _priority_links(final_url, soup):
            if page_url.rstrip("/") in visited:
                continue
            visited.add(page_url.rstrip("/"))
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                if page["url"].rstrip("/") not in {item["url"].rstrip("/") for item in pages}:
                    pages.append(page)
                social.extend(page_social)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "blocked" if exc.code in {401, 403, 429} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes)
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
