import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest
from norway_company_agent.jobs import fetch_jobs_module, match_and_filter_jobs
from norway_company_agent.external_footprint import publishable_observation, validate_observation


def test_empty_legacy_nav_search_does_not_assert_zero(monkeypatch):
    from norway_company_agent import jobs
    monkeypatch.setattr(jobs, 'query_nav_jobs', lambda *args, **kwargs: ([], 'ok', {'requests': 1, 'bytes': 2, 'latencies_ms': [1], 'content_sha256': 'a' * 64}))
    record, observations, _ = jobs.fetch_jobs_module({'organisation_number': '923609016', 'name': 'Example AS'})
    assert record['status'] == 'ambiguous'
    assert record['value'] is None
    assert not observations


def test_legacy_nav_job_uses_search_response_hash_and_url(monkeypatch):
    from norway_company_agent import jobs
    source_url = jobs.NAV_STILLINGER_SEARCH_URL + '?q=Example&size=15'
    hits = [{'_source': {'uuid': 'job-1', 'title': 'Engineer', 'employer': {'orgnr': '923609016'}, 'expires': '2099-01-01'}}]
    monkeypatch.setattr(jobs, 'query_nav_jobs', lambda *args, **kwargs: (hits, 'ok', {'requests': 1, 'bytes': 2, 'latencies_ms': [1], 'content_sha256': 'a' * 64, 'source_url': source_url, 'retrieved_at': '2026-10-08T00:00:00Z'}))
    record, observations, _ = jobs.fetch_jobs_module({'organisation_number': '923609016', 'name': 'Example AS'})
    assert record['status'] == 'available'
    assert observations[0]['content_sha256'] == 'a' * 64
    assert observations[0]['source_url'] == source_url
    assert observations[0]['retrieved_at'] == '2026-10-08T00:00:00Z'


def test_match_and_filter_jobs_exact_and_negative():
    profile = {
        "name": "DNB Bank ASA",
        "organisation_number": "984851006",
        "municipality": "OSLO",
    }
    mock_hits = [
        {
            "_source": {
                "uuid": "test-uuid-1",
                "title": "Senior Utvikler",
                "businessName": "DNB Bank ASA",
                "employer": {"orgnr": "984851006", "name": "DNB Bank ASA"},
                "published": "2026-10-01T12:00:00Z",
                "expires": "2026-10-31T00:00:00Z",
                "locationList": [{"city": "OSLO"}],
            }
        },
        {
            "_source": {
                "uuid": "test-uuid-2",
                "title": "Annet selskap stilling",
                "businessName": "SpareBank 1 Østlandet",
                "published": "2026-10-01T12:00:00Z",
                "expires": "2026-10-31T00:00:00Z",
                "locationList": [{"city": "HAMAR"}],
            }
        },
    ]

    verified = match_and_filter_jobs(profile, mock_hits)
    assert len(verified) == 1
    job = verified[0]
    assert job["id"] == "nav-job-test-uuid-1"
    assert job["organisation_number"] == "984851006"
    assert job["exact_entity"] is True

    # Crucial test: must be 100% publishable per external_footprint validation
    reasons = validate_observation(job)
    assert reasons == []
    assert publishable_observation(job) is True


def test_match_and_filter_jobs_rejects_subsidiary_collision():
    profile = {
        "name": "DNB Bank ASA",
        "organisation_number": "984851006",
        "municipality": "OSLO",
    }
    # DNB Bank Eiendom AS should NEVER match DNB Bank ASA
    mock_hits = [
        {
            "_source": {
                "uuid": "test-uuid-eiendom",
                "title": "Eiendomsmegler",
                "businessName": "DNB Bank Eiendom AS",
                "published": "2026-10-01T12:00:00Z",
                "expires": "2028-10-31T00:00:00Z",
            }
        },
        {
            "_source": {
                "uuid": "test-uuid-wrong-org",
                "title": "Analytiker",
                "businessName": "DNB Bank ASA",
                "employer": {"name": "DNB Bank ASA", "orgnr": "911222333"},  # Conflicting orgnr
                "published": "2026-10-01T12:00:00Z",
                "expires": "2028-10-31T00:00:00Z",
            }
        },
        {
            "_source": {
                "uuid": "test-uuid-expired",
                "title": "Gammel stilling",
                "businessName": "DNB Bank ASA",
                "published": "2024-01-01T12:00:00Z",
                "expires": "2024-02-01T00:00:00Z",  # Expired
            }
        },
    ]
    verified = match_and_filter_jobs(profile, mock_hits)
    assert len(verified) == 0, "All collisions and expired jobs must be rejected"


def test_fetch_jobs_module_empty_profile():
    profile = {"name": "", "organisation_number": "123456789"}
    record, obs, metrics = fetch_jobs_module(profile)
    assert record["status"] == "not_applicable"
    assert obs == []


def test_nav_failure_preserves_error_states(monkeypatch):
    import urllib.error
    from norway_company_agent import jobs

    def mock_urlopen_blocked(*args, **kwargs):
        raise urllib.error.HTTPError("https://arbeidsplassen.nav.no", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(jobs.urllib.request, "urlopen", mock_urlopen_blocked)
    record, obs, metrics = jobs.fetch_jobs_module({"name": "Test AS", "organisation_number": "123456789"})
    assert record["status"] == "blocked"
    assert "rate limit" in record["note"]

    def mock_urlopen_500(*args, **kwargs):
        raise urllib.error.HTTPError("https://arbeidsplassen.nav.no", 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(jobs.urllib.request, "urlopen", mock_urlopen_500)
    record, obs, metrics = jobs.fetch_jobs_module({"name": "Test AS", "organisation_number": "123456789"})
    assert record["status"] == "source_error"
    assert "failed" in record["note"]


@pytest.mark.parametrize("override", [
    {"employer": {"orgnr": "999 999 999"}},
    {"employer": {"orgnr": "NO999999999MVA"}},
    {"employer": {"orgnr": "invalid"}},
    {"employer": {"orgnr": "984851006"}, "properties": {"orgnr": "999999999"}},
    {"businessName": "Bank DNB ASA"},
    {"businessName": "DNB Bank AS"},
    {"businessName": "DNB Bank Oslo ASA"},
    {"expires": "2020-01-01"},
    {"expires": "not-a-date"},
    {"status": "INACTIVE"},
])
def test_jobs_reject_identity_and_lifecycle_bypasses(override):
    source = {"uuid": "audit", "businessName": "DNB Bank ASA", "title": "Analyst", **override}
    assert not match_and_filter_jobs({"name": "DNB Bank ASA", "organisation_number": "984851006", "municipality": "OSLO"}, [{"_source": source}])


def test_verified_subunit_employer_is_attributed_to_parent():
    profile = {"name": "Example AS", "organisation_number": "923609016", "evidence": {
        "locations": {"status": "available", "value": {"locations": [{"organisation_number": "973246534"}]}}}}
    hit = {"uuid": "branch", "employer": {"orgnr": "973 246 534", "name": "Example Oslo branch"}, "title": "Engineer"}
    assert match_and_filter_jobs(profile, [hit])[0]["organisation_number"] == "923609016"


@pytest.mark.parametrize("title,footer", [
    ("DNB Bank Eiendom AS", ""),
    ("Bank DNB ASA", ""),
    ("DNB Bank ASA", "Org nr: 999 999 999"),
    ("Parent Group", ""),
])
def test_website_identity_bypasses_are_quarantined(title, footer):
    from norway_company_agent.identity import apply_website_identity_gate
    profile = {"name": "DNB Bank ASA", "organisation_number": "984851006"}
    website = {"status": "available", "source_url": "https://example.no", "value": {
        "title": title, "identity_text_excerpt": footer, "main_text_excerpt": "Useful company information. " * 10,
        "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/dnbbank"}]}}
    result = apply_website_identity_gate(profile, website)
    assert result["website"]["status"] == "ambiguous"
    assert not result["website"]["value"]["social_links"]


def _audit_profile():
    from norway_company_agent.evidence import evidence
    return {"organisation_number": "923609016", "name": "Example AS", "evidence": {
        "registry": evidence("registry", "available", "official_registry", "https://data.brreg.no/example",
                             value={"organisasjonsnummer": "923609016", "navn": "Example AS"}, content_sha256="a" * 64)}}


@pytest.mark.parametrize("damage", ["identity", "claims", "evidence", "span", "value", "hash", "snapshot", "period"])
def test_validator_rejects_material_envelope_corruption(damage):
    from norway_company_agent.batch import terminal_envelope, validate_envelopes
    profile = _audit_profile()
    from norway_company_agent.evidence import evidence
    profile["evidence"]["financials"] = evidence("financials", "available", "official_annual_accounts", "https://data.brreg.no/accounts",
        value={"records": [{"period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "revenue": 100}]}, content_sha256="b" * 64)
    envelope = terminal_envelope(profile, run_id="audit", modules=["registry", "financials"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:01:00Z")
    assert validate_envelopes([envelope], 1)["passed"]
    if damage == "identity": envelope["profile"]["organisation_number"] = "999999999"
    elif damage == "claims": envelope["claims"] = []
    elif damage == "evidence": envelope["evidence"] = []
    elif damage == "span": envelope["evidence"][0]["claim_span"] = ""
    elif damage == "value": envelope["claims"][0]["value"] = "wrong company"
    elif damage == "hash": envelope["evidence"][0]["content_sha256"] = "bad"
    elif damage == "snapshot": envelope["source_snapshots"]["registry"]["value"]["navn"] = "Wrong AS"
    elif damage == "period": next(e for e in envelope["evidence"] if e["snapshot_id"] == "financials")["reporting_period"] = None
    assert not validate_envelopes([envelope], 1)["passed"]


def test_refresh_retains_evidence_and_does_not_turn_failure_into_deletion():
    import copy
    from norway_company_agent.refresh import refresh_profile
    previous = _audit_profile()
    previous["employees"] = 5
    current = copy.deepcopy(previous)
    current["employees"] = None
    current["evidence"]["registry"] = {"status": "source_error"}
    assert refresh_profile(previous, current)["changes"] == []
    assert current["evidence_history"][0]["record"]["value"]["navn"] == "Example AS"
    unchanged = copy.deepcopy(previous)
    assert refresh_profile(previous, unchanged)["changes"] == []
    changed = copy.deepcopy(previous)
    changed["employees"] = 6
    changed["evidence"]["registry"]["content_sha256"] = "b" * 64
    assert refresh_profile(previous, changed)["changes"][0]["field"] == "registry.employees"


def test_only_explicit_current_structured_vacancies_are_published():
    from norway_company_agent.jobs import extract_structured_site_jobs
    profile = _audit_profile()
    job = {"@type": "JobPosting", "title": "Engineer", "description": "A specific engineering vacancy",
           "datePosted": "2026-01-01", "validThrough": "2099-12-31", "hiringOrganization": {"name": "Example AS"}}
    page = {"url": "https://example.no/careers", "title": "Careers: no vacancies", "content_sha256": "a" * 64, "structured_jobs": []}
    profile["evidence"]["website"] = {"status": "available", "source_url": page["url"], "retrieved_at": "2026-10-08T00:00:00Z",
        "value": {"identity_assessment": {"publishable": True}, "pages": [page]}}
    assert extract_structured_site_jobs(profile) == []
    page["structured_jobs"] = [job]
    assert len(extract_structured_site_jobs(profile)) == 1
    job["hiringOrganization"]["name"] = "Example Holding AS"
    assert extract_structured_site_jobs(profile) == []


def test_review_connector_stays_quarantined(tmp_path):
    from scripts.run_fagfolkguiden_reviews_connector import fetch
    profile = _audit_profile()
    (tmp_path / "923609016.html").write_text('<html>Example AS 923609016<script type="application/ld+json">{"aggregateRating":{"ratingValue":4,"ratingCount":3}}</script></html>')
    observations, status = fetch(profile, tmp_path)
    assert observations and status["rated"]
    assert all(not publishable_observation(o) for o in observations)


def test_batch_refresh_compares_real_inputs_and_preserves_previous_evidence(tmp_path, monkeypatch):
    import csv
    import json
    from scripts import run_competition_batch as runner
    fields = ["organisasjonsnummer", "navn", "antallAnsatte", "organisasjonsform.kode"]
    bulk = tmp_path / "registry.csv"
    wanted = tmp_path / "companies.txt"
    wanted.write_text("923609016\n")
    previous = tmp_path / "previous.jsonl"
    current = tmp_path / "current.jsonl"
    output = tmp_path / "envelopes.jsonl"
    report = tmp_path / "report.json"

    def write_bulk(employees):
        with bulk.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
            writer.writeheader()
            writer.writerow(dict(zip(fields, ["923609016", "Example AS", str(employees), "AS"])))

    base_args = ["runner", "--organisations", str(wanted), "--bulk", str(bulk), "--output", str(output),
                 "--report", str(report), "--run-id", "refresh-test", "--modules", "registry,accounting_obligation"]
    write_bulk(5)
    monkeypatch.setattr(sys, "argv", base_args + ["--profiles-output", str(previous)])
    with pytest.raises(SystemExit) as first:
        runner.main()
    assert first.value.code == 0
    write_bulk(6)
    monkeypatch.setattr(sys, "argv", base_args + ["--profiles-output", str(current), "--previous-profiles", str(previous)])
    with pytest.raises(SystemExit) as second:
        runner.main()
    assert second.value.code == 0
    envelope = json.loads(output.read_text())
    assert envelope["changes"][0]["field"] == "registry.employees"
    assert envelope["profile"]["evidence_history"]
    monkeypatch.setattr(sys, "argv", base_args + ["--profiles-output", str(previous), "--previous-profiles", str(current)])
    with pytest.raises(SystemExit) as replay:
        runner.main()
    assert replay.value.code == 0
    assert json.loads(output.read_text())["changes"] == []


@pytest.mark.parametrize('compressed', [False, True])
def test_bulk_extra_cells_cannot_crash_or_publish_unlabelled_values(tmp_path, monkeypatch, compressed):
    import gzip
    import hashlib
    import json
    from scripts import run_competition_batch as runner
    from norway_company_agent.batch import profiles_from_bulk
    bulk = tmp_path / ('registry.csv.gz' if compressed else 'registry.csv')
    text = 'organisasjonsnummer;navn;organisasjonsform.kode\n'
    text += ''.join(f'{100000000+i};Padding {i} AS;AS\n' for i in range(100))
    text += '923609016;Example AS;AS;;;\n987445378;Questionable AS;AS;unlabelled-value\n'
    if compressed:
        with gzip.open(bulk, 'wt', encoding='utf-8', newline='') as handle:
            handle.write(text)
    else:
        bulk.write_text(text, encoding='utf-8')
    profiles, metadata = profiles_from_bulk(bulk, ['923609016', '987445378'])
    good, bad = profiles
    assert good['evidence']['registry']['status'] == 'available'
    assert None not in good['evidence']['registry']['value']
    assert good['evidence']['registry']['value']['navn'] == 'Example AS'
    assert good['evidence']['registry']['content_sha256'] == hashlib.sha256(bulk.read_bytes()).hexdigest()
    assert bad['evidence']['registry']['status'] == 'source_error'
    assert bad['evidence']['registry']['value'] is None
    assert bad['name'] is None
    assert bad['evidence']['accounting_obligation']['status'] == 'ambiguous'
    assert bad['evidence']['registry']['source_parse_diagnostics']['unlabelled_cells'] == ['unlabelled-value']
    assert len(metadata['registry_parse_warnings']) == 2
    wanted = tmp_path / 'companies.txt'
    wanted.write_text('923609016\n987445378\n')
    output = tmp_path / 'envelopes.jsonl'
    report = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['runner', '--organisations', str(wanted), '--bulk', str(bulk), '--profiles-output', str(tmp_path / 'profiles.jsonl'), '--output', str(output), '--report', str(report), '--run-id', 'extra-csv-cells', '--modules', 'registry,accounting_obligation'])
    with pytest.raises(SystemExit) as result:
        runner.main()
    assert result.value.code == 0
    envelopes = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(envelopes) == 2
    assert json.loads(report.read_text())['validation']['passed']
    assert all('unlabelled-value' not in json.dumps(claim) for item in envelopes for claim in item['claims'])


def test_worker_failure_still_emits_a_terminal_envelope(tmp_path, monkeypatch):
    import json
    from scripts import run_competition_batch as runner
    monkeypatch.setattr(runner, "profiles_from_bulk", lambda *_args: ([_audit_profile()], {}))
    def broken(*_args):
        raise RuntimeError("controlled worker failure")
    monkeypatch.setattr(runner, "fetch_official_modules", broken)
    wanted = tmp_path / "companies.txt"
    wanted.write_text("923609016\n")
    output = tmp_path / "envelopes.jsonl"
    monkeypatch.setattr(sys, "argv", ["runner", "--organisations", str(wanted), "--bulk", "unused.csv", "--output", str(output),
                                     "--report", str(tmp_path / "report.json"), "--profiles-output", str(tmp_path / "profiles.jsonl"),
                                     "--run-id", "failure-test", "--modules", "registry,financials"])
    with pytest.raises(SystemExit) as result:
        runner.main()
    assert result.value.code == 1
    envelope = json.loads(output.read_text())
    assert envelope["state"] == "failed"
    assert "controlled worker failure" in envelope["errors"][0]["error"]


def test_social_handle_cannot_collapse_a_subsidiary_into_parent():
    from norway_company_agent.identity import assess_social_identity
    profile = {"name": "DNB Bank ASA"}
    assert not assess_social_identity(profile, {"platform": "linkedin", "url": "https://linkedin.com/company/dnbbankeiendom"})["publishable"]


def test_nonsemantic_footer_preserves_identity_without_relaxing_company_match():
    from bs4 import BeautifulSoup
    from norway_company_agent.website import _identity_text
    from norway_company_agent.identity import assess_website_identity
    soup = BeautifulSoup('<div role="contentinfo"><div class="footer">Example AS | Org. nr. 923 609 016</div></div>', 'html.parser')
    text = _identity_text(soup)
    assert text.count('923 609 016') == 1
    website = {'status': 'available', 'value': {'title': 'Example', 'identity_text_excerpt': text}}
    assert assess_website_identity({'name': 'Example AS', 'organisation_number': '923609016', 'evidence': {'website': website}})['publishable']
    assert not assess_website_identity({'name': 'Example Holding AS', 'organisation_number': '999999999', 'evidence': {'website': website}})['publishable']


def test_structured_tax_identifier_rejects_wrong_company_even_with_matching_name():
    from norway_company_agent.identity import assess_website_identity
    website = {'status': 'available', 'value': {'title': 'Example AS', 'structured_organisations': [{'@type': 'Organization', 'legalName': 'Example AS', 'vatID': 'NO 999 999 999 MVA'}]}}
    profile = {'name': 'Example AS', 'organisation_number': '923609016', 'evidence': {'website': website}}
    assert not assess_website_identity(profile)['publishable']
    website['value']['structured_organisations'][0]['vatID'] = 'NO 923 609 016 MVA'
    assert assess_website_identity(profile)['publishable']


@pytest.mark.parametrize('code,state', [(403, 'blocked'), (429, 'blocked'), (500, 'source_error'), (404, 'not_found')])
def test_website_http_errors_preserve_availability(code, state, monkeypatch):
    import urllib.error
    from norway_company_agent import website
    monkeypatch.setattr(website, 'assert_public_url', lambda *_args: None)
    monkeypatch.setattr(website, '_robots_allowed', lambda *_args: True)
    def refused(*_args, **_kwargs):
        raise urllib.error.HTTPError('https://example.no/', code, 'controlled test', {}, None)
    monkeypatch.setattr(website.SAFE_OPENER, 'open', refused)
    record, metrics = website.fetch_website('https://example.no/')
    assert record['status'] == state
    assert metrics['requests'] == 2


def test_domain_candidates_are_bounded_unique_and_include_com():
    from scripts.run_competition_batch import generate_domain_candidates
    candidates = generate_domain_candidates('Example Invest AS')
    assert len(candidates) <= 4
    assert len(set(candidates)) == len(candidates)
    assert 'https://exampleinvest.com' in candidates
    assert generate_domain_candidates('AS') == []


def test_alternative_discovery_is_bounded_and_never_an_identity_proof():
    from scripts.run_competition_batch import generate_alternative_website_candidates, generate_website_candidates
    from norway_company_agent.identity import apply_website_identity_gate
    from norway_company_agent.evidence import evidence
    assert generate_alternative_website_candidates('INSTITUTT FOR HELHETSMEDISIN AS') == ['https://ifh.no']
    assert generate_alternative_website_candidates('KLIKKBAR MEDIA AS') == ['https://klikkbar.no']
    assert generate_alternative_website_candidates('BYBRUA FUS BARNEHAGE AS') == ['https://fus.no/bybrua']
    assert generate_alternative_website_candidates('FAGER SKOG FUS BARNEHAGE AS') == ['https://fus.no/fagerskog', 'https://fus.no/fager-skog']
    assert all('/' not in url.removeprefix('https://') for url in generate_alternative_website_candidates('FUS BARNEHAGE HOLDING AS'))
    assert generate_alternative_website_candidates('AS') == []
    profile = {'organisation_number': '923609016', 'name': 'Example Media AS', 'evidence': {}}
    candidates = generate_website_candidates(profile)
    assert candidates[0] == 'https://examplemedia.no'
    assert candidates[1] == 'https://example.no'
    assert len(candidates) <= 8 and len(candidates) == len(set(candidates))
    wrong = evidence('website', 'available', 'company_site', candidates[1], content_sha256='a' * 64,
                     value={'final_url': candidates[1], 'title': 'Example AS', 'identity_text_excerpt': 'Example AS Org nr 999999999'})
    assert apply_website_identity_gate(profile, wrong)['website']['status'] == 'ambiguous'


@pytest.mark.parametrize('reference', ['https://attacker.test/api/v1/feed', 'http://pam-stilling-feed.nav.no/api/v1/feed', 'https://secret@pam-stilling-feed.nav.no/api/v1/feed', 'https://pam-stilling-feed.nav.no:444/api/v1/feed', '/outside'])
def test_feed_urls_cannot_forward_credentials_outside_nav(reference):
    from norway_company_agent.jobs import _nav_feed_url
    with pytest.raises(ValueError):
        _nav_feed_url(reference)


@pytest.mark.parametrize('inactive,wrong_id,stale_prefix,branch_name', [(False, False, False, False), (True, False, False, False), (False, True, False, False), (False, False, True, False), (False, False, False, True), (False, True, False, True), (False, False, False, 'priority')])
def test_documented_feed_uses_latest_detail_and_preserves_real_hash(monkeypatch, inactive, wrong_id, stale_prefix, branch_name):
    import json
    import hashlib
    from norway_company_agent import jobs
    token = 'eyJ0.e30.test_signature'
    entry = {'url': '/api/v1/feedentry/job-1', '_feed_entry': {'uuid': 'job-1', 'businessName': 'Example AS', 'status': 'ACTIVE'}}
    if branch_name:
        entry['_feed_entry']['businessName'] = 'Example Services avd. Oslo'
    if branch_name == 'priority':
        entry['_feed_entry']['businessName'] = 'Example Services AS'
    page = {'items': [entry], 'next_url': None}
    if branch_name == 'priority':
        page['items'].insert(0, {'url': '/api/v1/feedentry/lower-priority', '_feed_entry': {'uuid': 'lower-priority', 'businessName': 'Example Services avd. Bergen', 'status': 'ACTIVE'}})
    detail = {'uuid': 'job-1', 'status': 'INACTIVE' if inactive else 'ACTIVE', 'ad_content': {'uuid': 'job-1', 'title': 'Engineer', 'published': '2026-10-01T00:00:00Z', 'expires': '2099-12-31T00:00:00Z', 'employer': {'name': 'Example AS', 'orgnr': '999999999' if wrong_id else '923609016'}, 'workLocations': [{'city': 'Oslo'}]}}
    if inactive:
        detail.pop('ad_content')  # Stopped vacancies mask their employer/content.
    detail_bytes = json.dumps(detail).encode()
    payloads = {'/api/publicToken': ('Public token for experiments\n' + token + '\n').encode(), '/api/v1/feed': json.dumps(page).encode(), '/api/v1/feedentry/job-1': detail_bytes}
    if stale_prefix:
        page['items'].insert(0, {'url': '/api/v1/feedentry/stopped', '_feed_entry': {'uuid': 'stopped', 'businessName': 'Example AS', 'status': 'ACTIVE'}})
        payloads['/api/v1/feed'] = json.dumps(page).encode()
        payloads['/api/v1/feedentry/stopped'] = json.dumps({'uuid': 'stopped', 'status': 'INACTIVE'}).encode()
    calls = []
    class Response:
        def __init__(self, url, raw): self.url, self.raw = url, raw
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self, *_args): return self.raw
        def geturl(self): return self.url
    class Opener:
        def open(self, request, **_kwargs):
            from urllib.parse import urlparse
            calls.append(request)
            return Response(request.full_url, payloads[urlparse(request.full_url).path])
    monkeypatch.setattr(jobs.urllib.request, 'build_opener', lambda *_args: Opener())
    profile = _audit_profile()
    if branch_name:
        profile['name'] = 'Example Services AS'
    snapshot = jobs.fetch_nav_feed_snapshot([profile], max_pages=1, max_details=1 if branch_name == 'priority' else 2)
    if branch_name == 'priority':
        assert snapshot['candidate_count'] == 2
        assert not snapshot['details_complete']
        assert all('lower-priority' not in request.full_url for request in calls)
    assert snapshot['metrics']['requests'] == 3 + int(stale_prefix)
    assert snapshot['scan_complete']
    assert snapshot['status'] == 'ambiguous'  # Name-hint scan is never exhaustive per-company coverage.
    assert token not in json.dumps(snapshot)
    monkeypatch.setattr(jobs, 'query_nav_jobs', lambda *_args, **_kwargs: pytest.fail('Legacy endpoint must not be called in feed mode'))
    record, observations, metrics = jobs.fetch_jobs_module(profile, feed_snapshot=snapshot)
    postings = [item for item in observations if item['signal_type'] == 'job_posting']
    assert metrics['requests'] == 0
    if inactive or wrong_id:
        assert postings == []
        assert record['status'] == 'ambiguous'
        assert record['value'] is None
    else:
        assert record['status'] == 'available'
        assert record['value']['job_count_scope'] == 'observed_verified_vacancies'
        assert postings[0]['content_sha256'] == hashlib.sha256(detail_bytes).hexdigest()
        assert postings[0]['source_url'].endswith('/api/v1/feedentry/job-1')


def test_public_token_parser_rejects_multiple_tokens_without_echoing_them():
    from norway_company_agent.jobs import _public_nav_token
    with pytest.raises(ValueError):
        _public_nav_token('eyJ0.e30.one eyJ0.e30.two')


def test_feed_name_hints_rank_exact_matches_and_exclude_partial_names():
    from norway_company_agent.jobs import _nav_feed_name_priority
    names = {'example services', 'example'}
    assert _nav_feed_name_priority('Example Services AS', names) == 0
    assert _nav_feed_name_priority('Example Services avd. Oslo', names) == 1
    assert _nav_feed_name_priority('Example Oslo', names) is None
    assert _nav_feed_name_priority('Other Example Services', names) is None
    assert _nav_feed_name_priority('Example Service', names) is None


def test_bounded_vacancy_snapshot_does_not_claim_unobserved_job_was_deleted():
    import copy
    from norway_company_agent.refresh import diff_profile
    from norway_company_agent.evidence import evidence
    old = _audit_profile()
    old['evidence']['jobs'] = evidence('jobs', 'available', 'official_nav_vacancy_feed', 'https://pam-stilling-feed.nav.no/api/v1/feed', value={'active_postings': [{'id': 'nav-job-old'}]}, content_sha256='a' * 64)
    current = copy.deepcopy(old)
    current['evidence']['jobs']['content_sha256'] = 'b' * 64
    current['evidence']['jobs']['value'] = {'active_postings': [{'id': 'nav-job-new'}], 'job_count_scope': 'observed_verified_vacancies'}
    assert diff_profile(old, current) == []
    current['evidence']['jobs']['value']['active_postings'].append({'id': 'nav-job-old'})
    assert diff_profile(old, current)[0]['field'] == 'jobs.active_postings'


def test_failed_subunit_lookup_cannot_authorize_a_job_publication():
    profile = {'name': 'Example AS', 'organisation_number': '923609016', 'evidence': {'locations': {'status': 'source_error', 'value': {'locations': [{'organisation_number': '973246534'}]}}}}
    hit = {'uuid': 'branch', 'employer': {'orgnr': '973246534'}, 'title': 'Engineer'}
    assert match_and_filter_jobs(profile, [hit]) == []


def test_workforce_observation_uses_source_snapshot_hash_and_date():
    from norway_company_agent.jobs import extract_brreg_workforce_snapshot
    from norway_company_agent.evidence import evidence
    profile = {'organisation_number': '923609016', 'employees': 999, 'evidence': {'registry': evidence('registry', 'available', 'official_registry_bulk', 'https://example.test/bulk', value={'organisasjonsnummer': '923609016', 'antallAnsatte': '12'}, retrieved_at='2025-12-31T00:00:00Z', content_sha256='a'*64)}}
    observation = extract_brreg_workforce_snapshot(profile)
    assert observation['metrics']['employees'] == 12
    assert observation['retrieved_at'] == '2025-12-31T00:00:00Z'
    assert observation['content_sha256'] == 'a'*64
    assert observation['source_url'] == 'https://example.test/bulk'
    assert observation['evidence_span'] == '"12"'
    profile['evidence']['registry']['value']['organisasjonsnummer'] = '999999999'
    assert extract_brreg_workforce_snapshot(profile) is None


def test_feed_batch_prefetches_subunit_names_and_reuses_requests(tmp_path, monkeypatch):
    import json
    from scripts import run_competition_batch as runner
    from norway_company_agent.evidence import evidence
    profile = _audit_profile()
    calls = []
    monkeypatch.setattr(runner, 'profiles_from_bulk', lambda *_args: ([profile], {}))
    def official(org, modules):
        calls.append(modules)
        if 'locations' in modules:
            return {'locations': evidence('locations', 'available', 'official_subunits', 'https://data.brreg.no/subunits', value={'locations': [{'name': 'Example Oslo branch', 'organisation_number': '973246534'}]}, content_sha256='c'*64)}, []
        return {}, []
    monkeypatch.setattr(runner, 'fetch_official_modules', official)
    def feed(profiles, **_kwargs):
        assert profiles[0]['evidence']['locations']['value']['locations'][0]['name'] == 'Example Oslo branch'
        return {'hits': [{'uuid': 'branch', 'employer': {'orgnr': '973246534'}, 'title': 'Engineer'}], 'status': 'ambiguous', 'metrics': {'requests': 2, 'bytes': 100, 'latencies_ms': [1, 2]}}
    monkeypatch.setattr(runner, 'fetch_nav_feed_snapshot', feed)
    wanted = tmp_path / 'companies.txt'
    wanted.write_text('923609016\n')
    report = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['runner', '--organisations', str(wanted), '--bulk', 'unused.csv', '--output', str(tmp_path / 'envelopes.jsonl'), '--report', str(report), '--profiles-output', str(tmp_path / 'profiles.jsonl'), '--run-id', 'subunit-feed', '--modules', 'registry,locations,jobs', '--nav-feed', 'public'])
    with pytest.raises(SystemExit) as result:
        runner.main()
    assert result.value.code == 0
    assert sum('locations' in modules for modules in calls) == 1
    result_report = json.loads(report.read_text())
    assert result_report['operations']['requests'] == 2
    assert result_report['coverage']['job_postings'] == 1
    assert sum(result_report['coverage']['jobs_states'].values()) == 1


def test_registered_email_domains_are_only_bounded_discovery_hints():
    from scripts.run_competition_batch import generate_website_candidates
    profile = {'name': 'Example AS', 'evidence': {'registry': {'status': 'available', 'value': {'epostadresse': 'contact@accountant.no'}}}}
    candidates = generate_website_candidates(profile)
    assert candidates[0] == 'https://accountant.no'
    assert len(candidates) <= 5
    for email in ('contact@gmail.com', 'contact@outlook.com', 'not an email', 'x@example.no/secret'):
        profile['evidence']['registry']['value']['epostadresse'] = email
        assert generate_website_candidates(profile) == ['https://example.no', 'https://example.com']
    profile['evidence']['registry']['value']['epostadresse'] = 'x@accountant.no'
    profile['evidence']['registry']['status'] = 'blocked'
    assert 'https://accountant.no' not in generate_website_candidates(profile)


def test_live_registered_email_hint_requires_matching_entity():
    from scripts.run_competition_batch import generate_website_candidates
    from norway_company_agent.official import normalize_entity
    profile = {'organisation_number': '923609016', 'name': 'Example AS', 'evidence': {'registry_live': {'status': 'available', 'value': normalize_entity({
        'organisasjonsnummer': '923609016', 'epostadresse': 'public@brand.no', 'telefon': '12345678',
    })}}}
    assert generate_website_candidates(profile)[0] == 'https://brand.no'
    assert profile['evidence']['registry_live']['value']['telephone'] == '12345678'
    profile['evidence']['registry_live']['value']['organisation_number'] = '999888777'
    assert 'https://brand.no' not in generate_website_candidates(profile)


def test_synthesis_uses_registry_business_activity_with_its_own_snapshot_date():
    from norway_company_agent.research import answer_profile
    from norway_company_agent.evidence import evidence
    bulk = evidence('registry', 'available', 'official_registry_bulk', 'https://example.test/bulk', value={'aktivitet': 'Manufactures equipment.', 'epostadresse': 'contact@example.no'}, retrieved_at='2026-01-01T00:00:00Z', content_sha256='a' * 64)
    live = evidence('registry_live', 'available', 'official_registry', 'https://example.test/live', value={'name': 'Example AS'}, retrieved_at='2026-02-01T00:00:00Z', content_sha256='b' * 64)
    profile = {'name': 'Example AS', 'organisation_number': '923609016', 'evidence': {'registry': bulk, 'registry_live': live}}
    facts = answer_profile(profile, 'Summarize the company')['facts']
    activity = next(fact for fact in facts if fact['claim'] == 'Registered business activity')
    assert activity['value'] == 'Manufactures equipment.'
    assert activity['source_url'] == bulk['source_url']
    assert activity['retrieved_at'] == bulk['retrieved_at']
    bulk['status'] = 'blocked'
    assert not any(fact['claim'] == 'Registered business activity' for fact in answer_profile(profile, 'Summarize the company')['facts'])


def test_default_evaluator_run_does_not_use_a_personal_search_key(tmp_path, monkeypatch):
    import json
    from scripts import run_competition_batch as runner
    from norway_company_agent.evidence import evidence
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'must-not-be-used')
    monkeypatch.setattr(runner, 'profiles_from_bulk', lambda *_args: ([_audit_profile()], {}))
    monkeypatch.setattr(runner, 'fetch_official_modules', lambda *_args: ({}, []))
    monkeypatch.setattr(runner, 'fetch_website', lambda url: (evidence('website', 'not_found', 'company_site', url), {'requests': 0, 'bytes': 0, 'latencies_ms': []}))
    def unexpected(*_args, **_kwargs):
        raise AssertionError('Default evaluator run must not invoke Brave')
    monkeypatch.setattr(runner, 'brave_search', unexpected)
    wanted = tmp_path / 'companies.txt'
    wanted.write_text('923609016\n')
    report = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['runner', '--organisations', str(wanted), '--bulk', 'unused.csv', '--output', str(tmp_path / 'envelopes.jsonl'), '--report', str(report), '--profiles-output', str(tmp_path / 'profiles.jsonl'), '--run-id', 'no-personal-key', '--modules', 'registry,website'])
    with pytest.raises(SystemExit) as result:
        runner.main()
    assert result.value.code == 0
    result_report = json.loads(report.read_text())
    assert result_report['validation']['passed']
    assert result_report['search_requests'] == 0
    assert result_report['gross_third_party_cost_usd'] == 0


@pytest.mark.parametrize('render_succeeds', [True, False])
def test_discovered_shell_renders_once_and_preserves_failure(tmp_path, monkeypatch, render_succeeds):
    import json
    from scripts import run_competition_batch as runner
    from norway_company_agent.evidence import evidence
    from norway_company_agent.website import _rendered_website_record
    monkeypatch.setattr(runner, 'profiles_from_bulk', lambda *_args: ([_audit_profile()], {}))
    monkeypatch.setattr(runner, 'fetch_official_modules', lambda *_args: ({}, []))
    monkeypatch.setattr(runner, 'generate_website_candidates', lambda _profile: ['https://example.no', 'https://example.com'])
    def fetch(url):
        return evidence('website', 'available', 'company_site', url, content_sha256='a' * 64,
                        value={'title': 'Loading', 'final_url': url, 'extraction_state': 'js_fallback_candidate'}), {'requests': 1, 'bytes': 20, 'latencies_ms': [1]}
    monkeypatch.setattr(runner, 'fetch_website', fetch)
    renders = []
    def render(record):
        renders.append(record['source_url'])
        if render_succeeds:
            record = _rendered_website_record(record['source_url'], b'<html><body><footer>Example AS organisation number 923609016</footer></body></html>', b'<html></html>', record['source_url'])
        else:
            record['value']['browser_error'] = {'status': 'source_error', 'error': 'TimeoutError', 'source_url': record['source_url']}
        return record, {'requests': 1, 'bytes': 100, 'latencies_ms': [5]}
    monkeypatch.setattr(runner, 'fetch_rendered_website', render)
    monkeypatch.setattr(runner, 'fetch_site_activity_pages', lambda _record: {'requests': 0, 'bytes': 0, 'latencies_ms': []})
    wanted = tmp_path / 'companies.txt'
    wanted.write_text('923609016\n')
    report = tmp_path / 'report.json'
    profiles = tmp_path / 'profiles.jsonl'
    monkeypatch.setattr(sys, 'argv', ['runner', '--organisations', str(wanted), '--bulk', 'unused.csv', '--output', str(tmp_path / 'envelopes.jsonl'), '--report', str(report), '--profiles-output', str(profiles), '--run-id', 'discovered-shell', '--modules', 'registry,website'])
    with pytest.raises(SystemExit) as result:
        runner.main()
    assert result.value.code == 0
    assert renders == ['https://example.no']
    result_report = json.loads(report.read_text())
    profile = json.loads(profiles.read_text())
    assert result_report['validation']['passed']
    if render_succeeds:
        assert result_report['browser']['rendered_websites'] == 1
        assert profile['evidence']['website']['status'] == 'available'
    else:
        assert result_report['browser']['source_errors'] == 1
        assert profile['evidence']['website']['status'] == 'ambiguous'
        assert profile['connector_errors'][0]['error'] == 'TimeoutError'


def test_brave_discovery_is_capped_and_does_not_persist_provider_data(tmp_path, monkeypatch):
    import json
    from scripts import run_competition_batch as runner
    from norway_company_agent.evidence import evidence
    calls = []
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-only-secret")
    monkeypatch.setattr(runner, "profiles_from_bulk", lambda *_args: ([_audit_profile()], {}))
    monkeypatch.setattr(runner, "fetch_official_modules", lambda *_args: ({}, []))
    def search(profile, key, **_kwargs):
        calls.append(key)
        return [{"url": "https://example.no", "title": "Example AS", "snippet": "provider-only-snippet", "rank": 1}], {"bytes": 50, "latency_ms": 1}
    monkeypatch.setattr(runner, "brave_search", search)
    fetched = []
    def fetch(url, **_kwargs):
        fetched.append(url)
        metrics = {"requests": 2, "bytes": 100, "latencies_ms": [1]}
        if len(fetched) == 1:
            return evidence("website", "not_found", "company_site", url), metrics
        return evidence("website", "available", "company_site", url, content_sha256="b" * 64,
                        value={"title": "Example AS", "final_url": url, "content_sha256": "b" * 64}), metrics
    monkeypatch.setattr(runner, "fetch_website", fetch)
    wanted = tmp_path / "companies.txt"
    wanted.write_text("923609016\n")
    output = tmp_path / "envelopes.jsonl"
    monkeypatch.setattr(sys, "argv", ["runner", "--organisations", str(wanted), "--bulk", "unused.csv", "--output", str(output),
                                     "--report", str(tmp_path / "report.json"), "--profiles-output", str(tmp_path / "profiles.jsonl"),
                                     "--run-id", "search-test", "--modules", "registry,website", "--search-limit", "1"])
    with pytest.raises(SystemExit) as result:
        runner.main()
    assert result.value.code == 0
    assert len(calls) == 1
    serialized = output.read_text()
    assert "test-only-secret" not in serialized and "provider-only-snippet" not in serialized
    assert json.loads(serialized)["operations"]["third_party_cost_usd"] == 0.005
