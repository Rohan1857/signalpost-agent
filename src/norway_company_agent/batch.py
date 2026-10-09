from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .official import accounting_obligation_assessment
from .research import answer_profile
from .external_footprint import publishable_observation
from .sampling import iter_bulk


TERMINAL_STATES = {
    "available", "not_available", "blocked", "not_applicable", "ambiguous", "failed",
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
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    snapshot_sha256 = digest.hexdigest()
    retrieved_at = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    parse_warnings = []
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        extras = raw.pop(None, [])
        malformed = any(str(cell or "").strip() for cell in extras)
        if extras:
            parse_warnings.append({"organisation_number": org, "extra_cell_count": len(extras), "nonempty_extra_cells": malformed})
        if malformed:
            # Unlabelled data may signal column misalignment; do not publish this row.
            profile = {"organisation_number": org, "name": None}
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "source_error" if malformed else "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=None if malformed else raw,
                note="Registry CSV row has unlabelled nonempty cells; row quarantined" if malformed else
                     f"Ignored {len(extras)} unlabelled empty trailing CSV cells" if extras else None,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        if malformed:
            profile["evidence"]["registry"]["source_parse_diagnostics"] = {"unlabelled_cells": extras, "named_cells": raw}
            profile["evidence"]["accounting_obligation"] = evidence(
                "accounting_obligation", "ambiguous", "official_rule_interpretation", "https://www.brreg.no/",
                note="Legal form unavailable because the supplied registry row failed parsing checks",
            )
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    for org in missing:
        found[org] = {"organisation_number": org, "name": None, "evidence": {
            "registry": evidence("registry", "not_found", "official_registry_bulk", "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                                 note="Company absent from supplied frozen snapshot; live registry lookup required", content_sha256=snapshot_sha256, retrieved_at=retrieved_at),
            "accounting_obligation": evidence("accounting_obligation", "ambiguous", "official_rule_interpretation", "https://www.brreg.no/",
                                              note="Legal form not available in supplied snapshot")}}
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found) - len(missing),
        "missing_from_snapshot": missing,
        "registry_parse_warnings": parse_warnings,
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "failed"
    status = record.get("status")
    if status in ("available", "complete"):
        return "available"
    if status == "not_applicable":
        return "not_applicable"
    if status in ("not_found", "not_available", "empty"):
        return "not_available"
    if status in ("blocked", "blocked_robots", "blocked_policy"):
        return "blocked"
    if status in ("source_error", "error", "failed"):
        return "failed"
    return "ambiguous" if status == "ambiguous" else "failed"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _leaves(value: Any, path: tuple = ()):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaves(child, (*path, key))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _leaves(child, (*path, index))
    elif value is not None and value != "":
        yield path, value


def _path_value(value: Any, path: list) -> Any:
    for key in path:
        value = value[key]
    return value


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    modules = list(modules)
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "failed" if profile.get("fatal_error") else "available"

    evidence_dict = profile.get("evidence", {})
    fin_val = (evidence_dict.get("financials") or {}).get("value") or {}
    roles_val = (evidence_dict.get("roles") or {}).get("value") or {}
    locs_val = (evidence_dict.get("locations") or {}).get("value") or {}
    web_val = (evidence_dict.get("website") or {}).get("value") or {}
    if evidence_terminal_state(evidence_dict.get("website")) != "available":
        web_val = {}
    jobs_val = (evidence_dict.get("jobs") or {}).get("value") or {}

    def _avail(mod_name: str) -> str:
        return evidence_terminal_state(evidence_dict.get(mod_name))

    claims: list[dict[str, Any]] = []
    evidence_items: list[dict[str, Any]] = []
    snapshots = {}
    for mod in modules:
        rec = evidence_dict.get(mod) or {}
        avail = _avail(mod)
        val = rec.get("value")
        if avail != "available":
            claims.append({"field": mod, "value": None, "availability": avail, "confidence": 0.0, "evidence_ids": []})
            continue
        # Preserve the normalized source snapshot alongside its original response hash.
        # API spans are exact JSON values, rather than invented natural-language excerpts.
        snapshots[mod] = {"value": val, "normalized_sha256": hashlib.sha256(_canonical(val).encode()).hexdigest(),
                          "content_sha256": rec.get("content_sha256"), "source_row_key": rec.get("source_row_key"),
                          "source_url": rec.get("source_url"), "retrieved_at": rec.get("retrieved_at")}
        selected = val
        if mod == "website" and isinstance(val, dict):
            selected = {key: val[key] for key in ("final_url", "title", "description", "social_links") if key in val}
        for index, (path, leaf) in enumerate(_leaves(selected)):
            ev_id = f"ev-{mod}-{index}"
            period = rec.get("effective_at") or rec.get("as_of")
            if mod == "financials" and len(path) > 1 and path[0] == "records":
                period = val["records"][path[1]].get("period")
            claims.append({"field": ".".join([mod, *map(str, path)]), "value": leaf, "availability": avail,
                           "confidence": 0.99, "reporting_period": period, "evidence_ids": [ev_id]})
            evidence_items.append({"id": ev_id, "source_url": rec.get("source_url"),
                                   "source_class": rec.get("source_class") or rec.get("source_type"),
                                   "retrieved_at": rec.get("retrieved_at"), "content_sha256": rec.get("content_sha256"),
                                   "claim_span": _canonical(leaf), "snapshot_id": mod, "extraction_path": list(path),
                                   "extraction_method": "normalized_json_path", "reporting_period": period})
            if mod == "website" and val.get("capture_kind") == "rendered_dom":
                evidence_items[-1]["capture_kind"] = "rendered_dom"
                evidence_items[-1]["extraction_method"] = "rendered_dom_snapshot_json_path"
                evidence_items[-1]["http_document"] = val.get("http_document")

    published_observations = [item for item in profile.get("observations", [])
                              if str(item.get("organisation_number")) == str(profile["organisation_number"]) and publishable_observation(item)]
    for item in published_observations:
        ev_id = "ev-observation-" + item["id"]
        claims.append({"field": "observations." + item["signal_type"], "value": item.get("metrics") or item.get("evidence_span"),
                       "availability": "available", "confidence": 0.99, "evidence_ids": [ev_id]})
        evidence_items.append({"id": ev_id, "source_url": item["source_url"], "source_class": item["source_class"],
                               "retrieved_at": item["retrieved_at"], "content_sha256": item["content_sha256"],
                               "claim_span": item.get("evidence_span"), "observation_id": item["id"],
                               "extraction_method": item.get("extraction_method") or "verified_observation", "reporting_period": item.get("published_at"),
                               "date_evidence_span": item.get("date_evidence_span")})

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
            "url": web_val.get("final_url"),
            "social_links": web_val.get("social_links") or [],
        },
        "hiring_and_dated_public_activity": {
            "status": _avail("jobs"),
            "job_count": jobs_val.get("job_count") if _avail("jobs") in {"available", "not_available"} else None,
            "vacancies": jobs_val.get("active_postings") or [],
            "observations": [o for o in published_observations if o.get("signal_type") in ("job_posting", "workforce_snapshot", "public_post", "public_mention")],
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
            "terminal_status": "completed" if entity_state == "available" else "failed",
        },
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "claims": claims,
        "evidence": evidence_items,
        "source_snapshots": snapshots,
        "changes": profile.get("changes") or [],
        "errors": profile.get("connector_errors") or [],
        "operations": {**{
            "requests": sum(item.get("retry_count", 0) + 1 for item in module_states.values()),
            "runtime_ms": 0,
            "third_party_cost_usd": 0,
        }, **(profile.get("run_metrics") or {})},
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
    submission_errors = [item.get("organisation_number") for item in envelopes if (item.get("profile") or {}).get("fatal_error") or item.get("state") == "submission_error"]

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
    invalid_identities = []
    invalid_claims = []
    required_fields = {"organisation_number", "run", "claims", "evidence", "source_snapshots", "changes", "errors", "operations", "sections", "modules", "profile"}
    for item in envelopes:
        org = item.get("organisation_number")
        profile = item.get("profile") or {}
        legal = (item.get("sections") or {}).get("legal_identity_and_public_brand") or {}
        if not (re.fullmatch(r"\d{9}", str(org or "")) and org == profile.get("organisation_number") == legal.get("organisation_number")):
            invalid_identities.append(org)
        live = (profile.get("evidence", {}).get("registry_live", {}).get("value") or {}).get("organisation_number")
        if live and live != org:
            invalid_identities.append(org)
        try:
            if not required_fields.issubset(item) or not item["claims"] or not item["modules"]:
                raise ValueError("missing required envelope fields or claims")
            for timestamp in (item["run"]["started_at"], item["run"]["completed_at"]):
                if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError("run timestamp has no timezone")
            refs = {value["id"]: value for value in item["evidence"]}
            if len(refs) != len(item["evidence"]):
                raise ValueError("duplicate evidence IDs")
            for claim in item["claims"]:
                if claim["availability"] not in TERMINAL_STATES or not 0 <= claim["confidence"] <= 1:
                    raise ValueError("invalid claim state or confidence")
                if claim["availability"] != "available":
                    if claim.get("value") is not None:
                        raise ValueError("unavailable claim contains a published value")
                    continue
                if claim.get("value") is None or not claim.get("evidence_ids"):
                    raise ValueError("available claim lacks value or evidence")
                for ev_id in claim["evidence_ids"]:
                    ev = refs[ev_id]
                    if not ev.get("source_url", "").startswith(("https://", "http://")) or not ev.get("claim_span"):
                        raise ValueError("missing source or evidence span")
                    if not re.fullmatch(r"[a-f0-9]{64}", str(ev.get("content_sha256") or "")):
                        raise ValueError("invalid content hash")
                    if datetime.fromisoformat(ev["retrieved_at"].replace("Z", "+00:00")).tzinfo is None:
                        raise ValueError("evidence timestamp has no timezone")
                    if "snapshot_id" in ev:
                        snap = item["source_snapshots"][ev["snapshot_id"]]
                        if snap["normalized_sha256"] != hashlib.sha256(_canonical(snap["value"]).encode()).hexdigest():
                            raise ValueError("normalized snapshot hash mismatch")
                        if snap["content_sha256"] != ev["content_sha256"]:
                            raise ValueError("source hash mismatch")
                        leaf = _path_value(snap["value"], ev["extraction_path"])
                        if leaf != claim["value"] or _canonical(leaf) != ev["claim_span"]:
                            raise ValueError("claim does not match its source span")
                        if ev["snapshot_id"] == "financials" and not ev.get("reporting_period"):
                            raise ValueError("financial claim lacks reporting period")
                    elif "observation_id" in ev:
                        obs = next(o for o in profile.get("observations", []) if o["id"] == ev["observation_id"])
                        if obs["organisation_number"] != org or not publishable_observation(obs):
                            raise ValueError("observation identity or rights mismatch")
                        if claim["value"] != (obs.get("metrics") or obs.get("evidence_span")) or ev["claim_span"] != obs.get("evidence_span") or ev["content_sha256"] != obs["content_sha256"]:
                            raise ValueError("observation claim/evidence mismatch")
                    else:
                        raise ValueError("evidence has no retained snapshot")
            for module, state in item["modules"].items():
                module_claims = [c for c in item["claims"] if c["field"] == module or c["field"].startswith(module + ".")]
                if not module_claims or any(c["availability"] != state["state"] for c in module_claims):
                    raise ValueError("module state does not match its claims")
            registry_value = (item["source_snapshots"].get("registry") or {}).get("value") or {}
            registry_org = registry_value.get("organisasjonsnummer") or registry_value.get("organisation_number")
            if registry_org and str(registry_org) != org:
                raise ValueError("registry snapshot belongs to a different organisation")
            website = profile.get("evidence", {}).get("website") or {}
            if website.get("status") == "available" and not (website.get("value", {}).get("identity_assessment") or {}).get("publishable"):
                raise ValueError("website has no exact-entity assessment")
            website_value = website.get("value") or {}
            if website.get("status") == "available" and website_value.get("capture_kind") == "rendered_dom":
                capture = website_value.get("captured_html")
                if not isinstance(capture, str) or hashlib.sha256(capture.encode("utf-8")).hexdigest() != website.get("content_sha256"):
                    raise ValueError("rendered DOM snapshot hash does not match retained capture")
                if website_value.get("content_sha256") != website.get("content_sha256"):
                    raise ValueError("rendered DOM value hash differs from source snapshot hash")
                document = website_value.get("http_document") or {}
                if document.get("capture_kind") != "browser_http_response_body" or not document.get("source_url") or not re.fullmatch(r"[0-9a-f]{64}", str(document.get("content_sha256") or "")):
                    raise ValueError("rendered DOM has no separate HTTP-document provenance")
        except (KeyError, TypeError, ValueError, IndexError, AttributeError, StopIteration) as exc:
            invalid_claims.append({"organisation_number": org, "reason": str(exc)})

    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES and item.get("state") != "submission_error" for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_submission_errors": not submission_errors,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
        "all_sections_present": not incomplete_sections,
        "all_identities_valid": not invalid_identities,
        "all_claims_supported": not invalid_claims,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "invalid_states": invalid_states,
        "submission_errors": submission_errors,
        "incomplete_sections": incomplete_sections,
        "invalid_identities": invalid_identities,
        "invalid_claims": invalid_claims,
    }


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
