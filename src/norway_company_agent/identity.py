from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def normalize_organisation_number(value: Any) -> str | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:NO\s*)?\d{3}[ .-]?\d{3}[ .-]?\d{3}(?:\s*MVA)?", text, re.I):
        return None
    return re.sub(r"\D", "", text)


def _legal_tokens(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or "").translate(
        str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE"})
    )).encode("ascii", "ignore").decode().casefold()
    return re.findall(r"[a-z0-9]+", text)


def exact_legal_name(name: Any, candidate: Any) -> bool:
    return bool(_legal_tokens(name) and _legal_tokens(name) == _legal_tokens(candidate))


def _contains_legal_name(name: Any, text: Any) -> bool:
    expected = _legal_tokens(name)
    # A hostname or scattered brand words cannot establish a legal identity.
    if not expected:
        return False
    for fragment in re.split(r"[|;\n©]|\s[-–—]\s", str(text or "")):
        tokens = _legal_tokens(fragment)
        for index in range(len(tokens) - len(expected) + 1):
            if tokens[index:index + len(expected)] == expected:
                if index == 0 or tokens[index - 1] in {"welcome", "to", "hos", "fra", "av", "om", "by", "copyright"} or tokens[index - 1].isdigit():
                    return True
    return False


def _legal_operator_proof(profile: dict[str, Any], value: dict[str, Any]) -> dict[str, Any] | None:
    """A site's sale terms can identify its operator; portfolio mentions cannot."""
    homepage = urllib.parse.urlparse(value.get("final_url") or "")
    if homepage.scheme not in {"http", "https"} or not homepage.hostname:
        return None
    expected = normalize_organisation_number(profile.get("organisation_number"))
    name = str(profile.get("name") or "")
    if not expected or not name:
        return None
    for page in value.get("pages") or []:
        parsed = urllib.parse.urlparse(str(page.get("url") or ""))
        if parsed.scheme not in {"http", "https"} or parsed.hostname != homepage.hostname or parsed.port != homepage.port:
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", str(page.get("content_sha256") or "")):
            continue
        context = parsed.path + " " + str(page.get("title") or "")
        text = str(page.get("main_text_excerpt") or "")
        if parsed.path.rstrip("/") == "/policies/contact-information":
            # A merchant's own legal contact disclosure is distinct from a group directory.
            trade = re.search(r"(?im)^\s*(?:Handelsnavn|Trade name)\s*:\s*([^\n]+)", text)
            operator = re.search(r"(?im)^\s*(?:Fysisk adresse|Physical address)\s*:\s*([^,\n]+),", text)
            numbers = re.findall(r"(?:org(?:anisasjons)?\.?\s*(?:nr|nummer)\.?\s*:?|organisation number)\s*((?:NO\s*)?\d{3}[ .-]?\d{3}[ .-]?\d{3})", text, re.I)
            trade_host = trade.group(1).strip().casefold().removeprefix("www.") if trade else None
            if (trade_host == homepage.hostname.casefold().removeprefix("www.") and operator and
                    exact_legal_name(name, operator.group(1).strip()) and numbers and
                    {normalize_organisation_number(number) for number in numbers} == {expected}):
                return {"source_url": page["url"], "content_sha256": page["content_sha256"], "claim_span": text,
                        "extraction_method": "merchant_contact_policy_exact_name_org_domain_v1"}
        if not re.search(r"terms|vilk[aå]r|betingelser|impressum", context, re.I):
            continue
        for line in str(page.get("main_text_excerpt") or "").splitlines():
            # Require an explicit seller declaration at the start, not a company listed in a group.
            match = re.match(r"\s*(?:selger(?:en)?\s+er|the\s+seller\s+is)\s+(.{1,700})$", line, re.I)
            if not match:
                continue
            declaration = match.group(1)
            if not exact_legal_name(name, re.split(r"[,;]", declaration, maxsplit=1)[0].strip()):
                continue
            numbers = re.findall(r"(?:org(?:anisasjons)?\.?\s*(?:nr|nummer)\.?\s*:?|organisation number)\s*((?:NO\s*)?\d{3}[ .-]?\d{3}[ .-]?\d{3})", declaration, re.I)
            if not numbers or {normalize_organisation_number(number) for number in numbers} != {expected}:
                continue
            return {"source_url": page["url"], "content_sha256": page["content_sha256"], "claim_span": line, "extraction_method": "legal_terms_exact_seller_name_and_org_v1"}
    return None


def _registered_contact_proof(profile: dict[str, Any], value: dict[str, Any]) -> dict[str, Any] | None:
    live = profile.get("evidence", {}).get("registry_live") or {}
    record = live.get("value") or {}
    if live.get("status") != "available" or str(record.get("organisation_number")) != str(profile.get("organisation_number")):
        return None
    if not exact_legal_name(record.get("name"), profile.get("name")):
        return None
    if not live.get("source_url") or not re.fullmatch(r"[0-9a-f]{64}", str(live.get("content_sha256") or "")):
        return None
    if not live.get("retrieved_at") or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("content_sha256") or "")):
        return None
    # Shared group addresses/phones cannot prove identity without the complete name on the homepage.
    bare_name = re.sub(r"\s+(?:AS|ASA|ANS|DA|ENK|IKS|SA|NUF)\s*$", "", str(profile.get("name") or ""), flags=re.I)
    title_fragments = re.split(r"[|\n©]|\s[-–—]\s", str(value.get("title") or ""))
    if len(_tokens(bare_name)) < 2 or not any(exact_legal_name(bare_name, fragment.strip()) for fragment in title_fragments):
        return None
    identity_text = str(value.get("identity_text_excerpt") or "")
    normalized = " " + " ".join(_legal_tokens(identity_text)) + " "
    address = record.get("business_address") or {}
    streets = address.get("adresse") or []
    postcode = str(address.get("postnummer") or "")
    if not streets or not re.fullmatch(r"\d{4}", postcode) or f" {postcode} " not in normalized:
        return None
    if not all(" " + " ".join(_legal_tokens(street)) + " " in normalized for street in streets):
        return None
    phone = re.sub(r"\D", "", str(record.get("telephone") or ""))
    if phone.startswith("0047"):
        phone = phone[4:]
    elif len(phone) == 10 and phone.startswith("47"):
        phone = phone[2:]
    if len(phone) != 8 or not re.search(r"(?<!\d)" + r"[ .-]*".join(phone) + r"(?!\d)", identity_text):
        return None
    return {"source_url": value.get("final_url"), "content_sha256": value.get("content_sha256"),
            "extraction_method": "homepage_name_registered_street_postcode_phone_v1",
            "claim_span": identity_text, "registered_name": record["name"], "registered_address": address,
            "registered_telephone": record["telephone"], "registry_source_url": live["source_url"],
            "registry_content_sha256": live["content_sha256"], "registry_retrieved_at": live.get("retrieved_at")}


def _registered_domain_address_proof(profile: dict[str, Any], value: dict[str, Any]) -> dict[str, Any] | None:
    live = profile.get("evidence", {}).get("registry_live") or {}
    record = live.get("value") or {}
    if live.get("status") != "available" or str(record.get("organisation_number")) != str(profile.get("organisation_number")):
        return None
    if not exact_legal_name(record.get("name"), profile.get("name")):
        return None
    if not live.get("source_url") or not live.get("retrieved_at") or not re.fullmatch(r"[0-9a-f]{64}", str(live.get("content_sha256") or "")):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("content_sha256") or "")):
        return None
    registered = str(record.get("website") or "").strip()
    registered = urllib.parse.urlparse(registered if "://" in registered else "https://" + registered)
    homepage = urllib.parse.urlparse(str(value.get("final_url") or ""))
    if not registered.hostname or homepage.scheme not in {"http", "https"}:
        return None
    try:
        registered_host = registered.hostname.encode("idna").decode("ascii").casefold().removeprefix("www.")
        homepage_host = (homepage.hostname or "").encode("idna").decode("ascii").casefold().removeprefix("www.")
    except UnicodeError:
        return None
    if registered_host != homepage_host:
        return None
    if registered.port != homepage.port or registered.path not in {"", "/"}:
        return None
    bare_name = re.sub(r"\s+(?:AS|ASA|ANS|DA|ENK|IKS|SA|NUF)\s*$", "", str(profile.get("name") or ""), flags=re.I)
    fragments = re.split(r"[|\n©]|\s[-–—]\s", str(value.get("title") or ""))
    if not _tokens(bare_name) or not any(exact_legal_name(bare_name, fragment.strip()) for fragment in fragments):
        return None
    address = record.get("business_address") or {}
    streets, postcode = address.get("adresse") or [], str(address.get("postnummer") or "")
    if not streets or not re.fullmatch(r"\d{4}", postcode):
        return None
    for page in value.get("pages") or []:
        parsed = urllib.parse.urlparse(str(page.get("url") or ""))
        if parsed.scheme not in {"http", "https"} or parsed.hostname != homepage.hostname or parsed.port != homepage.port:
            continue
        if not re.search(r"(?:^|[/\s-])(?:contact|kontakt)(?:[/\s-]|$)", parsed.path + " " + str(page.get("title") or ""), re.I):
            continue
        if not page.get("retrieved_at") or not re.fullmatch(r"[0-9a-f]{64}", str(page.get("content_sha256") or "")):
            continue
        span = str(page.get("identity_text_excerpt") or "")
        normalized = " " + " ".join(_legal_tokens(span)) + " "
        if f" {postcode} " not in normalized or not all(" " + " ".join(_legal_tokens(street)) + " " in normalized for street in streets):
            continue
        labelled = re.findall(r"(?:org(?:anisasjons)?\.?\s*(?:nr|nummer)\.?\s*:?|organisation number)\s*((?:NO\s*)?\d{3}[ .-]?\d{3}[ .-]?\d{3})", span, re.I)
        if any(normalize_organisation_number(number) != str(profile["organisation_number"]) for number in labelled):
            continue
        return {"source_url": page["url"], "content_sha256": page["content_sha256"], "retrieved_at": page["retrieved_at"],
                "claim_span": span, "extraction_method": "registered_domain_exact_brand_contact_address_v1",
                "homepage_source_url": value["final_url"], "homepage_content_sha256": value["content_sha256"],
                "registered_website": record["website"], "registered_name": record["name"], "registered_address": address,
                "registry_source_url": live["source_url"], "registry_content_sha256": live["content_sha256"], "registry_retrieved_at": live["retrieved_at"]}
    return None


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    core = _tokens(profile.get("name"))
    hostname = urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").hostname or ""
    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}
    homepage_identity_parts = [
        value.get("title"), value.get("description"), value.get("identity_text_excerpt"), hostname, *structured_names,
        rendered.get("title"),
    ]
    candidate_parts = [
        *homepage_identity_parts, value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
    ]
    candidate_parts.append(rendered.get("main_text_excerpt"))
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(str(part or "") for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    normalized_candidate_text = " ".join(_tokens(candidate_text))
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    homepage_numbers = set(re.findall(r"(?<!\d)(\d{3}[ .]?\d{3}[ .]?\d{3})(?!\d)", homepage_candidate_text))
    homepage_numbers = {re.sub(r"\D", "", number) for number in homepage_numbers}
    labelled_numbers = re.findall(r"(?:org(?:anisasjons)?\.?\s*(?:nr|nummer)?\.?\s*:?|organisation number)\s*((?:NO\s*)?\d{3}[ .-]?\d{3}[ .-]?\d{3})", homepage_candidate_text, re.I)
    structured_numbers = [
        organization[key]
        for organization in value.get("structured_organisations") or []
        if isinstance(organization, dict)
        for key in ("taxID", "vatID")
        if organization.get(key)
    ]
    conflicting_number = any(normalize_organisation_number(number) != org_digits for number in [*labelled_numbers, *structured_numbers])
    homepage_numbers.update(normalize_organisation_number(number) for number in structured_numbers)
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons = []
    parked_markers = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    exact_homepage_name = any(_contains_legal_name(profile.get("name"), part) for part in homepage_identity_parts if part and part != hostname)
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 100
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?\s*$", str(profile.get("name") or ""), re.I))
    operator_proof = _legal_operator_proof(profile, value)
    contact_proof = _registered_contact_proof(profile, value)
    domain_address_proof = _registered_domain_address_proof(profile, value)
    if any(marker in normalized_raw for marker in parked_markers):
        score = 0.1
        reasons.append("captured page is a parked, for-sale, or generic hosting placeholder")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif conflicting_number:
        score = 0.3
        reasons.append("homepage identifies a different legal organisation number")
    elif org_digits and org_digits in homepage_numbers:
        score = 1.0
        reasons.append("exact organisation number appears in homepage identity evidence")
    elif operator_proof:
        score = 1.0
        reasons.append("same-host legal disclosure identifies the exact legal operator and organisation number")
    elif contact_proof:
        score = 0.98
        reasons.append("complete homepage name matches the exact live-registered street, postcode and phone")
    elif domain_address_proof:
        score = 0.98
        reasons.append("exact live-registered domain and homepage brand match a same-host contact-page registered street and postcode")
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.95
        reasons.append("complete ordered legal name appears in homepage identity evidence")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage:
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence with substantive content")
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")
    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "reasons": reasons,
        "identity_evidence": [proof for proof in (operator_proof, contact_proof, domain_address_proof) if proof] if not conflicting_number and status == "exact" else [],
        "method": "deterministic_name_org_evidence_v8",
    }


def assess_social_identity(profile: dict[str, Any], link: dict[str, str]) -> dict[str, Any]:
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    handle_text = urllib.parse.unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    handle_compact = "".join(_tokens(handle_text))
    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    ratio = len(set(matched)) / len(set(core)) if core else 0.0
    suffix = handle_compact[len(core_compact):] if core_compact and handle_compact.startswith(core_compact) else None
    if core_compact and suffix in {"", "official", "com", "no"}:
        score = 0.98
        reason = "social handle equals the normalized legal-name sequence with an optional official/domain suffix"
    else:
        score = 0.3
        reason = "social handle lacks strong exact-entity name evidence"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v3",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    if not assessment["publishable"]:
        website["status"] = "ambiguous"
        website["note"] = "; ".join(assessment["reasons"])
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }
