#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
import time
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.external_footprint import aggregate_footprint, publishable_observation  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate, assess_social_identity  # noqa: E402
from norway_company_agent.jobs import fetch_jobs_module, fetch_nav_feed_snapshot  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.refresh import refresh_profile  # noqa: E402
from norway_company_agent.discovery import choose_search_candidate  # noqa: E402
from norway_company_agent.website import fetch_website, fetch_site_activity_pages, fetch_rendered_website  # noqa: E402
from scripts.extract_company_site_activity import observation as site_activity_obs  # noqa: E402
from scripts.extract_company_site_news import observations as site_news_obs  # noqa: E402
from scripts.run_google_news_rss_connector import fetch as fetch_google_news  # noqa: E402
from scripts.run_fagfolkguiden_reviews_connector import fetch as fetch_fagfolk  # noqa: E402
from scripts.run_brave_discovery import brave_search  # noqa: E402

LEGAL_SUFFIXES = {"as", "asa", "ans", "da", "enk", "iks", "sa", "nuf", "holding", "eiendom", "invest", "gruppen", "group"}


def generate_domain_candidates(name: str) -> list[str]:
    cleaned = re.sub(r"\b(ASA|AS|ANS|DA|ENK|IKS|SA|NUF)\b", "", name, flags=re.IGNORECASE).strip()
    text = cleaned.translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        return []
    candidates = []
    candidates.append("https://" + "".join(words) + ".no")
    if len(words) > 1:
        candidates.append("https://" + "-".join(words) + ".no")
    non_generic = [w for w in words if w not in LEGAL_SUFFIXES]
    if non_generic and len(non_generic) < len(words):
        candidates.append("https://" + "".join(non_generic) + ".no")
    candidates.append("https://" + "".join(words) + ".com")
    return list(dict.fromkeys(candidates))


def generate_alternative_website_candidates(name: str) -> list[str]:
    cleaned = re.sub(r"\s+(?:ASA|AS|ANS|DA|ENK|IKS|SA|NUF)\s*$", "", name, flags=re.I).strip()
    text = cleaned.translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    if len(words) >= 3 and words[-2:] == ["fus", "barnehage"]:
        brand = words[:-2]
        return list(dict.fromkeys("https://fus.no/" + slug for slug in ("".join(brand), "-".join(brand))))
    candidates = []
    if len(words) >= 2 and words[-1] == "media" and len("".join(words[:-1])) >= 5:
        candidates.append("https://" + "".join(words[:-1]) + ".no")
    if 3 <= len(words) <= 5:
        candidates.append("https://" + "".join(word[0] for word in words) + ".no")
    return candidates[:2]


def generate_website_candidates(profile: dict) -> list[str]:
    candidates = []
    public_mail_hosts = {"gmail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "online.no", "frisurf.no", "start.no", "proton.me", "protonmail.com"}
    registry = (profile.get("evidence", {}).get("registry") or {})
    live = profile.get("evidence", {}).get("registry_live") or {}
    emails = []
    if live.get("status") == "available" and str((live.get("value") or {}).get("organisation_number")) == str(profile.get("organisation_number")):
        emails.append((live.get("value") or {}).get("email"))
    # A registered email domain is a discovery hint, never an identity proof.
    if registry.get("status") == "available":
        emails.append((registry.get("value") or {}).get("epostadresse"))
    for email in emails[:2]:
        email = str(email or "").strip()
        matched = re.fullmatch(r"[^\s@]+@([a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?)", email)
        if matched:
            host = matched.group(1).casefold()
            if "." in host and host not in public_mail_hosts:
                candidates.append("https://" + host + "/")
    name = profile.get("name") or ""
    domains = generate_domain_candidates(name)
    candidates.extend(domains[:1])
    candidates.extend(generate_alternative_website_candidates(name))
    candidates.extend(domains[1:])
    return list(dict.fromkeys(candidate.rstrip("/") for candidate in candidates))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def site_social_observations(profile: dict) -> list[dict]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    if website.get("status") != "available" or not (value.get("identity_assessment") or {}).get("publishable"):
        return []
    accepted = {(item["platform"], item["url"]) for item in value.get("social_links") or []}
    found = {}
    org = str(profile["organisation_number"])
    for page in value.get("pages") or []:
        digest = str(page.get("content_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not page.get("url"):
            continue
        for capture in page.get("social_link_evidence") or []:
            key = (capture.get("platform"), capture.get("url"))
            if key not in accepted or key in found or not capture.get("claim_span"):
                continue
            assessment = assess_social_identity(profile, capture)
            if not assessment["publishable"]:
                continue
            item = {
                "id": "social-" + hashlib.sha256(f"{org}|{key[1]}".encode()).hexdigest()[:20],
                "organisation_number": org, "platform": key[0], "signal_type": "profile_handle",
                "source_url": page["url"], "retrieved_at": website.get("retrieved_at"), "content_sha256": digest,
                "exact_entity": True,
                "identity_proof": [{"type": "company_site_crosslink_and_handle_match", "value": key[1], "score": assessment["identity_score"]}],
                "acquisition_mode": "permitted_public_page", "rights_status": "approved", "source_class": "company_site",
                "evidence_span": capture["claim_span"], "extraction_method": capture.get("extraction_method"),
                "metrics": {"platform": key[0], "url": key[1], "relationship": "company_site_declared_link", "destination_verified": False}, "strategy": "verified_handle_extraction_v2",
            }
            if publishable_observation(item):
                found[key] = item
    return list(found.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, help="Optional assertion; defaults to supplied batch size")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--previous-profiles", help="Previous profile JSONL for live refresh and retained evidence")
    parser.add_argument("--experimental-connectors", action="store_true", help="Run quarantined news/review discovery; these observations cannot be published")
    parser.add_argument("--search-limit", type=int, default=0, help="Maximum transient Brave discovery requests (requires BRAVE_SEARCH_API_KEY; disabled by default)")
    parser.add_argument("--nav-feed", choices=("off", "public", "token"), default="off", help="Opt-in documented NAV feed; public uses NAV's experimental token, token requires NAV_FEED_TOKEN")
    parser.add_argument("--nav-feed-days", type=int, default=30)
    parser.add_argument("--nav-feed-pages", type=int, default=10)
    parser.add_argument("--nav-feed-details", type=int, default=30)
    parser.add_argument("--nav-feed-seconds", type=float, default=120)
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,jobs")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if args.expected_count is None:
        args.expected_count = len(orgs)
    if not orgs or args.workers < 1 or args.checkpoint_every < 1:
        parser.error("Supply a nonempty batch, positive workers and checkpoint interval")
    if args.resume and args.previous_profiles:
        parser.error("Refresh requires fresh fetches; --resume cannot be combined with --previous-profiles")
    search_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if args.search_limit < 0 or (args.search_limit and not search_key):
        parser.error("A positive search limit requires BRAVE_SEARCH_API_KEY")
    if not 1 <= args.nav_feed_days <= 184 or min(args.nav_feed_pages, args.nav_feed_details, args.nav_feed_seconds) <= 0:
        parser.error("NAV feed limits must be positive; days must be 1..184")
    if args.nav_feed == "token" and not os.environ.get("NAV_FEED_TOKEN", "").strip():
        parser.error("Registered NAV feed requires NAV_FEED_TOKEN")
    if args.nav_feed != "off" and args.resume:
        parser.error("NAV feed requires a fresh run; --resume cannot reuse potentially inactive vacancies")
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    supported_modules = {"registry", "accounting_obligation", "registry_live", "financials", "financial_history", "roles", "group", "locations", "website", "jobs"}
    if set(requested_modules) - supported_modules or not requested_modules:
        parser.error("Unknown or empty module list")
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "jobs"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}
    nav_feed_snapshot = None
    prefetched_locations = {}
    if args.nav_feed != "off" and "jobs" in requested_modules:
        if "locations" in fetch_modules:
            # NAV commonly identifies a workplace/subunit rather than its parent company.
            # Fetch relationships before building name hints, and reuse these requests.
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                location_futures = {executor.submit(fetch_official_modules, profile["organisation_number"], {"locations"}): profile for profile in profiles}
                for future in as_completed(location_futures):
                    profile = location_futures[future]
                    try:
                        location_records, location_metrics = future.result()
                    except Exception as exc:
                        location_records = {"locations": evidence("locations", "source_error", "official_subunits", "https://data.brreg.no/enhetsregisteret/api/underenheter", note=type(exc).__name__)}
                        location_metrics = []
                    prefetched_locations[profile["organisation_number"]] = (location_records, location_metrics)
                    profile["evidence"].update(location_records)
        nav_feed_snapshot = fetch_nav_feed_snapshot(profiles, token=os.environ.get("NAV_FEED_TOKEN", "").strip() if args.nav_feed == "token" else None,
                                                   days=args.nav_feed_days, max_pages=args.nav_feed_pages, max_details=args.nav_feed_details, max_seconds=args.nav_feed_seconds)
        for key in operations:
            operations[key] = nav_feed_snapshot["metrics"][key]
    previous = {}
    search_lock = threading.Lock()
    search_count = 0
    search_last_started = 0.0
    if args.previous_profiles:
        for line in Path(args.previous_profiles).read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item["organisation_number"] in previous:
                    parser.error("Previous profiles contain duplicate organisation numbers")
                previous[item["organisation_number"]] = item

    def enrich(profile: dict) -> tuple[dict, dict]:
        nonlocal search_count, search_last_started
        company_started = time.monotonic()
        preloaded = prefetched_locations.get(profile["organisation_number"])
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules - {"locations"} if preloaded is not None else fetch_modules)
        if preloaded is not None:
            records.update(preloaded[0])
            metrics.extend(preloaded[1])
        profile["evidence"].update(records)
        live = (records.get("registry_live") or {}).get("value") or {}
        if (records.get("registry_live") or {}).get("status") == "available":
            if live.get("organisation_number") != profile["organisation_number"]:
                raise ValueError("Live registry returned a different organisation number")
            for key in ("name", "legal_form", "employees", "website", "latest_submitted_accounts"):
                if key in live:
                    profile[key] = live[key]
            profile["municipality"] = (live.get("business_address") or {}).get("kommune")
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        browser_attempted = False
        search_cost = 0.0
        if "website" in requested_modules:
            website_url = profile.get("website") or (records.get("registry_live", {}).get("value") or {}).get("website")
            if not website_url and records.get("locations", {}).get("value"):
                for loc in records["locations"]["value"].get("locations", []):
                    if isinstance(loc, dict) and loc.get("website"):
                        website_url = loc["website"]
                        break

            website_record = None
            if website_url:
                website_record, website_metrics = fetch_website(website_url)
                gated = apply_website_identity_gate(profile, website_record)["website"]
                rendered, rendered_metrics = fetch_rendered_website(gated)
                browser_attempted = bool(rendered_metrics["latencies_ms"])
                for key in ("requests", "bytes"):
                    website_metrics[key] += rendered_metrics[key]
                website_metrics["latencies_ms"].extend(rendered_metrics["latencies_ms"])
                gated = apply_website_identity_gate(profile, rendered)["website"]
                browser_error = (gated.get("value") or {}).get("browser_error")
                if browser_error:
                    profile.setdefault("connector_errors", []).append({"connector": "browser", **browser_error})
                profile["evidence"]["website"] = gated

            if profile.get("evidence", {}).get("website", {}).get("status") != "available":
                for cand in generate_website_candidates(profile):
                    if website_url and cand.rstrip("/") == website_url.rstrip("/"):
                        continue
                    cand_rec, cand_metrics = fetch_website(cand)
                    website_metrics["requests"] += cand_metrics.get("requests", 0)
                    website_metrics["bytes"] += cand_metrics.get("bytes", 0)
                    website_metrics["latencies_ms"].extend(cand_metrics.get("latencies_ms", []))
                    if cand_rec.get("status") == "available":
                        cand_gated = apply_website_identity_gate(profile, cand_rec)["website"]
                        if not browser_attempted:
                            rendered, rendered_metrics = fetch_rendered_website(cand_gated)
                            browser_attempted = bool(rendered_metrics["latencies_ms"])
                            for key in ("requests", "bytes"):
                                website_metrics[key] += rendered_metrics[key]
                            website_metrics["latencies_ms"].extend(rendered_metrics["latencies_ms"])
                            cand_gated = apply_website_identity_gate(profile, rendered)["website"]
                            browser_error = (cand_gated.get("value") or {}).get("browser_error")
                            if browser_error:
                                profile.setdefault("connector_errors", []).append({"connector": "browser", **browser_error})
                        if cand_gated.get("status") == "available":
                            profile["evidence"]["website"] = cand_gated
                            break
                        if (cand_gated.get("value") or {}).get("capture_kind") == "rendered_dom" or (cand_gated.get("value") or {}).get("browser_error"):
                            profile["evidence"]["website"] = cand_gated
                    if "website" not in profile["evidence"] or profile["evidence"]["website"]["status"] == "not_found":
                        profile["evidence"]["website"] = cand_rec

            if "website" not in profile.get("evidence", {}):
                if website_record is not None:
                    profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
                else:
                    profile["evidence"]["website"] = apply_website_identity_gate(
                        profile,
                        evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No website discovered or registry URL missing")
                    )["website"]

            if profile["evidence"]["website"].get("status") != "available" and args.search_limit:
                reserved = False
                with search_lock:
                    if search_count < args.search_limit:
                        search_count += 1
                        time.sleep(max(0, 1.0 - (time.monotonic() - search_last_started)))
                        search_last_started = time.monotonic()
                        reserved = True
                if reserved:
                    results, search_ops = brave_search(profile, search_key, timeout=15.0, count=5)
                    search_cost = 0.005  # Gross published price; account credits may offset billing.
                    website_metrics["requests"] += 1
                    website_metrics["bytes"] += search_ops["bytes"]
                    website_metrics["latencies_ms"].append(search_ops["latency_ms"])
                    if search_ops.get("error"):
                        profile.setdefault("connector_errors", []).append({"connector": "brave", "error": search_ops["error"]})
                    selected = choose_search_candidate(profile, results).get("selected")
                    if selected:
                        candidate, candidate_ops = fetch_website(selected["url"])
                        website_metrics["requests"] += candidate_ops["requests"]
                        website_metrics["bytes"] += candidate_ops["bytes"]
                        website_metrics["latencies_ms"].extend(candidate_ops["latencies_ms"])
                        gated = apply_website_identity_gate(profile, candidate)["website"]
                        if gated.get("status") == "available":
                            profile["evidence"]["website"] = gated
                    # No provider result, title, snippet, rank or query is persisted.
                    del results

            if profile["evidence"]["website"].get("status") == "available":
                activity_metrics = fetch_site_activity_pages(profile["evidence"]["website"])
                website_metrics["requests"] += activity_metrics["requests"]
                website_metrics["bytes"] += activity_metrics["bytes"]
                website_metrics["latencies_ms"].extend(activity_metrics["latencies_ms"])
                act_obs = site_activity_obs(profile)
                if act_obs and publishable_observation(act_obs):
                    profile.setdefault("observations", []).append(act_obs)

                for news_obs in site_news_obs(profile):
                    if publishable_observation(news_obs):
                        profile.setdefault("observations", []).append(news_obs)

                profile.setdefault("observations", []).extend(site_social_observations(profile))

        jobs_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "jobs" in requested_modules:
            job_record, job_obs, job_metrics = fetch_jobs_module(profile, feed_snapshot=nav_feed_snapshot) if nav_feed_snapshot is not None else fetch_jobs_module(profile)
            profile["evidence"]["jobs"] = job_record
            if job_obs:
                profile.setdefault("observations", []).extend(job_obs)
            jobs_metrics = job_metrics

        news_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        try:
            news_obs_list, news_stats = fetch_google_news(profile, limit=2, years=1) if args.experimental_connectors else ([], {})
            for nobs in news_obs_list:
                if publishable_observation(nobs):
                    profile.setdefault("observations", []).append(nobs)
            news_metrics["requests"] = int(args.experimental_connectors)
            if news_stats.get("error"):
                profile.setdefault("connector_errors", []).append({"connector": "news", "error": news_stats["error"]})
        except Exception as exc:
            profile.setdefault("connector_errors", []).append({"connector": "news", "error": str(exc)})

        fagfolk_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        try:
            fagfolk_obs_list, fagfolk_stats = fetch_fagfolk(profile, cache_dir=Path(".cache/fagfolk")) if args.experimental_connectors else ([], {})
            for fobs in fagfolk_obs_list:
                if publishable_observation(fobs):
                    profile.setdefault("observations", []).append(fobs)
            fagfolk_metrics["requests"] = int(args.experimental_connectors)
            if fagfolk_stats.get("error"):
                profile.setdefault("connector_errors", []).append({"connector": "fagfolk", "error": fagfolk_stats["error"]})
        except Exception as exc:
            profile.setdefault("connector_errors", []).append({"connector": "fagfolk", "error": str(exc)})

        if profile.get("observations"):
            profile["external_footprint"] = aggregate_footprint(profile["observations"])

        metric = {
            "requests": len(metrics) + website_metrics["requests"] + jobs_metrics["requests"] + news_metrics["requests"] + fagfolk_metrics["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"] + jobs_metrics["bytes"] + news_metrics["bytes"] + fagfolk_metrics["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"] + jobs_metrics["latencies_ms"] + news_metrics["latencies_ms"] + fagfolk_metrics["latencies_ms"],
            "runtime_ms": int((time.monotonic() - company_started) * 1000),
            "third_party_cost_usd": search_cost,
            "completed_at": utc_now(),
        }
        profile["run_metrics"] = metric
        if profile["organisation_number"] in previous:
            refresh_profile(previous[profile["organisation_number"]], profile)
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            try:
                profile, metric = future.result()
            except Exception as exc:
                profile = futures[future]
                profile["fatal_error"] = f"{type(exc).__name__}: {exc}"
                profile.setdefault("connector_errors", []).append({"connector": "pipeline", "error": profile["fatal_error"]})
                for module in requested_modules:
                    if module not in profile["evidence"]:
                        profile["evidence"][module] = evidence(module, "source_error", "pipeline", "https://builderr.ai/challenges/signalpost", note=profile["fatal_error"])
                metric = profile.get("run_metrics") or {"requests": 0, "bytes": 0, "latencies_ms": [], "runtime_ms": 0, "third_party_cost_usd": 0}
                profile["run_metrics"] = metric
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
        "coverage": {
            "verified_websites": sum((p["evidence"].get("website") or {}).get("status") == "available" for p in ordered_profiles),
            "jobs_states": {status: sum((p["evidence"].get("jobs") or {}).get("status") == status for p in ordered_profiles) for status in ("available", "not_found", "blocked", "source_error", "ambiguous", "not_applicable")},
            "job_postings": sum(o["signal_type"] == "job_posting" for p in ordered_profiles for o in p.get("observations", [])),
            "connector_errors": sum(len(p.get("connector_errors", [])) for p in ordered_profiles),
            "failed_companies": sum(bool(p.get("fatal_error")) for p in ordered_profiles),
        },
        "official_score": None,
        "experimental_connectors_enabled": args.experimental_connectors,
        "browser": {"rendered_websites": sum((p["evidence"].get("website", {}).get("value") or {}).get("capture_kind") == "rendered_dom" and p["evidence"]["website"].get("status") == "available" for p in ordered_profiles),
                    "source_errors": sum(error.get("connector") == "browser" for p in ordered_profiles for error in p.get("connector_errors", [])),
                    "bytes_scope": "captured_http_document_rendered_dom_and_secondary_http_not_all_asset_traffic", "max_concurrency": 2, "max_seconds_per_attempt": 20, "max_requests_per_attempt": 40},
        "search_requests": search_count,
        "nav_feed": {key: value for key, value in nav_feed_snapshot.items() if key not in {"hits", "metrics"}} if nav_feed_snapshot is not None else None,
        "gross_third_party_cost_usd": round(sum((p.get("run_metrics") or {}).get("third_party_cost_usd", 0) for p in ordered_profiles), 4),
        "score_status": "Not measured; official checked reference collection is required for the 65/100 threshold",
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
