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


def test_fetch_jobs_module_empty_profile():
    profile = {"name": "", "organisation_number": "123456789"}
    record, obs, metrics = fetch_jobs_module(profile)
    assert record["status"] == "not_applicable"
    assert obs == []
