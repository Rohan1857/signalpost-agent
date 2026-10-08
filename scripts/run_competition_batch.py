#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.external_footprint import aggregate_footprint, publishable_observation  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate, assess_social_identity  # noqa: E402
from norway_company_agent.jobs import fetch_jobs_module  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402
from scripts.extract_company_site_activity import observation as site_activity_obs  # noqa: E402
from scripts.extract_company_site_news import observation as site_news_obs  # noqa: E402
from scripts.run_google_news_rss_connector import fetch as fetch_google_news  # noqa: E402
from scripts.run_fagfolkguiden_reviews_connector import fetch as fetch_fagfolk  # noqa: E402

LEGAL_SUFFIXES = {"as", "asa", "ans", "da", "enk", "iks", "sa", "nuf", "holding", "eiendom", "invest", "gruppen", "group"}


def generate_domain_candidates(name: str) -> list[str]:
    cleaned = re.sub(r"\b(ASA|AS|ANS|DA|ENK|IKS|SA|NUF)\b", "", name, flags=re.IGNORECASE).strip()
    text = cleaned.translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        return []
    candidates = []
    candidates.append("https://www." + "".join(words) + ".no")
    if len(words) > 1:
        candidates.append("https://www." + "-".join(words) + ".no")
    non_generic = [w for w in words if w not in LEGAL_SUFFIXES]
    if non_generic and len(non_generic) < len(words):
        candidates.append("https://www." + "".join(non_generic) + ".no")
    return candidates


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,jobs")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "jobs"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    def enrich(profile: dict) -> tuple[dict, dict]:
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            website_url = profile.get("website") or (records.get("registry_live", {}).get("value") or {}).get("website")
            if not website_url and records.get("locations", {}).get("value"):
                for loc in records["locations"]["value"]:
                    if isinstance(loc, dict) and loc.get("website"):
                        website_url = loc["website"]
                        break

            website_record = None
            if website_url:
                website_record, website_metrics = fetch_website(website_url)
                gated = apply_website_identity_gate(profile, website_record)["website"]
                if gated.get("status") == "available":
                    profile["evidence"]["website"] = gated

            if profile.get("evidence", {}).get("website", {}).get("status") != "available":
                for cand in generate_domain_candidates(profile.get("name") or ""):
                    cand_rec, cand_metrics = fetch_website(cand)
                    website_metrics["requests"] += cand_metrics.get("requests", 0)
                    website_metrics["bytes"] += cand_metrics.get("bytes", 0)
                    website_metrics["latencies_ms"].extend(cand_metrics.get("latencies_ms", []))
                    if cand_rec.get("status") == "available":
                        cand_gated = apply_website_identity_gate(profile, cand_rec)["website"]
                        if cand_gated.get("status") == "available":
                            profile["evidence"]["website"] = cand_gated
                            break

            if "website" not in profile.get("evidence", {}):
                if website_record is not None:
                    profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
                else:
                    profile["evidence"]["website"] = apply_website_identity_gate(
                        profile,
                        evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No website discovered or registry URL missing")
                    )["website"]

            if profile["evidence"]["website"].get("status") == "available":
                web_val = profile["evidence"]["website"].get("value") or {}
                act_obs = site_activity_obs(profile)
                if act_obs and publishable_observation(act_obs):
                    profile.setdefault("observations", []).append(act_obs)

                news_obs = site_news_obs(profile)
                if news_obs and publishable_observation(news_obs):
                    profile.setdefault("observations", []).append(news_obs)

                org = str(profile["organisation_number"])
                retrieved = profile["evidence"]["website"].get("retrieved_at") or utc_now()
                content_digest = web_val.get("content_sha256")
                for link in web_val.get("social_links") or []:
                    social_assessment = assess_social_identity(profile, link)
                    if social_assessment.get("publishable") and content_digest:
                        platform = link.get("platform")
                        social_url = link.get("url")
                        so_id = "social-" + hashlib.sha256(f"{org}|{social_url}".encode()).hexdigest()[:20]
                        so_obs = {
                            "id": so_id,
                            "organisation_number": org,
                            "platform": platform,
                            "signal_type": "profile_metrics",
                            "source_url": social_url,
                            "retrieved_at": retrieved,
                            "content_sha256": content_digest,
                            "exact_entity": True,
                            "identity_proof": [
                                {"type": "company_site_crosslink_and_handle_match", "value": social_url, "score": social_assessment.get("identity_score")},
                            ],
                            "acquisition_mode": "permitted_public_page",
                            "rights_status": "approved",
                            "source_class": "company_site",
                            "evidence_span": f"Verified {platform} profile link on official company website.",
                            "metrics": {"platform": platform, "url": social_url},
                            "strategy": "social_profile_metrics",
                        }
                        if publishable_observation(so_obs):
                            profile.setdefault("observations", []).append(so_obs)

        jobs_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "jobs" in requested_modules:
            job_record, job_obs, job_metrics = fetch_jobs_module(profile)
            profile["evidence"]["jobs"] = job_record
            if job_obs:
                profile.setdefault("observations", []).extend(job_obs)
            jobs_metrics = job_metrics

        news_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        try:
            news_obs_list, news_stats = fetch_google_news(profile, limit=2, years=1)
            for nobs in news_obs_list:
                if publishable_observation(nobs):
                    profile.setdefault("observations", []).append(nobs)
            news_metrics["requests"] = 1
            if news_stats.get("error"):
                profile.setdefault("connector_errors", []).append({"connector": "news", "error": news_stats["error"]})
        except Exception as exc:
            profile.setdefault("connector_errors", []).append({"connector": "news", "error": str(exc)})

        fagfolk_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        try:
            fagfolk_obs_list, fagfolk_stats = fetch_fagfolk(profile, cache_dir=Path(".cache/fagfolk"))
            for fobs in fagfolk_obs_list:
                if publishable_observation(fobs):
                    profile.setdefault("observations", []).append(fobs)
            fagfolk_metrics["requests"] = 1
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
        }
        profile["run_metrics"] = metric
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
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
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
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
