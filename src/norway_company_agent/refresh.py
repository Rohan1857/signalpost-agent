from __future__ import annotations

from typing import Any
from datetime import datetime, timezone
from .external_footprint import publishable_observation


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.website": ("website",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
    "website.title": ("evidence", "website", "value", "title"),
    "website.description": ("evidence", "website", "value", "description"),
    "website.social_links": ("evidence", "website", "value", "social_links"),
    "jobs.active_postings": ("evidence", "jobs", "value", "active_postings"),
}


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def _publication_changes(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_website = previous.get("evidence", {}).get("website") or {}
    new_website = current.get("evidence", {}).get("website") or {}
    if old_website.get("status") != "available" or new_website.get("status") != "available":
        return []
    def publications(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in profile.get("observations") or []
                if item.get("signal_type") == "public_post" and item.get("source_class") == "company_site" and
                item.get("published_at") and str(item.get("organisation_number")) == str(profile.get("organisation_number")) and publishable_observation(item)}
    old_items = publications(previous)
    changes = []
    for key, item in publications(current).items():
        old = old_items.get(key)
        if old and (old.get("metrics") == item.get("metrics") or old.get("content_sha256") == item.get("content_sha256")):
            continue
        change_type = "publication_updated" if old else "publication_discovered"
        if not old:
            try:
                published = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
                baseline = datetime.fromisoformat(str(old_website.get("retrieved_at")).replace("Z", "+00:00"))
                if published.replace(tzinfo=published.tzinfo or timezone.utc) > baseline.replace(tzinfo=baseline.tzinfo or timezone.utc):
                    change_type = "new_publication"
            except (ValueError, TypeError):
                pass
        changes.append({"organisation_number": current["organisation_number"], "field": "observations.public_post",
                        "change_type": change_type, "old_value": old.get("metrics") if old else None, "new_value": item.get("metrics"),
                        "source_url": item["source_url"], "retrieved_at": item["retrieved_at"], "effective_at": item["published_at"],
                        "source_class": item["source_class"], "old_content_sha256": old.get("content_sha256") if old else None,
                        "new_content_sha256": item["content_sha256"], "status": "available"})
    # A bounded crawl losing a post cannot establish that it was removed.
    return changes


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field, path in TRACKED_FIELDS.items():
        old_value = _read(previous, path)
        new_value = _read(current, path)
        if old_value == new_value:
            continue
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
        # A blocked/failed lookup is a gap in observation, not a real deletion.
        if record.get("status") not in {"available", "not_found", "not_available"}:
            continue
        if previous_record.get("status") not in {"available", "not_found", "not_available"}:
            continue
        if field == "jobs.active_postings" and (record.get("value") or {}).get("job_count_scope") == "observed_verified_vacancies":
            old_ids = {item.get("id") for item in old_value or []}
            new_ids = {item.get("id") for item in new_value or []}
            # Losing a name-hint/page-limited observation does not prove vacancy deletion.
            if old_ids - new_ids:
                continue
        if record.get("content_sha256") and record.get("content_sha256") == previous_record.get("content_sha256"):
            continue
        changes.append({
            "organisation_number": new_org,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "source_url": record.get("source_url"),
            "retrieved_at": record.get("retrieved_at"),
            "effective_at": record.get("effective_at") or record.get("as_of"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "old_content_sha256": previous_record.get("content_sha256"),
            "new_content_sha256": record.get("content_sha256"),
            "status": record.get("status"),
        })
    return changes + _publication_changes(previous, current)


def refresh_profile(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    current["changes"] = diff_profile(previous, current)
    history = list(previous.get("evidence_history") or [])
    for module, record in previous.get("evidence", {}).items():
        if not record.get("content_sha256"):
            continue
        key = (module, record["content_sha256"])
        if not any((entry["module"], entry["record"].get("content_sha256")) == key for entry in history):
            history.append({"module": module, "record": record})
    current["evidence_history"] = history
    current["refresh_baseline"] = previous.get("run_metrics", {}).get("completed_at")
    return current


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]
