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
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from .evidence import evidence, utc_now
from .identity import _tokens, exact_legal_name, normalize_organisation_number

logger = logging.getLogger(__name__)

NAV_STILLINGER_SEARCH_URL = "https://arbeidsplassen.nav.no/stillinger/api/search"
NAV_JOB_BASE_URL = "https://arbeidsplassen.nav.no/stillinger/stilling"
NAV_FEED_BASE = "https://pam-stilling-feed.nav.no"


def _nav_feed_url(reference: str) -> str:
    url = urllib.parse.urljoin(NAV_FEED_BASE + "/", reference)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "pam-stilling-feed.nav.no" or parsed.port not in (None, 443) or parsed.username or parsed.password or not parsed.path.startswith("/api/"):
        raise ValueError("NAV feed URL is outside the documented API origin")
    return url


class _NavFeedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _nav_feed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _public_nav_token(text: str) -> str:
    tokens = set(re.findall(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", text))
    if len(tokens) != 1:
        raise ValueError("Public token response does not contain one unambiguous JWT")
    return tokens.pop()


def _nav_feed_name_priority(name: Any, registered_names: set[str]) -> int | None:
    candidate = " ".join(_tokens(name))
    if candidate in registered_names:
        return 0
    # Discovery only: branch/advertised suffixes still require an exact employer ID.
    parts = candidate.split()
    if any(" ".join(parts[:length]) in registered_names for length in range(2, len(parts))):
        return 1
    return None


def fetch_nav_feed_snapshot(profiles: list[dict[str, Any]], *, token: str | None = None, days: int = 30, max_pages: int = 10, max_details: int = 30, max_seconds: float = 120, timeout: float = 15) -> dict[str, Any]:
    """Bounded shared feed discovery; absence never proves zero company vacancies.

    NAV explicitly provides a rotating public token for experiments. Passing a token
    instead supports a registered consumer. Neither credential is retained.
    """
    if not 1 <= days <= 184 or min(max_pages, max_details, max_seconds, timeout) <= 0:
        raise ValueError("NAV feed limits must be positive; days must be 1..184")
    started = time.monotonic()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    snapshot = {"hits": [], "status": "ambiguous", "access_mode": "registered_token" if token else "public_experiment", "cutoff": cutoff.isoformat(), "pages": 0, "entries": 0, "details": 0, "scan_complete": False, "candidate_filter": "exact_then_multitoken_prefix_hint_then_exact_employer_id", "metrics": {"requests": 0, "bytes": 0, "latencies_ms": []}}
    opener = urllib.request.build_opener(_NavFeedRedirect())

    def request(reference: str, *, authenticated: bool = True, since: bool = False) -> tuple[bytes, str]:
        remaining = max_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("NAV feed time budget exhausted")
        url = _nav_feed_url(reference)
        headers = {"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"}
        if authenticated:
            headers["Authorization"] = "Bearer " + str(token)
        if since:
            headers["If-Modified-Since"] = format_datetime(cutoff, usegmt=True)
        snapshot["metrics"]["requests"] += 1
        request_started = time.monotonic()
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=min(timeout, remaining)) as response:
                _nav_feed_url(response.geturl())
                raw = response.read(8_000_001)
                snapshot["metrics"]["bytes"] += len(raw)
                if len(raw) > 8_000_000:
                    raise ValueError("NAV response exceeds byte limit")
                return raw, url
        finally:
            snapshot["metrics"]["latencies_ms"].append(int((time.monotonic() - request_started) * 1000))

    try:
        if not token:
            raw, _ = request("/api/publicToken", authenticated=False)
            token = _public_nav_token(raw.decode("utf-8"))
        names = {
            " ".join(_tokens(name))
            for profile in profiles
            for name in [profile.get("name"), *[location.get("name") for location in (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations", [])]]
        }
        names.discard("")
        candidates: dict[str, tuple[int, dict[str, Any]]] = {}
        next_url = "/api/v1/feed"
        visited = set()
        for _ in range(max_pages):
            url = _nav_feed_url(next_url)
            if url in visited:
                raise ValueError("NAV feed pagination cycle")
            visited.add(url)
            raw, _ = request(url, since=True)
            page = json.loads(raw)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ValueError("NAV feed page has an invalid schema")
            snapshot["pages"] += 1
            for item in page["items"]:
                entry = item.get("_feed_entry") or {}
                uuid = entry.get("uuid")
                snapshot["entries"] += 1
                if entry.get("status") == "INACTIVE":
                    candidates.pop(uuid, None)
                elif uuid:
                    priority = _nav_feed_name_priority(entry.get("businessName"), names)
                    if priority is not None:
                        candidates[uuid] = (priority, item)
            next_url = page.get("next_url")
            if not next_url:
                snapshot["scan_complete"] = True
                break
        for uuid, (_, item) in sorted(candidates.items(), key=lambda candidate: candidate[1][0])[:max_details]:
            raw, url = request(item["url"])
            detail = json.loads(raw)
            snapshot["details"] += 1
            if detail.get("uuid") != uuid:
                raise ValueError("NAV vacancy UUID differs from feed entry")
            if detail.get("status") == "INACTIVE":
                continue
            if detail.get("status") != "ACTIVE":
                raise ValueError("NAV vacancy has an invalid status")
            # Live responses use ad_content; older documented responses use json.
            ad = detail.get("ad_content", detail.get("json"))
            if not isinstance(ad, dict):
                raise ValueError("NAV vacancy has an invalid schema")
            ad = {key: ad.get(key) for key in ("uuid", "title", "employer", "published", "expires", "source", "workLocations")}
            ad.update({"uuid": uuid, "status": "ACTIVE", "locationList": ad.pop("workLocations") or [], "_content_sha256": hashlib.sha256(raw).hexdigest(), "_source_url": url, "_retrieved_at": utc_now()})
            snapshot["hits"].append(ad)
        snapshot["candidate_count"] = len(candidates)
        snapshot["details_complete"] = len(candidates) <= max_details
    except urllib.error.HTTPError as exc:
        snapshot["status"] = "blocked" if exc.code in {401, 403, 429} else "source_error"
        snapshot["error"] = f"HTTP {exc.code}"
    except Exception as exc:
        snapshot["status"] = "source_error"
        snapshot["error"] = type(exc).__name__  # Never include a credential-bearing request/header.
    return snapshot

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
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Query NAV Arbeidsplassen public search API for job vacancies.
    
    NAV Arbeidsplassen is Norway's official public employment platform (NLOD open data).
    Returns (hits, status, metrics) where status is 'ok', 'blocked', 'failed', or 'not_applicable'.
    """
    clean_name = _clean_company_name(company_name)
    if not clean_name:
        return [], "not_applicable", {"requests": 0, "bytes": 0, "latencies_ms": []}
    
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
    
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}

    for attempt in range(2):
        metrics["requests"] += 1
        _reserve_nav_slot()
        request = urllib.request.Request(url, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                elapsed_ms = int((time.monotonic() - started) * 1000)
                metrics["bytes"] = len(raw)
                metrics["content_sha256"] = hashlib.sha256(raw).hexdigest()
                metrics["source_url"] = url
                metrics["retrieved_at"] = utc_now()
                metrics["latencies_ms"].append(elapsed_ms)
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                hits = (payload.get("hits") or {}).get("hits") or []
                return hits, "ok", metrics
        except urllib.error.HTTPError as exc:
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
            if exc.code == 429 and attempt == 0:
                time.sleep(1.5)
                continue
            logger.debug("NAV jobs HTTP %s for %s", exc.code, company_name)
            if exc.code in (403, 429):
                return [], "blocked", metrics
            return [], "failed", metrics
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
            logger.debug("NAV jobs error for %s: %s", company_name, exc)
            return [], "failed", metrics

    return [], "failed", metrics


SUBSIDIARY_MODIFIERS = {
    "eiendom", "holding", "invest", "forsikring", "pensjon", "kapital", "finans",
    "drift", "service", "bygg", "anlegg", "transport", "logistikk", "consulting",
    "partner", "handel", "butikk", "norge", "norway", "nordic", "gruppen",
    "group", "eiendommer", "bolig", "prosjekt", "kraft", "energi", "revisjon",
    "advokat", "megler", "forvaltning",
}


def match_and_filter_jobs(
    profile: dict[str, Any],
    hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strictly filter job hits to ensure 100% exact-entity match."""
    core = _tokens(profile.get("name"))
    if not core:
        return []
    
    org_number = str(profile.get("organisation_number") or "").strip()
    allowed_orgs = {org_number}
    location_record = profile.get("evidence", {}).get("locations", {})
    for location in (location_record.get("value") or {}).get("locations", []) if location_record.get("status") == "available" else []:
        subunit = normalize_organisation_number(location.get("organisation_number"))
        if subunit:
            allowed_orgs.add(subunit)
    verified_jobs = []

    for hit in hits:
        source = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
        uuid = source.get("uuid")
        if not uuid:
            continue
        if source.get("status") and source["status"] != "ACTIVE":
            continue

        # 1. Filter out expired job vacancies
        expires = source.get("expires")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt < datetime.now(timezone.utc):
                    continue
            except (ValueError, TypeError, AttributeError):
                continue

        # 2. Check employer organisation number if present in hit metadata
        employer_dict = source.get("employer") or {}
        props = source.get("properties") or {}
        employer_numbers = [employer_dict.get("orgnr"), props.get("employerorgnr"), props.get("orgnr")]
        supplied_numbers = [value for value in employer_numbers if value not in (None, "")]
        if supplied_numbers:
            if any(normalize_organisation_number(value) not in allowed_orgs for value in supplied_numbers):
                continue

        # 3. Match candidate names (businessName or employer name)
        business_name = source.get("businessName") or ""
        employer_name = employer_dict.get("name") or ""
        candidate_names = [business_name, employer_name]

        # Names alone cannot disambiguate legal entities with identical registered names.
        exact_match = bool(supplied_numbers)

        if not exact_match:
            continue

        locations = source.get("locationList") or []
        city = locations[0].get("city") or locations[0].get("municipal") if locations else None

        title = source.get("title") or "Uspesifisert stilling"
        published = source.get("published")
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
        content_hash = source.get("_content_sha256") or hashlib.sha256(content_str.encode("utf-8")).hexdigest()

        verified_jobs.append({
            "id": f"nav-job-{uuid}",
            "organisation_number": org_number,
            "platform": "job_board",
            "signal_type": "job_posting",
            "source_url": source.get("_source_url") or job_url,
            "retrieved_at": source.get("_retrieved_at") or utc_now(),
            "content_sha256": content_hash,
            "exact_entity": True,
            "identity_proof": [
                {"type": "employer_organisation_number" if supplied_numbers else "exact_ordered_legal_name", "value": supplied_numbers or business_name or employer_name},
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
                "vacancy_url": job_url,
            },
        })

    return verified_jobs


def extract_brreg_workforce_snapshot(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Emit an official workforce_snapshot observation if registered employees count > 0."""
    records = profile.get("evidence") or {}
    org = str(profile.get("organisation_number") or "").strip()
    record = None
    for module, key, identity_key in (("registry_live", "employees", "organisation_number"), ("registry", "antallAnsatte", "organisasjonsnummer")):
        candidate = records.get(module) or {}
        value = candidate.get("value") or {}
        if candidate.get("status") == "available" and str(value.get(identity_key) or "") == org and value.get(key) not in (None, ""):
            record, emp_val, extraction_key = candidate, value[key], key
            break
    if record is None or not record.get("retrieved_at") or len(str(record.get("content_sha256") or "")) != 64:
        return None
    try:
        count = int(emp_val) if emp_val is not None else 0
    except (ValueError, TypeError):
        count = 0
    if count <= 0:
        return None
    source_url = record.get("source_url")
    if not source_url:
        return None
    return {
        "id": f"brreg-workforce-{org}",
        "organisation_number": org,
        "platform": "brreg",
        "signal_type": "workforce_snapshot",
        "source_url": source_url,
        "retrieved_at": record["retrieved_at"],
        "content_sha256": record["content_sha256"],
        "exact_entity": True,
        "identity_proof": [
            {"type": "official_brreg_register_record", "value": org},
        ],
        "acquisition_mode": "official_api",
        "rights_status": "approved",
        "source_class": "official_registry",
        "evidence_span": json.dumps(emp_val, ensure_ascii=False),
        "extraction_path": [extraction_key],
        "extraction_method": "registry_employee_count_integer",
        "metrics": {"employees": count, "workforce_value": count, "measure": "employees"},
        "strategy": "official_registry_workforce",
    }


def extract_structured_site_jobs(profile: dict[str, Any]) -> list[dict[str, Any]]:
    website = profile.get("evidence", {}).get("website") or {}
    value = website.get("value") or {}
    if website.get("status") != "available" or not (value.get("identity_assessment") or {}).get("publishable"):
        return []
    observations = []
    for page in value.get("pages", []):
        for job in page.get("structured_jobs", []):
            hiring = job.get("hiringOrganization") or {}
            if not isinstance(hiring, dict) or not exact_legal_name(profile.get("name"), hiring.get("legalName") or hiring.get("name")):
                continue
            # A real vacancy requires explicit structured content and a current deadline.
            if not job.get("title") or not job.get("description") or not job.get("datePosted") or not job.get("validThrough"):
                continue
            try:
                expiry = datetime.fromisoformat(str(job["validThrough"]).replace("Z", "+00:00"))
                posted = datetime.fromisoformat(str(job["datePosted"]).replace("Z", "+00:00"))
                expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
                posted = posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)
                if expiry < datetime.now(timezone.utc) or posted > datetime.now(timezone.utc):
                    continue
            except ValueError:
                continue
            digest = page.get("content_sha256")
            if not digest or len(digest) != 64:
                continue
            identifier = hashlib.sha256(json.dumps(job, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
            observations.append({"id": "site-job-" + identifier, "organisation_number": profile["organisation_number"],
                                 "platform": "company_site", "signal_type": "job_posting", "source_url": page["url"],
                                 "retrieved_at": website["retrieved_at"], "published_at": job["datePosted"],
                                 "content_sha256": digest, "exact_entity": True,
                                 "identity_proof": [{"type": "verified_website_and_exact_hiring_legal_name", "value": hiring}],
                                 "acquisition_mode": "permitted_public_page", "rights_status": "approved", "source_class": "company_owned",
                                 "evidence_span": json.dumps(job, sort_keys=True, ensure_ascii=False),
                                 "metrics": {"title": job["title"], "published": job["datePosted"], "expires": job["validThrough"],
                                             "type": "schema_org_JobPosting"}})
    return list({row["id"]: row for row in observations}.values())


def fetch_jobs_module(
    profile: dict[str, Any],
    *,
    timeout: float = 8.0,
    feed_snapshot: dict[str, Any] | None = None,
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

    if feed_snapshot is None:
        hits, nav_status, metrics = query_nav_jobs(company_name, timeout=timeout)
    else:
        hits = feed_snapshot.get("hits") or []
        nav_status = "ok" if hits else "failed" if feed_snapshot.get("status") == "source_error" else feed_snapshot.get("status", "ambiguous")
        metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}  # Shared requests counted once by batch.
        if feed_snapshot.get("error"):
            profile.setdefault("connector_errors", []).append({"connector": "nav_feed", "error": feed_snapshot["error"]})
    
    clean_name = _clean_company_name(company_name)
    source_url = NAV_FEED_BASE + "/api/v1/feed" if feed_snapshot is not None else f"{NAV_STILLINGER_SEARCH_URL}?q={urllib.parse.quote(clean_name)}"
    retrieved_at = utc_now()
    if feed_snapshot is None and nav_status == "ok":
        source_url = metrics.get("source_url") or source_url
        retrieved_at = metrics.get("retrieved_at") or retrieved_at
        digest = str(metrics.get("content_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            retained = []
            for hit in hits:
                source = dict(hit.get("_source") if isinstance(hit.get("_source"), dict) else hit)
                source.update({"_content_sha256": digest, "_source_url": source_url, "_retrieved_at": retrieved_at})
                retained.append({"_source": source})
            hits = retained
        else:
            hits = []  # No source-response provenance means no published NAV vacancy.

    # Discover official registered workforce count
    workforce_obs = extract_brreg_workforce_snapshot(profile)
    all_observations = [workforce_obs] if workforce_obs else []
    site_jobs = extract_structured_site_jobs(profile)
    all_observations.extend(site_jobs)

    if nav_status in {"blocked", "failed"} and site_jobs:
        profile.setdefault("connector_errors", []).append({"connector": "nav", "error": nav_status})
        verified_jobs = site_jobs
    elif nav_status == "blocked":
        record = evidence(
            "jobs",
            "blocked",
            "official_nav_arbeidsplassen_api",
            source_url,
            retrieved_at=retrieved_at,
            note="NAV Arbeidsplassen public search rate limit or access blocked (HTTP 429/403)",
        )
        return record, all_observations, metrics
    
    elif nav_status == "failed":
        record = evidence(
            "jobs",
            "source_error",
            "official_nav_arbeidsplassen_api",
            source_url,
            retrieved_at=retrieved_at,
            note="NAV Arbeidsplassen search request failed or timed out",
        )
        return record, all_observations, metrics

    # nav_status == "ok"
    else:
        verified_jobs = match_and_filter_jobs(profile, hits) + site_jobs
        all_observations = [o for o in all_observations if o["signal_type"] != "job_posting"] + verified_jobs

    if verified_jobs:
        content_hash = hashlib.sha256(
            json.dumps([{ "id": j["id"], "metrics": j["metrics"], "content_sha256": j["content_sha256"]} for j in sorted(verified_jobs, key=lambda row: row["id"])], sort_keys=True).encode()
        ).hexdigest()
        record = evidence(
            "jobs",
            "available",
            "verified_job_sources",
            source_url if any(j["platform"] == "job_board" for j in verified_jobs) else website_url_for_jobs(profile),
            value={
                "job_count": len(verified_jobs),
                "job_count_scope": "observed_verified_vacancies",
                "active_postings": [
                    {
                        "id": j["id"],
                        "title": j["metrics"]["title"],
                        "url": j["metrics"].get("vacancy_url") or j["source_url"],
                        "published": j["metrics"].get("published"),
                        "expires": j["metrics"].get("expires"),
                        "location": j["metrics"].get("city"),
                    }
                    for j in verified_jobs
                ],
            },
            retrieved_at=retrieved_at,
            content_sha256=content_hash,
        )
        if feed_snapshot is not None:
            record["value"]["feed_scope"] = {key: feed_snapshot.get(key) for key in ("access_mode", "cutoff", "scan_complete", "pages", "entries", "details", "details_complete")}
            record["note"] = "Observed verified vacancies only; bounded name-hint feed discovery does not establish the total company vacancy count."
    elif feed_snapshot is not None:
        record = evidence("jobs", "ambiguous", "official_nav_vacancy_feed", source_url, retrieved_at=retrieved_at,
                          note="No verified vacancy in bounded name-hint feed discovery; older ads, brands and unscanned pages remain unknown. Zero vacancies is not asserted.")
    else:
        record = evidence(
            "jobs",
            "ambiguous",
            "official_nav_arbeidsplassen_api",
            source_url,
            retrieved_at=retrieved_at,
            content_sha256=metrics.get("content_sha256"),
            note="No verified vacancy in bounded legal-name search; alternate brands and unreturned results remain unknown. Zero vacancies is not asserted.",
        )

    return record, all_observations, metrics


def website_url_for_jobs(profile: dict[str, Any]) -> str:
    website = profile["evidence"]["website"]
    return (website.get("value") or {}).get("final_url") or website["source_url"]
