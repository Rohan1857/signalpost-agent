import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest
from norway_company_agent.jobs import fetch_jobs_module, match_and_filter_jobs
from norway_company_agent.external_footprint import publishable_observation, validate_observation


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
