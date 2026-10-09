#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse


NEWS_PATH = re.compile(r"/(?:news|press|aktuelt|nyheter|artikler|blog|blogs|blogg|inspiration|inspirasjon|journal)(?:/|$)", re.I)


def observations(profile: dict) -> list[dict]:
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not identity.get("publishable"):
        return []
    pages = [
        page for page in (value.get("pages") or [])
        if (NEWS_PATH.search(urlparse(str(page.get("url") or "")).path) or
            (NEWS_PATH.search(urlparse(str(page.get("activity_discovered_from") or "")).path) and
             urlparse(str(page.get("activity_discovered_from"))).hostname == urlparse(str(page.get("url") or "")).hostname)) and page.get("article")
    ]
    found = {}
    for page in pages:
        item = _page_observation(profile, website, identity, page)
        if item:
            found[item["id"]] = item
    homepage_host = urlparse(str(value.get("final_url") or website.get("source_url") or "")).netloc
    for feed in value.get("publication_feeds") or []:
        if not homepage_host or urlparse(str(feed.get("url") or "")).netloc != homepage_host:
            continue
        for publication in feed.get("items") or []:
            publication_url = str(publication.get("publication_url") or "")
            if urlparse(publication_url).netloc != homepage_host:
                continue
            page = {**feed, "article": {**publication, "date_extraction_method": "company_rss_pubDate_v1"}}
            item = _page_observation(profile, website, identity, page)
            if item:
                item["id"] = "company-site-news-" + hashlib.sha256(f"{profile['organisation_number']}|{publication_url}".encode()).hexdigest()[:24]
                item["date_evidence_span"] = publication["date_evidence_span"]
                item["metrics"]["publication_url"] = publication_url
                found[item["id"]] = item
    return sorted(found.values(), key=lambda item: (item["published_at"], item["source_url"]), reverse=True)


def observation(profile: dict) -> dict | None:
    return next(iter(observations(profile)), None)


def _page_observation(profile: dict, website: dict, identity: dict, page: dict) -> dict | None:
    article = page["article"]
    published = str(article.get("published_at") or "")
    retrieved = page.get("retrieved_at") or website.get("retrieved_at")
    try:
        date = datetime.fromisoformat(published.replace("Z", "+00:00"))
        cutoff = datetime.fromisoformat(str(retrieved).replace("Z", "+00:00"))
        if date.replace(tzinfo=date.tzinfo or timezone.utc) > cutoff.replace(tzinfo=cutoff.tzinfo or timezone.utc):
            return None
    except (ValueError, TypeError):
        return None
    url = str(page.get("url") or "")
    digest = str(page.get("content_sha256") or "")
    if not url.startswith(("http://", "https://")) or len(digest) != 64:
        return None
    org = str(profile["organisation_number"])
    title = str(article.get("title") or "").strip()
    if not title or not article.get("date_extraction_method"):
        return None
    return {
        "id": "company-site-news-" + hashlib.sha256(f"{org}|{url}".encode()).hexdigest()[:24],
        "organisation_number": org,
        "platform": "company_site",
        "signal_type": "public_post",
        "source_url": url,
        "retrieved_at": retrieved,
        "published_at": published,
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": [{"type": "website_identity_gate", "score": identity.get("score"), "method": identity.get("method")}],
        "acquisition_mode": "permitted_public_page",
        "rights_status": "approved",
        "source_class": "company_site",
        "evidence_span": title[:1200],
        "date_evidence_span": published,
        "extraction_method": article["date_extraction_method"],
        "metrics": {"title": title, "published_at": published, "interpretation": "Company-owned dated publication; not independently verified event facts or sentiment."},
        "strategy": "company_site_dated_activity_v2",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract company-owned activity from exact-site bounded news pages.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    profiles = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [item for profile in profiles for item in observations(profile)]
    Path(args.output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    report = {
        "connector": "exact_company_site_dated_activity_v2",
        "profiles": len(profiles),
        "companies_with_activity": len({item["organisation_number"] for item in rows}),
        "observations": len(rows),
        "claim_boundary": "Company-owned activity only; never treated as independent sentiment.",
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
