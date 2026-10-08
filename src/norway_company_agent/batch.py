from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .official import accounting_obligation_assessment
from .research import answer_profile
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    for value in values:
        org = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(org or "") if character.isdigit())
        if len(org) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    orgs = [record["organisation_number"] for record in records]
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    if missing:
        raise ValueError(f"Organisation numbers absent from registry snapshot: {missing[:10]}")
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status in ("available", "complete"):
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status in ("not_found", "not_available", "empty"):
        return "not_found"
    if status in ("blocked", "blocked_robots", "blocked_policy"):
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status in ("source_error", "error", "failed"):
        return "source_error"
    if status == "budget_exhausted":
        return "budget_exhausted"
    return "submission_error"


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"

    evidence_dict = profile.get("evidence", {})
    fin_val = (evidence_dict.get("financials") or {}).get("value") or {}
    roles_val = (evidence_dict.get("roles") or {}).get("value") or {}
    locs_val = (evidence_dict.get("locations") or {}).get("value") or {}
    web_val = (evidence_dict.get("website") or {}).get("value") or {}
    jobs_val = (evidence_dict.get("jobs") or {}).get("value") or {}

    def _avail(mod_name: str) -> str:
        rec = evidence_dict.get(mod_name)
        if not rec:
            return "not_available"
        st = rec.get("status")
        if st in ("available", "complete"):
            return "available"
        if st in ("not_found", "empty", "not_available"):
            return "not_available"
        if st in ("blocked", "blocked_robots", "blocked_policy"):
            return "blocked"
        if st == "not_applicable":
            return "not_applicable"
        if st in ("error", "source_error", "failed"):
            return "failed"
        return "ambiguous"

    claims: list[dict[str, Any]] = []
    evidence_items: list[dict[str, Any]] = []
    for mod in modules:
        rec = evidence_dict.get(mod) or {}
        avail = _avail(mod)
        ev_id = f"ev-{mod}"
        val = rec.get("value")
        claims.append({
            "field": mod,
            "value": val,
            "availability": avail,
            "confidence": 1.0 if avail == "available" else 0.0,
            "evidence_ids": [ev_id],
        })
        evidence_items.append({
            "id": ev_id,
            "source_url": rec.get("source_url") or "",
            "source_class": rec.get("source_type") or "system",
            "retrieved_at": rec.get("retrieved_at") or completed_at,
            "content_sha256": rec.get("content_sha256") or "",
            "claim_span": str(rec.get("note") or ""),
        })

    sections = {
        "legal_identity_and_public_brand": {
            "status": _avail("registry"),
            "organisation_number": profile.get("organisation_number"),
            "name": profile.get("name"),
            "legal_form": profile.get("legal_form"),
            "municipality": profile.get("municipality"),
            "industry": profile.get("industry_label") or profile.get("industry_code"),
        },
        "latest_annual_accounts_and_history": {
            "status": _avail("financials"),
            "records": fin_val.get("records") or [],
            "history_years": [h.get("year") for h in (evidence_dict.get("financial_history", {}).get("value") or {}).get("pdfs", []) if h.get("year")],
        },
        "leadership_and_registered_workplaces": {
            "status": "available" if (_avail("roles") == "available" or _avail("locations") == "available") else "not_available",
            "roles": roles_val.get("roles") or [],
            "locations": locs_val.get("locations") or [],
        },
        "verified_official_website_and_profiles": {
            "status": _avail("website"),
            "url": web_val.get("final_url") or profile.get("website"),
            "social_links": web_val.get("social_links") or [],
        },
        "hiring_and_dated_public_activity": {
            "status": _avail("jobs"),
            "job_count": jobs_val.get("job_count", 0) if _avail("jobs") == "available" else None,
            "vacancies": jobs_val.get("active_postings") or [],
            "observations": [o for o in profile.get("observations", []) if o.get("signal_type") in ("job_posting", "workforce_snapshot", "public_post", "public_mention")],
        },
        "claim_evidence": [
            {
                "module": mod,
                "availability": _avail(mod),
                "source_url": (evidence_dict.get(mod) or {}).get("source_url"),
                "retrieved_at": (evidence_dict.get(mod) or {}).get("retrieved_at"),
                "content_sha256": (evidence_dict.get(mod) or {}).get("content_sha256"),
            }
            for mod in modules if mod in evidence_dict
        ],
        "refresh_metadata": {
            "retrieved_at": completed_at,
            "observations_count": len(profile.get("observations") or []),
            "material_changes_since_previous": profile.get("changes") or [],
        },
        "synthesis": answer_profile(profile, "Summarize this company"),
    }

    return {
        "organisation_number": profile["organisation_number"],
        "run_id": run_id,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed" if entity_state == "complete" else entity_state,
        },
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "claims": claims,
        "evidence": evidence_items,
        "changes": profile.get("changes") or [],
        "errors": profile.get("connector_errors") or [],
        "operations": profile.get("run_metrics") or {
            "requests": sum(item.get("retry_count", 0) + 1 for item in module_states.values()),
            "runtime_ms": 0,
            "third_party_cost_usd": 0,
        },
        "sections": sections,
        "modules": module_states,
        "profile": profile,
    }


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES or state.get("state") == "submission_error"
    ]
    submission_errors = [item.get("organisation_number") for item in envelopes if item.get("state") == "submission_error"]

    required_sections = {
        "legal_identity_and_public_brand",
        "latest_annual_accounts_and_history",
        "leadership_and_registered_workplaces",
        "verified_official_website_and_profiles",
        "hiring_and_dated_public_activity",
        "claim_evidence",
        "refresh_metadata",
        "synthesis",
    }
    incomplete_sections = [
        item.get("organisation_number")
        for item in envelopes
        if not required_sections.issubset(set((item.get("sections") or {}).keys()))
    ]
    invalid_identities = [
        item.get("organisation_number")
        for item in envelopes
        if not (item.get("profile") or {}).get("organisation_number")
    ]

    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES and item.get("state") != "submission_error" for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_submission_errors": not submission_errors,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
        "all_sections_present": not incomplete_sections,
        "all_identities_valid": not invalid_identities,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "invalid_states": invalid_states,
        "submission_errors": submission_errors,
        "incomplete_sections": incomplete_sections,
    }


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
