from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .evidence import evidence, utc_now
from .identity import _tokens

logger = logging.getLogger(__name__)

NAV_STILLINGER_SEARCH_URL = "https://arbeidsplassen.nav.no/stillinger/api/search"
NAV_JOB_BASE_URL = "https://arbeidsplassen.nav.no/stillinger/stilling"

_nav_lock = threading.Lock()
_nav_last_request = 0.0
_MIN_NAV_INTERVAL = 0.4  # Pacing to stay comfortably below rate limit


def _clean_company_name(name: str) -> str:
    """Extract primary legal name tokens without corporate suffixes for search."""
    cleaned = re.sub(r"\b(ASA|AS|ANS|DA|ENK|IKS|SA|NUF)\b", "", name, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()


def _reserve_nav_slot() -> None:
    global _nav_last_request
    with _nav_lock:
        now = time.monotonic()
        delay = _MIN_NAV_INTERVAL - (now - _nav_last_request)
        if delay > 0:
            time.sleep(delay)
        _nav_last_request = time.monotonic()


def query_nav_jobs(
    company_name: str,
    *,
    timeout: float = 8.0,
    max_results: int = 15,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Query NAV Arbeidsplassen public search API for job vacancies.
    
    NAV Arbeidsplassen is Norway's official public employment platform (NLOD open data).
    """
    clean_name = _clean_company_name(company_name)
    if not clean_name:
        return [], {"requests": 0, "bytes": 0, "latencies_ms": []}
    
    query_str = f'"{clean_name}"' if " " in clean_name else clean_name
    query_params = urllib.parse.urlencode({
        "q": query_str,
        "size": max_results,
    })
    url = f"{NAV_STILLINGER_SEARCH_URL}?{query_params}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://arbeidsplassen.nav.no/stillinger",
    }
    
    metrics = {"requests": 1, "bytes": 0, "latencies_ms": []}

    for attempt in range(2):
        _reserve_nav_slot()
        request = urllib.request.Request(url, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                elapsed_ms = int((time.monotonic() - started) * 1000)
                metrics["bytes"] = len(raw)
                metrics["latencies_ms"].append(elapsed_ms)
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                hits = (payload.get("hits") or {}).get("hits") or []
                return hits, metrics
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                time.sleep(1.5)
                continue
            logger.debug("NAV jobs HTTP %s for %s", exc.code, company_name)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.debug("NAV jobs error for %s: %s", company_name, exc)
            break

    return [], metrics


def match_and_filter_jobs(
    profile: dict[str, Any],
    hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strictly filter job hits to ensure 100% exact-entity match."""
    core = _tokens(profile.get("name"))
    if not core:
        return []
    
    org_number = str(profile.get("organisation_number") or "").strip()
    verified_jobs = []

    for hit in hits:
        source = hit.get("_source") or {}
        uuid = source.get("uuid")
        if not uuid:
            continue

        business_name = source.get("businessName") or ""
        employer_name = (source.get("employer") or {}).get("name") or ""
        candidate_names = [business_name, employer_name]

        exact_match = False
        for cand in candidate_names:
            cand_tokens = set(_tokens(cand))
            if set(core).issubset(cand_tokens):
                exact_match = True
                break

        if not exact_match:
            continue

        locations = source.get("locationList") or []
        city = locations[0].get("city") or locations[0].get("municipal") if locations else None

        title = source.get("title") or "Uspesifisert stilling"
        published = source.get("published")
        expires = source.get("expires")
        job_url = f"{NAV_JOB_BASE_URL}/{uuid}"

        evidence_span = f"{title} — {business_name or employer_name}"
        if city:
            evidence_span += f" — {city}"

        content_str = json.dumps({
            "uuid": uuid,
            "title": title,
            "businessName": business_name,
            "employer": employer_name,
            "published": published,
            "expires": expires,
            "locations": locations,
        }, sort_keys=True)
        content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

        verified_jobs.append({
            "id": f"nav-job-{uuid}",
            "organisation_number": org_number,
            "platform": "job_board",
            "signal_type": "job_posting",
            "source_url": job_url,
            "retrieved_at": utc_now(),
            "content_sha256": content_hash,
            "exact_entity": True,
            "identity_proof": [
                {"type": "exact_business_name_token_match", "value": business_name or employer_name},
                {"type": "official_nav_stillinger_record", "uuid": uuid},
            ],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "job_board",
            "evidence_span": evidence_span,
            "metrics": {
                "uuid": uuid,
                "title": title,
                "business_name": business_name,
                "published": published,
                "expires": expires,
                "source": source.get("source"),
                "city": city,
            },
        })

    return verified_jobs


def extract_career_page_jobs(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract verified hiring signals from the company's verified own website."""
    website = profile.get("evidence", {}).get("website", {})
    if website.get("status") != "available":
        return []
    value = website.get("value") or {}
    assessment = value.get("identity_assessment") or {}
    if not assessment.get("publishable", False):
        return []
    
    pages = value.get("pages") or []
    career_terms = {"karriere", "career", "careers", "jobb", "stillinger", "jobs", "ledige-stillinger"}
    observations = []
    org_number = str(profile.get("organisation_number") or "").strip()

    for page in pages:
        url = str(page.get("url") or "")
        title = str(page.get("title") or "")
        haystack = (url + " " + title).casefold()
        if any(term in haystack for term in career_terms):
            content_hash = hashlib.sha256(f"{url}|{title}".encode("utf-8")).hexdigest()
            observations.append({
                "id": f"careers-{content_hash[:20]}",
                "organisation_number": org_number,
                "platform": "company_site",
                "signal_type": "job_posting",
                "source_url": url,
                "retrieved_at": utc_now(),
                "content_sha256": content_hash,
                "exact_entity": True,
                "identity_proof": [
                    {"type": "verified_company_website_career_page", "value": url}
                ],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "company_owned",
                "evidence_span": f"{title} — {profile.get('name')}",
                "metrics": {
                    "title": title,
                    "url": url,
                    "type": "career_page",
                },
            })

    return observations


def extract_brreg_workforce_snapshot(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Emit an official workforce_snapshot observation if registered employees count > 0."""
    emp_val = profile.get("employees")
    if emp_val is None:
        reg_val = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
        emp_val = reg_val.get("antallAnsatte")
    try:
        count = int(emp_val) if emp_val is not None else 0
    except (ValueError, TypeError):
        count = 0
    if count <= 0:
        return None
    org = str(profile.get("organisation_number") or "").strip()
    source_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
    content_str = json.dumps({"org": org, "employees": count}, sort_keys=True)
    digest = hashlib.sha256(content_str.encode("utf-8")).hexdigest()
    return {
        "id": f"brreg-workforce-{org}",
        "organisation_number": org,
        "platform": "brreg",
        "signal_type": "workforce_snapshot",
        "source_url": source_url,
        "retrieved_at": utc_now(),
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": [
            {"type": "official_brreg_register_record", "value": org},
        ],
        "acquisition_mode": "official_api",
        "rights_status": "approved",
        "source_class": "official_registry",
        "evidence_span": f"Official registered employees count: {count}",
        "metrics": {"employees": count, "workforce_value": count, "measure": "employees"},
        "strategy": "official_registry_workforce",
    }


def fetch_jobs_module(
    profile: dict[str, Any],
    *,
    timeout: float = 8.0,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Fetch and return jobs evidence module, individual observations, and network metrics."""
    company_name = profile.get("name") or ""
    if not company_name:
        record = evidence(
            "jobs",
            "not_applicable",
            "official_nav_arbeidsplassen_api",
            NAV_STILLINGER_SEARCH_URL,
            note="No legal company name available to query jobs",
        )
        return record, [], {"requests": 0, "bytes": 0, "latencies_ms": []}

    hits, metrics = query_nav_jobs(company_name, timeout=timeout)
    verified = match_and_filter_jobs(profile, hits)
    
    # Also discover career pages from verified website
    career_obs = extract_career_page_jobs(profile)
    hiring_observations = verified + career_obs

    # Discover official registered workforce count
    workforce_obs = extract_brreg_workforce_snapshot(profile)
    all_observations = list(hiring_observations)
    if workforce_obs:
        all_observations.append(workforce_obs)

    retrieved_at = utc_now()
    clean_name = _clean_company_name(company_name)
    source_url = f"{NAV_STILLINGER_SEARCH_URL}?q={urllib.parse.quote(clean_name)}"

    if hiring_observations:
        content_hash = hashlib.sha256(
            json.dumps([j["id"] for j in hiring_observations], sort_keys=True).encode()
        ).hexdigest()
        record = evidence(
            "jobs",
            "available",
            "official_nav_arbeidsplassen_api",
            source_url,
            value={
                "job_count": len(hiring_observations),
                "active_postings": [
                    {
                        "id": j["id"],
                        "title": j["metrics"]["title"],
                        "url": j["source_url"],
                        "published": j["metrics"].get("published"),
                        "expires": j["metrics"].get("expires"),
                        "location": j["metrics"].get("city"),
                    }
                    for j in hiring_observations
                ],
            },
            retrieved_at=retrieved_at,
            content_sha256=content_hash,
        )
    else:
        record = evidence(
            "jobs",
            "not_found",
            "official_nav_arbeidsplassen_api",
            source_url,
            value={"job_count": 0, "active_postings": []},
            retrieved_at=retrieved_at,
            note="No active job postings found matching exact entity name on Arbeidsplassen",
        )

    return record, all_observations, metrics
