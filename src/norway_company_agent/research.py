from __future__ import annotations

import re
from typing import Any
from .external_footprint import publishable_observation
from .official import latest_financial_record


def _claim(label: str, value: Any, record: dict[str, Any], classification: str) -> dict[str, Any]:
    return {
        "claim": label,
        "value": value,
        "classification": classification,
        "source_url": record.get("source_url"),
        "retrieved_at": record.get("retrieved_at"),
        "source_class": record.get("source_class") or record.get("source_type"),
        "content_sha256": record.get("content_sha256"),
    }


def answer_profile(row: dict[str, Any], question: str) -> dict[str, Any]:
    """Deterministic retrieval/answer layer; it never invents a missing field."""
    q = question.casefold()
    financial_terms = ("financial", "finance", "account", "revenue", "income", "profit", "result", "debt", "asset", "regnskap")
    activity_terms = ("news", "activity", "publication", "public post", "nyheter", "aktuelt")
    all_topics = not any(term in q for term in (*financial_terms, *activity_terms, "lead", "role", "location", "where", "social", "sentiment", "employee"))
    evidence = row.get("evidence", {})
    facts: list[dict[str, Any]] = []
    unsupported: list[str] = []
    uncertainties: list[dict[str, Any]] = []

    def uncertain(message: str, module: str) -> None:
        unsupported.append(message)
        record = evidence.get(module) or {}
        uncertainties.append({**_claim("Unresolved information", message, record, "source_availability_or_scope"),
                              "module": module, "status": record.get("status", "not_checked")})

    registry = evidence.get("registry_live") if (evidence.get("registry_live") or {}).get("status") == "available" else evidence.get("registry", {})
    if all_topics or "employee" in q:
        for label, value in (
            ("Registered name", row.get("name")),
            ("Organisation number", row.get("organisation_number")),
            ("Legal form", row.get("legal_form")),
            ("Municipality", row.get("municipality")),
            ("Registry employee count", row.get("employees")),
        ):
            if value not in (None, ""):
                facts.append(_claim(label, value, registry, "official_registry_fact"))

    if all_topics:
        bulk = evidence.get("registry") or {}
        raw = bulk.get("value") or {}
        if bulk.get("status") == "available":
            for label, key in (
                ("Registered business activity", "aktivitet"),
                ("Registered company purpose", "vedtektsfestetFormaal"),
                ("Registered contact email", "epostadresse"),
                ("Registered contact telephone", "telefon"),
            ):
                if raw.get(key) not in (None, "", []):
                    facts.append(_claim(label, raw[key], bulk, "official_registry_fact"))

    financial = evidence.get("financials", {})
    if all_topics or any(term in q for term in financial_terms):
        records = (financial.get("value") or {}).get("records") or []
        if records:
            latest = latest_financial_record(records)
            for label, key in (
                ("Account scope", "account_type"),
                ("Reporting period", "period"),
                ("Revenue", "revenue"),
                ("Operating result", "operating_result"),
                ("Annual result", "annual_result"),
                ("Assets", "assets"),
                ("Debt", "debt"),
            ):
                if latest.get(key) is not None:
                    facts.append({**_claim(label, latest[key], financial, "official_annual_account"), "reporting_period": latest.get("period"), "account_scope": latest.get("account_type")})
        else:
            uncertain("No normalized annual-account record was returned; missing values are not interpreted as zero.", "financials")

    roles = evidence.get("roles", {})
    if all_topics or any(term in q for term in ("lead", "role")):
        people = [item for item in (roles.get("value") or {}).get("roles", []) if not item.get("inactive")]
        for person in people[:12]:
            facts.append(_claim(person.get("role") or person.get("group") or "Registered role", person.get("name") or person.get("organisation_number"), roles, "official_role_record"))
        if not people:
            uncertain("No active public role holder was returned.", "roles")

    locations = evidence.get("locations", {})
    if all_topics or any(term in q for term in ("location", "where")):
        items = (locations.get("value") or {}).get("locations", [])
        for item in items[:12]:
            facts.append(_claim("Registered subunit", {"name": item.get("name"), "address": item.get("address")}, locations, "official_subunit_record"))
        if not items:
            uncertain("No registered subunit was returned; this does not prove the company has no physical presence.", "locations")

    website = evidence.get("website", {})
    if all_topics or "social" in q:
        value = website.get("value") or {}
        website_publishable = website.get("status") == "available" and (value.get("identity_assessment") or {}).get("publishable", False)
        if value.get("description") and website_publishable:
            facts.append(_claim("Website description", value["description"], website, "company_reported_claim"))
        for item in (value.get("social_links") or []) if website_publishable else []:
            facts.append(_claim(f"Declared {item['platform']} profile", item["url"], website, "company_linked_social_profile"))
        if website.get("status") == "available" and not website_publishable:
            uncertain("A registry-linked website was fetched, but exact legal-entity identity was not established; its claims and social links are quarantined.", "website")
        if website.get("status") != "available":
            uncertain("The registry-linked company website was not available to this run.", "website")

    if "sentiment" in q:
        uncertain("Sentiment is not scored: no labelled Norwegian news/social evaluation corpus has been run, and company-owned pages are structurally promotional.", "sentiment")

    if all_topics or any(term in q for term in activity_terms):
        publications = [item for item in row.get("observations") or [] if item.get("signal_type") == "public_post" and
                        item.get("source_class") == "company_site" and item.get("published_at") and
                        str(item.get("organisation_number")) == str(row.get("organisation_number")) and publishable_observation(item)]
        for item in publications:
            facts.append(_claim("Company-owned publication", {"title": (item.get("metrics") or {}).get("title"), "published_at": item["published_at"]}, item, "company_reported_publication"))
        if not publications:
            uncertain("No dated company-owned publication was verified; this does not imply inactivity.", "website")

    if all_topics:
        jobs = evidence.get("jobs") or {}
        if jobs.get("status") == "available":
            for job in (jobs.get("value") or {}).get("active_postings", []):
                facts.append(_claim("Active vacancy", job, jobs, "verified_job_posting"))
        else:
            uncertain(f"Hiring source status: {jobs.get('status', 'not_checked')}; unavailable hiring data does not mean zero vacancies.", "jobs")
        for change in row.get("changes") or []:
            label = "Newly discovered publication" if change.get("change_type") == "publication_discovered" else "Verified change: " + change["field"]
            facts.append({"claim": label, "value": {"previous": change["old_value"], "current": change["new_value"]},
                          "source_url": change["source_url"], "retrieved_at": change["retrieved_at"], "content_sha256": change["new_content_sha256"]})
        for error in row.get("connector_errors") or []:
            uncertain(f"{error['connector']} could not be checked: {error['error']}", error['connector'])

    return {
        "organisation_number": row.get("organisation_number"),
        "company_name": row.get("name"),
        "question": question,
        "facts": facts,
        "unsupported_or_uncertain": unsupported,
        "uncertainties": uncertainties,
        "refresh_summary": ({
            "state": "changes_observed" if row.get("changes") else "no_verified_changes" if row.get("evidence_history") or "refresh_baseline" in row else "baseline",
            "message": "Verified changes are listed with their source evidence." if row.get("changes") else
                       "No verified changes from the supplied prior snapshot; unavailable sources do not establish that the company is unchanged." if row.get("evidence_history") or "refresh_baseline" in row else
                       "Baseline run: no prior snapshot supplied; change over time is not established.",
            "material_change_count": len(row.get("changes") or []),
            "baseline_retrieved_at": row.get("refresh_baseline"),
        } if all_topics else None),
        "answer_policy": "Retrieval and deterministic filtering precede prose; only source-linked facts are returned.",
    }


UNSUPPORTED_SCREEN_TERMS = {
    "sentiment": "sentiment is not qualified",
    "glassdoor": "Glassdoor data is not available through a permitted connector",
    "linkedin": "LinkedIn-derived employee data is not available through a permitted connector",
    "traffic": "website traffic is not available through a qualified provider",
    "reviews": "review data is not available through a qualified provider",
    "buzz": "social buzz is not available through a qualified provider",
    "without a website": "missing or unverified website evidence does not prove that a company has no website",
}


def _latest_financial(row: dict[str, Any]) -> dict[str, Any]:
    records = ((row.get("evidence", {}).get("financials", {}).get("value") or {}).get("records") or [])
    return latest_financial_record(records)


def _numeric_operator(phrase: str) -> str:
    return {
        "more than": ">", "over": ">", "above": ">", "at least": ">=",
        "fewer than": "<", "less than": "<", "under": "<", "at most": "<=",
    }.get(phrase.casefold(), phrase)


def parse_screen_query(query: str) -> dict[str, Any]:
    """Parse a deliberately closed company-screen grammar into an inspectable plan."""
    text = " ".join(query.strip().split())
    lower = text.casefold()
    filters: list[dict[str, Any]] = []
    unsupported = [message for term, message in UNSUPPORTED_SCREEN_TERMS.items() if term in lower]

    municipality = re.search(r"\b(?:in|municipality(?:\s+is|\s*=)?)\s+([a-zæøåéü .'-]+?)(?=\s+(?:with|and|having|that|where)\b|$)", lower)
    if municipality:
        filters.append({"field": "municipality", "operator": "eq", "value": municipality.group(1).strip().upper(), "evidence_module": "registry"})

    legal_form = re.search(r"\b(?:legal\s+form|organisation\s+form)\s*(?:is|=)?\s*(asa|as|enk|nuf|ans|da|sa|sti|brl)\b", lower)
    if legal_form:
        filters.append({"field": "legal_form", "operator": "eq", "value": legal_form.group(1).upper(), "evidence_module": "registry"})

    employees = re.search(r"\b(more than|over|above|at least|fewer than|less than|under|at most)\s+(\d+)\s+(?:registered\s+)?employees?\b", lower)
    if not employees:
        employees = re.search(r"\bemployees?\s*(>=|<=|>|<|=)\s*(\d+)\b", lower)
    if employees:
        filters.append({"field": "employees", "operator": _numeric_operator(employees.group(1)), "value": int(employees.group(2)), "evidence_module": "registry"})

    revenue = re.search(r"\brevenue\s*(>=|<=|>|<|=|more than|over|above|at least|fewer than|less than|under|at most)\s*(?:nok\s*)?([\d.,]+)\s*(billion|million|bn|m)?\b", lower)
    if not revenue:
        revenue = re.search(r"\b(more than|over|above|at least|fewer than|less than|under|at most)\s*(?:nok\s*)?([\d.,]+)\s*(billion|million|bn|m)?\s+revenue\b", lower)
    if revenue:
        amount = float(revenue.group(2).replace(",", "."))
        unit = revenue.group(3)
        amount *= 1_000_000_000 if unit in {"billion", "bn"} else 1_000_000 if unit in {"million", "m"} else 1
        filters.append({"field": "revenue", "operator": _numeric_operator(revenue.group(1)), "value": amount, "evidence_module": "financials"})

    if re.search(r"\bunprofitable|loss[- ]making|negative annual result\b", lower):
        filters.append({"field": "annual_result", "operator": "<", "value": 0, "evidence_module": "financials"})
    elif re.search(r"\bprofitable|positive annual result\b", lower):
        filters.append({"field": "annual_result", "operator": ">", "value": 0, "evidence_module": "financials"})

    if re.search(r"\b(?:with|has|have)\s+(?:an?\s+)?(?:official\s+)?website\b", lower):
        filters.append({"field": "website", "operator": "present", "value": True, "evidence_module": "website"})
    if re.search(r"\b(?:with|has|have)\s+(?:annual\s+)?accounts\b", lower):
        filters.append({"field": "financials", "operator": "available", "value": True, "evidence_module": "financials"})

    industry = re.search(r"\bindustry(?:\s+contains|\s+is|\s*=)?\s+[\"']([^\"']+)[\"']", text, flags=re.IGNORECASE)
    if industry:
        filters.append({"field": "industry", "operator": "contains", "value": industry.group(1).casefold(), "evidence_module": "registry"})

    sort = None
    top = re.search(r"\btop\s+(\d+)\s+by\s+(revenue|employees)\b", lower)
    if top:
        sort = {"field": top.group(2), "direction": "desc", "limit": min(int(top.group(1)), 100)}
    return {
        "version": "closed_company_screen_v1",
        "query": text,
        "filters": filters,
        "sort": sort,
        "unsupported": unsupported,
        "executable": bool(filters or sort) and not unsupported,
    }


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "eq":
        return str(actual or "").casefold() == str(expected or "").casefold()
    if operator == "present":
        return bool(actual) is bool(expected)
    if operator == "available":
        return bool(actual) is bool(expected)
    if operator == "contains":
        return str(expected).casefold() in str(actual or "").casefold()
    if actual is None:
        return False
    return {">": actual > expected, ">=": actual >= expected, "<": actual < expected, "<=": actual <= expected, "=": actual == expected}[operator]


def _screen_value(row: dict[str, Any], field: str) -> Any:
    if field in {"municipality", "legal_form", "employees"}:
        return row.get(field)
    if field in {"revenue", "annual_result"}:
        return _latest_financial(row).get(field)
    if field == "website":
        record = row.get("evidence", {}).get("website", {})
        return record.get("status") == "available" and bool((record.get("value") or {}).get("identity_assessment", {}).get("publishable", True))
    if field == "financials":
        return row.get("evidence", {}).get("financials", {}).get("status") == "available"
    if field == "industry":
        return " ".join(filter(None, [str(row.get("industry_code") or ""), str(row.get("industry_label") or "")]))
    return None


def screen_profiles(rows: list[dict[str, Any]], query: str) -> dict[str, Any]:
    plan = parse_screen_query(query)
    if not plan["executable"]:
        return {"query": query, "plan": plan, "results": [], "result_count": 0, "abstained": True, "reason": "; ".join(plan["unsupported"]) or "No supported criterion was recognized."}
    results = []
    for row in rows:
        if not all(_compare(_screen_value(row, item["field"]), item["operator"], item["value"]) for item in plan["filters"]):
            continue
        citations = []
        evidence_modules = {item["evidence_module"] for item in plan["filters"]}
        if plan.get("sort"):
            evidence_modules.add("financials" if plan["sort"]["field"] == "revenue" else "registry")
        for module in sorted(evidence_modules):
            record = row.get("evidence", {}).get(module, {})
            citations.append({
                "module": module,
                "source_url": record.get("source_url"),
                "retrieved_at": record.get("retrieved_at"),
                "content_sha256": record.get("content_sha256"),
            })
        results.append({
            "organisation_number": row.get("organisation_number"),
            "name": row.get("name"),
            "municipality": row.get("municipality"),
            "employees": row.get("employees"),
            "revenue": _latest_financial(row).get("revenue"),
            "annual_result": _latest_financial(row).get("annual_result"),
            "citations": citations,
        })
    sort = plan.get("sort")
    if sort:
        results.sort(key=lambda item: (item.get(sort["field"]) is None, -(item.get(sort["field"]) or 0), item.get("organisation_number") or ""))
        results = results[: sort["limit"]]
    else:
        results.sort(key=lambda item: item.get("organisation_number") or "")
    return {"query": query, "plan": plan, "results": results, "result_count": len(results), "abstained": False}
