from __future__ import annotations

import json
import gzip
import csv
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.crawl_events import extract_page_event, merge_profile_events, missing_seed_error_events  # noqa: E402
from norway_company_agent.discovery import build_company_search_query, choose_search_candidate, parse_brave_web_results, score_search_candidate  # noqa: E402
from norway_company_agent.official import _reserve_history_slot, accounting_obligation_assessment, normalize_entity, normalize_financial_history, normalize_financials, normalize_roles  # noqa: E402
from norway_company_agent.operations import domain_request_summary, latency_summary, percentile  # noqa: E402
from norway_company_agent.sampling import deterministic_extension_sample, deterministic_financial_filer_sample, deterministic_website_audit_sample, financial_filer_eligible, normalize_row, stratum  # noqa: E402
from norway_company_agent.research import answer_profile, parse_screen_query, screen_profiles  # noqa: E402
from norway_company_agent.workspace import load_workspace, record_screen, save_workspace  # noqa: E402
from norway_company_agent.refresh import diff_datasets, diff_profile  # noqa: E402
from norway_company_agent.sentiment import aggregate_company_sentiment, evaluate_predictions, publishable_sentiment_item, sentiment_input_eligibility  # noqa: E402
from norway_company_agent.external_footprint import aggregate_footprint, publishable_observation, validate_observation  # noqa: E402
from norway_company_agent.external_tasks import plan_external_tasks  # noqa: E402
from norway_company_agent.external_control import development_score, run_company_control, strategy_order  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate, assess_social_identity, assess_website_identity  # noqa: E402
from norway_company_agent.website import _extraction_state, _priority_links, _social_links, assert_public_url, normalize_homepage, normalize_social_url, structured_social_links  # noqa: E402
from norway_company_agent.batch import evidence_terminal_state, profile_complete_for_modules, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.snapshots import SnapshotFetcher  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from scripts.build_prototype import compact as compact_prototype, qualification_copy  # noqa: E402
from scripts.run_brave_discovery import brave_search  # noqa: E402
from scripts.run_annual_report_workforce_connector import extract_candidate, needs_ocr  # noqa: E402
from scripts.normalize_google_maps_results import candidate_score  # noqa: E402
from scripts.run_scrapy_websites import terminal_events_for_run  # noqa: E402
from scripts.run_sentiment_model import MODEL_REVISION, normalize_generated_label  # noqa: E402
from scripts.score_company_completeness import score_rows, summarize  # noqa: E402
from scripts.extract_company_site_activity import observation as site_activity_observation  # noqa: E402
from scripts.extract_company_site_news import observation as site_news_observation  # noqa: E402
from scripts.build_verified_observations import build as build_verified_observations  # noqa: E402
from scripts.run_google_news_rss_connector import exact_title_match  # noqa: E402
from scripts.run_linkedin_guest_jobs_connector import canonical_company_url, parse_detail_company_urls, parse_job_cards, parse_typeahead  # noqa: E402
from scripts.run_linkedin_guest_experiment import (  # noqa: E402
    assess_profile_identity as assess_linkedin_profile_identity,
    extract_profile as extract_linkedin_profile,
    legal_name_profile_url,
)
from scripts.run_fagfolkguiden_reviews_connector import extract_aggregate_rating, slug  # noqa: E402
from scripts.discover_linkedin_company_profiles import (  # noqa: E402
    discovery_identity as linkedin_discovery_identity,
    normalized_full_name as linkedin_normalized_full_name,
    official_site_aliases as linkedin_official_site_aliases,
    parse_exact_typeahead as parse_linkedin_exact_typeahead,
)


class EvidenceTests(unittest.TestCase):
    def test_missing_is_not_zero_and_provenance_is_required(self):
        record = evidence("financials", "not_found", "official_annual_accounts", "https://example.test/123")
        self.assertIsNone(record["value"])
        self.assertNotEqual(record["value"], 0)
        self.assertTrue(record["source_url"])
        self.assertTrue(record["retrieved_at"])

    def test_available_zero_is_preserved(self):
        record = evidence("employees", "available", "official_registry_bulk", "https://example.test", value=0)
        self.assertEqual(record["value"], 0)
        self.assertEqual(record["status"], "available")

    def test_content_hash_can_be_carried_with_evidence(self):
        record = evidence("entity", "available", "official", "https://example.test", value={}, content_sha256="a" * 64, source_row_key="999999999")
        self.assertEqual(record["content_sha256"], "a" * 64)
        self.assertEqual(record["source_class"], "official")
        self.assertEqual(record["source_row_key"], "999999999")

    def test_not_fetched_is_distinct_from_not_applicable(self):
        record = evidence("history", "not_fetched", "official", "https://example.test", note="No filing flag in snapshot")
        self.assertEqual(record["status"], "not_fetched")
        self.assertNotEqual(record["status"], "not_applicable")


class ExternalFootprintTests(unittest.TestCase):
    def observation(self, **changes):
        base = {
            "id": "obs-1",
            "organisation_number": "923609016",
            "platform": "google_places",
            "signal_type": "review",
            "source_url": "https://maps.google.com/example",
            "retrieved_at": "2026-08-20T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "address_match", "value": "Oslo"}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "customer_review",
            "evidence_span": "Helpful staff",
        }
        return {**base, **changes}

    def test_publication_requires_rights_identity_hash_and_span(self):
        self.assertTrue(publishable_observation(self.observation()))
        bad = self.observation(exact_entity=False, content_sha256=None, evidence_span=None, rights_status="unknown")
        reasons = validate_observation(bad)
        self.assertIn("exact legal entity is not verified", reasons)
        self.assertIn("missing content hash", reasons)
        self.assertIn("missing evidence span", reasons)
        self.assertIn("source rights are not approved", reasons)

    def test_unofficial_scraper_output_is_experimental_not_publishable(self):
        item = self.observation(platform="linkedin", signal_type="job_posting", acquisition_mode="jobspy_experiment")
        self.assertFalse(publishable_observation(item))

    def test_linkedin_guest_jobs_require_exact_verified_company_url(self):
        raw = b'''<div class="base-search-card" data-entity-urn="urn:li:jobPosting:4456746433">
          <a class="base-card__full-link" href="https://no.linkedin.com/jobs/view/example-4456746433?x=1"></a>
          <span class="sr-only">Project manager</span>
          <h4 class="base-search-card__subtitle"><a href="https://no.linkedin.com/company/af-gruppen?trk=x">AF Gruppen</a></h4>
          <span class="job-search-card__location">Oslo</span><time datetime="2026-08-23"></time>
        </div>
        <div class="base-search-card" data-entity-urn="urn:li:jobPosting:4456746434">
          <a class="base-card__full-link" href="https://linkedin.com/jobs/view/other-4456746434"></a>
          <span class="sr-only">Wrong parent job</span>
          <h4 class="base-search-card__subtitle"><a href="https://linkedin.com/company/af-gruppen-sverige">AF Gruppen Sverige</a></h4>
        </div>'''
        jobs, candidates = parse_job_cards(raw, "https://linkedin.com/company/af-gruppen")
        self.assertEqual(candidates, 2)
        self.assertEqual([item["job_id"] for item in jobs], ["4456746433"])
        self.assertEqual(jobs[0]["company_url"], "https://linkedin.com/company/af-gruppen")

    def test_linkedin_company_urls_and_typeahead_are_normalized_without_claiming_ambiguous_ids(self):
        self.assertEqual(
            canonical_company_url("https://no.linkedin.com/company/Norsk-Fiskeeksport/about?trk=x"),
            "https://linkedin.com/company/norsk-fiskeeksport",
        )
        candidates = parse_typeahead(
            json.dumps([
                {"id": "34440", "type": "COMPANY", "displayName": "AF Gruppen"},
                {"id": "1188022", "type": "COMPANY", "displayName": "AF Gruppen Sverige"},
            ]).encode(),
            "AF GRUPPEN ASA",
        )
        self.assertTrue(candidates[0]["exact_legal_name_core"])
        self.assertFalse(candidates[1]["exact_legal_name_core"])
        self.assertEqual(
            parse_detail_company_urls(
                b'<a href="https://no.linkedin.com/company/af-gruppen?trk=job">AF Gruppen</a>'
                b'<a href="https://example.test/company/wrong">Wrong</a>'
            ),
            {"https://linkedin.com/company/af-gruppen"},
        )

    def test_linkedin_guest_profile_uses_structured_company_data_and_ignores_dormant_challenge_code(self):
        graph = {
            "@graph": [
                {
                    "@type": "DiscussionForumPosting",
                    "author": {"url": "https://no.linkedin.com/company/af-gruppen"},
                    "datePublished": "2026-08-21T06:15:05Z",
                    "text": "Exact company update",
                    "url": "https://no.linkedin.com/posts/example-activity-7496449781678927873-x",
                },
                {
                    "@type": "Organization",
                    "name": "AF Gruppen",
                    "url": "https://no.linkedin.com/company/af-gruppen",
                    "description": "Construction group",
                    "numberOfEmployees": {"value": 1303},
                },
            ]
        }
        raw = (
            '<meta name="description" content="AF Gruppen | 56 726 followers on LinkedIn">'
            f'<script type="application/ld+json">{json.dumps(graph)}</script>'
            '<script>const dormant="recaptcha/challengepage";</script>'
            '<div data-test-id="about-us__size"><dd>5,001-10,000 employees</dd></div>'
            '<article class="main-feed-activity-card" data-activity-urn="urn:li:activity:7496449781678927873">'
            '<a data-test-id="social-actions__reactions" data-num-reactions="29"></a>'
            '<a data-test-id="social-actions__comments" data-num-comments="4"></a></article>'
        ).encode()
        profile = extract_linkedin_profile(raw, "https://linkedin.com/company/af-gruppen")
        self.assertEqual(profile["followers"], 56726)
        self.assertEqual(profile["visible_employees"], 1303)
        self.assertEqual(profile["employee_size_label"], "5,001-10,000 employees")
        self.assertEqual(profile["posts"][0]["likes"], 29)
        self.assertEqual(profile["posts"][0]["comments"], 4)

    def test_linkedin_guest_profile_rejects_authwall_without_organization_data(self):
        with self.assertRaisesRegex(RuntimeError, "no structured organization"):
            extract_linkedin_profile(b'<script>recaptcha/challengepage</script>', "https://linkedin.com/company/example")

    def test_linkedin_stale_handle_fallback_is_bounded_to_registry_legal_name(self):
        self.assertEqual(legal_name_profile_url("DIPS AS"), "https://www.linkedin.com/company/dips-as")
        self.assertEqual(legal_name_profile_url("RØD & BLÅ AS"), "https://www.linkedin.com/company/rod-bla-as")

    def test_linkedin_discovery_requires_exact_typeahead_name_and_corroboration(self):
        raw = json.dumps([
            {"id": "1", "type": "COMPANY", "displayName": "DIPS AS"},
            {"id": "2", "type": "COMPANY", "displayName": "DIPS ASA"},
        ]).encode()
        self.assertEqual([item["linkedin_company_id"] for item in parse_linkedin_exact_typeahead(raw, "DIPS AS")], ["1"])
        self.assertEqual(linkedin_normalized_full_name("RØD & BLÅ AS"), "rød blå as")
        company = {
            "name": "DIPS AS",
            "municipality": "BODØ",
            "website": "https://dips.com",
            "evidence": {"website": {"status": "available", "value": {"final_url": "https://dips.com"}}},
        }
        exact = linkedin_discovery_identity(company, {"name": "DIPS AS", "website": "https://www.dips.com", "headquarters": "Bodø"}, {"legal_name_slug"})
        self.assertTrue(exact["exact_entity"])
        weak = linkedin_discovery_identity(company, {"name": "DIPS AS", "website": "https://unrelated.test", "headquarters": "Oslo"}, {"legal_name_slug"})
        self.assertFalse(weak["exact_entity"])

    def test_linkedin_fuzzy_discovery_uses_verified_site_alias_and_reverse_domain(self):
        company = {
            "name": "JARRE AS",
            "municipality": "INDRE ØSTFOLD",
            "website": "https://jarre.co",
            "evidence": {
                "website": {"status": "available", "value": {"final_url": "https://jarre.co", "title": "Jarre&Co"}},
                "roles": {"value": {"roles": [{"name": "Christian Jarre", "role_code": "DAGL"}]}},
            },
        }
        self.assertEqual(linkedin_official_site_aliases(company), ["Jarre&Co"])
        exact = linkedin_discovery_identity(
            company,
            {"name": "Jarre & Co", "website": "https://www.jarre.co", "headquarters": "Askim", "description": ""},
            {"official_site_alias:Jarre&Co"},
        )
        self.assertTrue(exact["exact_entity"])

    def test_linkedin_profile_identity_accepts_redirect_alias_only_with_name_or_reverse_domain_proof(self):
        profile = {
            "name": "ZAPTEC ASA",
            "website": "https://zaptec.com",
            "evidence": {"website": {"source_url": "https://www.zaptec.com/", "value": {"final_url": "https://www.zaptec.com/"}}},
        }
        accepted = assess_linkedin_profile_identity(
            profile,
            "https://linkedin.com/company/gozaptec",
            {"name": "Zaptec", "page_url": "https://linkedin.com/company/zaptec", "website": "https://www.zaptec.com"},
        )
        self.assertTrue(accepted["publishable_candidate"])
        rejected = assess_linkedin_profile_identity(
            profile,
            "https://linkedin.com/company/gozaptec",
            {"name": "Unrelated Parent", "page_url": "https://linkedin.com/company/unrelated", "website": "https://parent.test"},
        )
        self.assertFalse(rejected["publishable_candidate"])

    def test_google_play_observation_is_supported_but_unofficial_output_stays_experimental(self):
        item = self.observation(
            platform="google_play",
            signal_type="review_summary",
            acquisition_mode="unofficial_api_experiment",
            rights_status="review_required",
        )
        reasons = validate_observation(item)
        self.assertNotIn("unsupported platform", reasons)
        self.assertFalse(publishable_observation(item))

    def test_company_directory_is_not_a_website_discovery_candidate(self):
        profile = {"name": "OBLOMOV AS", "organisation_number": "991167315", "municipality": "SOLA"}
        result = {"url": "https://www.northdata.com/Oblomov-AS/BR-991167315", "title": "Oblomov AS", "snippet": "991167315", "rank": 1}
        assessment = score_search_candidate(profile, result)
        self.assertFalse(assessment["publishable_candidate"])
        self.assertEqual(assessment["status"], "rejected")

    def test_unknown_company_directory_with_org_number_is_not_a_candidate(self):
        profile = {"name": "AKSLA AS", "organisation_number": "923304290", "municipality": "ÅLESUND"}
        result = {"url": "https://vexter.no/selskap/aksla-as/923304290", "title": "AKSLA AS", "snippet": "923304290", "rank": 1}
        self.assertFalse(score_search_candidate(profile, result)["publishable_candidate"])

    def test_annual_workforce_parser_does_not_treat_norwegian_o_as_zero(self):
        heading = "Note 2 - Lonnskostnader, antall ansatte og lan til ansatte"
        self.assertEqual(extract_candidate(heading), (None, None, "no_employee_phrase", None))
        count, span, status, measure = extract_candidate("Det er to ansatte i sameiet.")
        self.assertEqual((count, status, measure), (2, "accepted", "employees"))
        self.assertEqual(span, "Det er to ansatte i sameiet.")
        self.assertEqual(extract_candidate("Selskapet har 1 2025 sysselsatt 2 arsverk.")[0], 2)
        self.assertEqual(extract_candidate("Antall arsverk syssetsatt i regnskapsaret: 3")[0], 3)
        self.assertEqual(extract_candidate("Stiftelsen har ingen ansatte og ingen arsverk.")[0], 0)
        self.assertEqual(extract_candidate("Selskapet hadde ingen ansatte i 2025.")[0], 0)
        self.assertEqual(extract_candidate("Gjennomsnittlig antall ansatte i regnskapsaret: 0")[0], 0)
        self.assertEqual(extract_candidate("Note Antall Aarsverk i regnskapsaret 0.00")[0], 0)
        self.assertEqual(extract_candidate("Tal pa Aarsverk i rekneskapsaret 1.50")[0], 1.5)
        self.assertTrue(needs_ocr("Digital cover text without the employee note"))
        self.assertFalse(needs_ocr("Selskapet har 2 ansatte. " + "Digital report text. " * 8))

    def test_aggregate_keeps_source_metrics_separate_and_abstains_on_thin_sentiment(self):
        items = [
            self.observation(id="a", sentiment_label="positive", sentiment_model_version="m1"),
            self.observation(id="b", platform="youtube", signal_type="profile_metrics", source_url="https://youtube.com/@example", evidence_span=None),
        ]
        result = aggregate_footprint(items, as_of="2026-08-22T00:00:00Z")
        self.assertEqual(result["accepted_observations"], 2)
        self.assertEqual(result["sentiment"]["status"], "abstain")
        self.assertNotIn("popularity_score", result)

    def test_customer_review_sentiment_accepts_ten_independent_reviewers_on_one_platform(self):
        items = [
            self.observation(
                id=f"review-{index}",
                sentiment_label="positive",
                sentiment_model_version="explicit_star_rating_v1",
                reviewer_id=f"reviewer-{index}",
            )
            for index in range(10)
        ]
        result = aggregate_footprint(items, as_of="2026-08-22T00:00:00Z")
        self.assertEqual(result["sentiment"]["status"], "available")
        self.assertEqual(result["sentiment"]["independent_reviewers"], 10)

    def test_google_maps_identity_gate_rejects_neighbor_and_accepts_exact_address(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {
                "registry": {"value": {
                    "forretningsadresse.adresse": "Standardveien 1",
                    "forretningsadresse.postnummer": "0581",
                    "telefon": "22 89 11 00",
                }},
                "website": {"value": {
                    "final_url": "https://afgruppen.no/",
                    "identity_assessment": {"publishable": True},
                }},
            },
        }
        exact = candidate_score(profile, {
            "title": "AF Gruppen", "address": "Standardveien 1, 0581 Oslo, Norge",
            "phone": "+47 22 89 11 00", "web_site": "https://afgruppen.no/", "review_count": 21,
        })
        neighbor = candidate_score(profile, {
            "title": "AF Eiendom", "address": "Standardveien 1, 0581 Oslo, Norge",
            "phone": "+47 22 89 11 00", "web_site": "https://afgruppen.no/eiendom/", "review_count": 0,
        })
        self.assertTrue(exact["accepted"])
        self.assertFalse(neighbor["accepted"])

    def test_google_maps_exact_name_and_postcode_city_can_resolve_operating_address(self):
        profile = {
            "organisation_number": "999999999",
            "name": "EXAMPLE INDUSTRI AS",
            "evidence": {"registry": {"value": {
                "forretningsadresse.adresse": "c/o Accountant Other Street 1",
                "forretningsadresse.postnummer": "4021",
                "forretningsadresse.poststed": "STAVANGER",
            }}},
        }
        result = candidate_score(profile, {
            "title": "Example Industri AS", "address": "Factory Road 7, 4021 Stavanger, Norway",
            "phone": "", "web_site": "", "review_count": 4,
        })
        self.assertTrue(result["accepted"])
        self.assertTrue(result["postcode_city_match"])

    def test_google_maps_trade_name_requires_exact_address_phone_and_no_partial_name_collision(self):
        profile = {
            "name": "OSLOFJORDEN EIENDOMSMEGLING AS",
            "evidence": {"registry": {"value": {
                "forretningsadresse.adresse": "Stranden 81", "forretningsadresse.postnummer": "0250",
                "forretningsadresse.poststed": "Oslo", "telefon": "22620000",
            }}},
        }
        candidate = {"title": "PrivatMegleren Premium", "address": "Stranden 81, 0250 Oslo", "phone": "+47 22 62 00 00"}
        result = candidate_score(profile, candidate)
        self.assertFalse(result["trade_name_match"])
        self.assertFalse(result["accepted"])
        profile["organisation_number"] = "932083108"
        result = candidate_score(profile, candidate)
        self.assertTrue(result["trade_name_match"])
        self.assertTrue(result["accepted"])
        candidate["phone"] = "+47 99 99 99 99"
        self.assertFalse(candidate_score(profile, candidate)["accepted"])

    def test_experimental_maps_signals_raise_only_experimental_places_score(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        common = {
            "organisation_number": "938702675",
            "platform": "google_places",
            "source_url": "https://www.google.com/maps/place/example",
            "retrieved_at": "2026-08-22T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "registry_address_match", "value": True}],
            "acquisition_mode": "unofficial_api_experiment",
            "rights_status": "review_required",
            "source_class": "public_business_listing",
            "evidence_span": "AF Gruppen; Standardveien 1; rating=2.5; reviews=21",
        }
        observations = [
            {**common, "id": "place", "signal_type": "place_summary", "strategy": "places_identity_resolution"},
            {**common, "id": "summary", "signal_type": "review_summary", "strategy": "places_rating_reviews"},
        ]
        score = development_score(profile, observations)
        self.assertEqual(score["score"], 0.0)
        self.assertEqual(score["experimental_potential_score"], 35.0)

    def test_aggregate_maps_rating_is_experimental_sentiment_and_buzz(self):
        profile = {
            "organisation_number": "938702675",
            "name": "AF GRUPPEN ASA",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        common = {
            "organisation_number": "938702675",
            "platform": "google_places",
            "source_url": "https://www.google.com/maps/place/example",
            "retrieved_at": "2026-08-22T00:00:00Z",
            "content_sha256": "a" * 64,
            "exact_entity": True,
            "identity_proof": [{"type": "registry_address_match", "value": True}],
            "acquisition_mode": "unofficial_api_experiment",
            "rights_status": "review_required",
            "evidence_span": "AF Gruppen; rating=4.4; reviews=21",
            "metrics": {"rating": 4.4, "rating_scale": 5, "review_count": 21},
        }
        observations = [
            {**common, "id": "summary", "signal_type": "review_summary", "strategy": "places_rating_reviews"},
            {**common, "id": "buzz", "signal_type": "buzz_metrics", "strategy": "buzz_peer_normalization"},
        ]
        score = development_score(profile, observations)
        self.assertEqual(score["score"], 0.0)
        self.assertEqual(score["experimental_sentiment_status"], "available")
        self.assertEqual(score["experimental_potential_score"], 35.0)

    def test_task_planner_uses_verified_handles_and_adds_core_connectors(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {
                "website": {"value": {"social_links": [{"platform": "youtube", "url": "https://youtube.com/@example"}]}},
            },
        }
        tasks = plan_external_tasks(profile)
        connectors = {item["connector"] for item in tasks}
        self.assertIn("google_places_api", connectors)
        self.assertIn("jobs_provider", connectors)
        self.assertIn("youtube_connector", connectors)
        self.assertNotIn("permitted_search_api", connectors)

    def test_controller_recomputes_sentiment_and_records_marginal_gain(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}},
        }
        handle = self.observation(signal_type="profile_handle", source_class="company_social", evidence_span=None, strategy="verified_handle_extraction")
        score = development_score(profile, [handle])
        self.assertGreater(score["score"], 0)
        self.assertEqual(score["sentiment_status"], "abstain")
        result = run_company_control(profile, [handle], minimum_iterations=10, maximum_iterations=15)
        self.assertGreaterEqual(result["iterations_run"], 10)
        self.assertTrue(any(item["score_delta"] > 0 for item in result["iterations"]))
        self.assertTrue(all("sentiment_status" in item for item in result["iterations"]))

    def test_controller_final_score_is_not_path_dependent_after_target_is_reached(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}},
        }
        observations = [
            self.observation(signal_type="profile_handle", source_class="company_social", evidence_span=None, strategy="verified_handle_extraction"),
            self.observation(id="metric", platform="youtube", signal_type="profile_metrics", source_url="https://youtube.com/@example", evidence_span=None, strategy="social_profile_metrics"),
        ]
        result = run_company_control(profile, observations, target=20, minimum_iterations=1)
        self.assertEqual(result["iterations_run"], len(strategy_order([])))
        self.assertEqual(result["final"], development_score(profile, observations))

    def test_exact_wikidata_org_profile_can_supply_external_identity(self):
        profile = {
            "organisation_number": "923609016",
            "name": "Example AS",
            "evidence": {"website": {"value": {"identity_assessment": {"publishable": False}}}},
        }
        wikidata = self.observation(
            platform="wikidata",
            signal_type="company_profile",
            source_url="https://www.wikidata.org/wiki/Q123",
            evidence_span="Q123: P2333=923609016",
            source_class="open_knowledge_graph",
            strategy="company_site_identity",
        )
        score = development_score(profile, [wikidata])
        self.assertEqual(score["components"]["exact_external_identity"], 20.0)
        self.assertTrue(publishable_observation(wikidata))

    def test_controller_replicates_prior_winning_strategy_first(self):
        prior = [
            {"strategy": "youtube_channel_feed", "learning_gain": 8.0},
            {"strategy": "verified_handle_extraction", "learning_gain": 2.0},
        ]
        self.assertEqual(strategy_order(prior)[0], "youtube_channel_feed")
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"website": {"value": {"identity_assessment": {"publishable": True}}}}}
        result = run_company_control(profile, [], prior_iterations=prior, minimum_iterations=1, maximum_iterations=2)
        self.assertEqual(result["iterations"][0]["strategy"], "youtube_channel_feed")
        self.assertEqual(result["iterations"][0]["controller_action"], "replicate")


class CompletenessScoreTests(unittest.TestCase):
    def test_all_source_weights_sum_to_one_hundred(self):
        from scripts.score_company_completeness import ENRICHMENT_WEIGHTS, FOUNDATION_WEIGHTS

        self.assertEqual(sum(FOUNDATION_WEIGHTS.values()) + sum(ENRICHMENT_WEIGHTS.values()), 100.0)

    def test_all_source_score_combines_foundation_and_external_without_imputing_missing(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry_live": {"status": "available", "value": {"organisation_number": "923609016"}},
                "financials": {"status": "available"},
                "roles": {"status": "available"},
                "locations": {"status": "available"},
                "website": {"status": "not_found"},
            },
        }
        components = {
            "exact_external_identity": 20,
            "verified_handles": 15,
            "profile_metrics": 10,
            "places_identity": 5,
            "places_reviews": 10,
            "workforce_jobs": 10,
            "public_buzz": 10,
            "independent_sentiment": 0,
            "freshness_evidence": 5,
        }
        result = {"organisation_number": "923609016", "company_name": "Example AS", "final": {"components": components, "experimental_components": components}}
        scored = score_rows([profile], [result])
        self.assertEqual(scored[0]["foundation_score"], 30.0)
        self.assertEqual(scored[0]["strict_enrichment"]["independent_sentiment"], 0.0)
        self.assertEqual(scored[0]["strict_completeness_score"], 88.0)
        self.assertEqual(summarize(scored)["companies"], 1)

    def test_site_activity_requires_exact_identity_and_preserves_snapshot_provenance(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {"website": {
                "status": "available",
                "source_url": "https://example.test/",
                "retrieved_at": "2026-08-23T00:00:00Z",
                "value": {
                    "final_url": "https://example.test/",
                    "content_sha256": "a" * 64,
                    "identity_assessment": {"publishable": True, "status": "exact", "score": 1.0},
                    "pages": [{"url": "https://example.test/"}],
                },
            }},
        }
        item = site_activity_observation(profile)
        self.assertIsNotNone(item)
        self.assertEqual(item["strategy"], "company_site_activity")
        self.assertTrue(publishable_observation(item))
        profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"] = False
        self.assertIsNone(site_activity_observation(profile))

    def test_news_title_gate_requires_the_full_legal_name_core(self):
        self.assertTrue(exact_title_match("NORDIC DOOR AS", "Nordic Door AS åpner ny fabrikk - Lokalavisa"))
        self.assertFalse(exact_title_match("NORDIC DOOR AS", "Nordic investors prefer another door - Example"))
        self.assertTrue(exact_title_match("SOLVANG ASA", "Sterkt årsresultat fra Solvang ASA i 2024 - Skipsrevyen"))
        self.assertFalse(exact_title_match("VIND HOLDING AS", "Inntektene til Aneo Roan Vind Holding AS stupte - mn24.no"))
        self.assertFalse(exact_title_match("CONSTO AS", "Drastisk fall hos Consto Bergen AS - BT"))

    def test_site_news_requires_exact_identity_and_a_captured_news_path(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {"website": {
                "status": "available", "retrieved_at": "2026-08-23T00:00:00Z",
                "value": {
                    "identity_assessment": {"publishable": True, "score": 1.0},
                    "pages": [{"url": "https://example.test/aktuelt/new-contract", "title": "New contract", "content_sha256": "a" * 64,
                               "article": {"title": "New contract", "published_at": "2026-08-22", "date_extraction_method": "article_jsonld_datePublished_v1"}}],
                },
            }},
        }
        item = site_news_observation(profile)
        self.assertEqual(item["signal_type"], "public_post")
        self.assertTrue(publishable_observation(item))
        self.assertEqual(item["published_at"], "2026-08-22")
        from scripts.extract_company_site_news import observations
        import copy
        valid = copy.deepcopy(profile["evidence"]["website"]["value"]["pages"][0])
        page = profile["evidence"]["website"]["value"]["pages"][0]
        page["article"]["published_at"] = "2099-01-01"
        self.assertIsNone(site_news_observation(profile))
        profile["evidence"]["website"]["value"]["pages"].append(valid)
        self.assertEqual(len(observations(profile)), 1)
        profile["evidence"]["website"]["value"]["pages"].pop()
        page["article"] = None
        self.assertIsNone(site_news_observation(profile))
        profile["evidence"]["website"]["value"]["pages"][0]["url"] = "https://example.test/contact"
        self.assertIsNone(site_news_observation(profile))

    def test_verified_observations_require_known_org_and_snapshot_hash(self):
        profiles = [{"organisation_number": "923609016", "name": "Example AS"}]
        seed = {"organisation_number": "923609016", "platform": "news", "signal_type": "public_mention", "source_url": "https://example.test/news", "content_sha256": "a" * 64, "evidence_span": "Example AS", "proof": "Exact legal name"}
        self.assertTrue(build_verified_observations([seed], profiles)[0]["exact_entity"])
        seed["content_sha256"] = "bad"
        with self.assertRaises(ValueError):
            build_verified_observations([seed], profiles)

    def test_directory_identity_is_experimental_and_never_becomes_strict(self):
        item = {
            "id": "directory-1", "organisation_number": "923609016", "platform": "company_directory",
            "signal_type": "company_profile", "source_url": "https://example.test/923609016",
            "retrieved_at": "2026-08-23T00:00:00Z", "content_sha256": "a" * 64,
            "exact_entity": True, "identity_proof": [{"type": "organisation_number", "value": "923609016"}],
            "acquisition_mode": "rights_review_experiment", "rights_status": "review_required",
            "source_class": "public_company_directory", "strategy": "company_directory_identity",
        }
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {}}
        score = development_score(profile, [item])
        self.assertEqual(score["components"]["exact_external_identity"], 0.0)
        self.assertEqual(score["experimental_components"]["exact_external_identity"], 20.0)
        self.assertFalse(publishable_observation(item))

    def test_fagfolk_rating_parser_uses_jsonld_and_slug_is_stable(self):
        raw = b'<script type="application/ld+json">{"aggregateRating":{"ratingValue":4.4,"ratingCount":25}}</script>'
        self.assertEqual(extract_aggregate_rating(raw)[:2], (4.4, 25))
        self.assertEqual(slug("NORDIC DØR AS"), "nordic-dor-as")


class SamplingTests(unittest.TestCase):
    def test_frozen_universe_selector_excludes_prior_batch_and_rejects_shortfall(self):
        from select_entry_batch import main as select_batch

        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.jsonl.gz"
            excluded = Path(directory) / "prior.jsonl"
            output = Path(directory) / "selected.jsonl"
            rows = [{"organisation_number": str(200000000 + index)} for index in range(6)]
            with gzip.open(universe, "wt", encoding="utf-8") as handle:
                handle.write("\n".join(json.dumps(row) for row in rows))
            excluded.write_text(json.dumps(rows[0]) + "\n" + json.dumps(rows[1]), encoding="utf-8")
            command = ["select_entry_batch.py", "--universe", str(universe), "--exclude", str(excluded), "--output", str(output), "--seed", "7", "--count", "3"]
            with patch.object(sys, "argv", command):
                select_batch()
                first = output.read_bytes()
                select_batch()
            self.assertEqual(first, output.read_bytes())
            selected = {json.loads(line)["organisation_number"] for line in first.splitlines()}
            self.assertEqual(len(selected), 3)
            self.assertTrue(selected <= {row["organisation_number"] for row in rows[2:]})
            with patch.object(sys, "argv", command[:-1] + ["5"]):
                with self.assertRaisesRegex(SystemExit, "after exclusions"):
                    select_batch()
            self.assertEqual(first, output.read_bytes())

    def test_financial_filer_sample_requires_current_active_rows_and_preserves_overlap(self):
        fields = ["organisasjonsnummer", "navn", "organisasjonsform.kode", "sisteInnsendteAarsregnskap", "konkurs", "underAvvikling"]
        rows = [
            {"organisasjonsnummer": str(200000000 + index), "navn": f"Company {index}", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "false", "underAvvikling": "false"}
            for index in range(12)
        ] + [
            {"organisasjonsnummer": "300000001", "navn": "Stale AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2024", "konkurs": "false", "underAvvikling": "false"},
            {"organisasjonsnummer": "300000002", "navn": "Bankrupt AS", "organisasjonsform.kode": "AS", "sisteInnsendteAarsregnskap": "2025", "konkurs": "true", "underAvvikling": "false"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
            repeated, _ = deterministic_financial_filer_sample(path, 5, latest_year="2025", preserved_organisation_numbers={"200000003"}, seed=7)
        self.assertEqual([row["organisation_number"] for row in selected], [row["organisation_number"] for row in repeated])
        self.assertIn("200000003", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000001", {row["organisation_number"] for row in selected})
        self.assertNotIn("300000002", {row["organisation_number"] for row in selected})
        self.assertEqual(metadata["eligible_rows"], 12)
        self.assertEqual(metadata["preserved_eligible_selected"], 1)
        self.assertTrue(all(financial_filer_eligible(row, "2025") for row in selected))

    def test_extension_sample_is_deterministic_and_excludes_initial(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": str(100000000 + index), "navn": f"Company {index}", "hjemmeside": "", "organisasjonsform.kode": "AS"}
            for index in range(20)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            first, metadata = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
            second, _ = deterministic_extension_sample(path, 5, {"100000000", "100000001"}, seed=3)
        self.assertEqual([item["organisation_number"] for item in first], [item["organisation_number"] for item in second])
        self.assertEqual(len(first), 5)
        self.assertEqual(metadata["overlap_with_excluded"], 0)

    def test_strata_distinguish_adverse_and_web_coverage(self):
        base = {"legal_form": "AS", "employees": 12, "bankrupt": False, "liquidating": False, "website": "example.no"}
        self.assertEqual(stratum(base), "AS|5-19|active|web")
        self.assertEqual(stratum({**base, "bankrupt": True, "website": ""}), "AS|5-19|adverse|no-web")

    def test_normalize_does_not_invent_employee_count(self):
        row = normalize_row({"organisasjonsnummer": "923609016", "navn": "Example AS", "antallAnsatte": ""})
        self.assertIsNone(row["employees"])
        self.assertEqual(row["latest_submitted_accounts"], "")

    def test_fresh_website_audit_sample_excludes_poc_and_deduplicates_hosts(self):
        fields = ["organisasjonsnummer", "navn", "hjemmeside", "organisasjonsform.kode"]
        rows = [
            {"organisasjonsnummer": "111111111", "navn": "Excluded AS", "hjemmeside": "excluded.no", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "222222222", "navn": "A AS", "hjemmeside": "https://www.shared.no/a", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "333333333", "navn": "B AS", "hjemmeside": "shared.no/b", "organisasjonsform.kode": "AS"},
            {"organisasjonsnummer": "444444444", "navn": "C AS", "hjemmeside": "unique.no", "organisasjonsform.kode": "AS"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            selected, metadata = deterministic_website_audit_sample(path, 1, {"111111111"}, {"shared.no"}, seed=9)
        self.assertEqual(len(selected), 1)
        self.assertNotIn("111111111", {row["organisation_number"] for row in selected})
        self.assertEqual(selected[0]["organisation_number"], "444444444")
        self.assertEqual(metadata["unique_hosts_selected"], 1)
        self.assertEqual(metadata["excluded_website_hosts"], 1)


class OperationsTests(unittest.TestCase):
    def test_history_rate_limiter_spaces_request_starts_not_responses(self):
        import norway_company_agent.official as official

        old = official._history_last_request
        now = [10.0]
        sleeps = []

        def clock():
            return now[0]

        def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        try:
            official._history_last_request = 9.0
            _reserve_history_slot(clock, sleeper)
            self.assertAlmostEqual(sleeps[0], 1.1)
            self.assertAlmostEqual(official._history_last_request, 11.1)
            now[0] = 13.3
            _reserve_history_slot(clock, sleeper)
            self.assertEqual(len(sleeps), 1)
            self.assertAlmostEqual(official._history_last_request, 13.3)
        finally:
            official._history_last_request = old

    def test_batch_input_preserves_split_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orgs.jsonl"
            path.write_text(json.dumps({"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress", "ignored": "x"}) + "\n", encoding="utf-8")
            self.assertEqual(read_organisation_inputs(path), [{"organisation_number": "923609016", "evaluation_split": "held_out", "sample_slice": "stress"}])

    def test_batch_contract_emits_exact_terminal_envelopes(self):
        profile = {
            "organisation_number": "923609016",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://example.test", value={"organisasjonsnummer": "923609016"}, content_sha256="a" * 64),
                "website": evidence("website", "blocked", "company_site", "https://example.test", note="robots.txt denied"),
            },
        }
        envelope = terminal_envelope(profile, run_id="day-1", modules=["registry", "website"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:01:00Z")
        self.assertEqual(envelope["modules"]["registry"]["state"], "available")
        self.assertEqual(envelope["modules"]["website"]["state"], "blocked")
        self.assertEqual(envelope["run"]["terminal_status"], "completed")
        self.assertIn("claims", envelope)
        self.assertIn("evidence", envelope)
        self.assertIn("sections", envelope)
        self.assertIn("synthesis", envelope["sections"])
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])
        self.assertFalse(validate_envelopes([envelope], 2)["passed"])

        bad_envelope = dict(envelope, state="submission_error")
        self.assertFalse(validate_envelopes([bad_envelope], 1)["passed"])

    def test_unknown_evidence_state_is_submission_error(self):
        self.assertEqual(evidence_terminal_state({"status": "not_fetched"}), "failed")
        self.assertEqual(evidence_terminal_state({"status": "not_available"}), "not_available")
        self.assertEqual(evidence_terminal_state({"status": "error"}), "failed")

    def test_envelope_activity_section_excludes_unapproved_and_foreign_observations(self):
        import copy

        post = {"id": "post-1", "organisation_number": "923609016", "platform": "company_site", "signal_type": "public_post",
                "source_class": "company_site", "source_url": "https://example.no/news", "retrieved_at": "2026-10-08T00:00:00Z",
                "published_at": "2023-10-26", "content_sha256": "a" * 64, "exact_entity": True, "identity_proof": [{"type": "website_identity"}],
                "evidence_span": "An older publication", "acquisition_mode": "permitted_public_page", "rights_status": "approved",
                "metrics": {"title": "An older publication"}}
        profile = {"organisation_number": "923609016", "evidence": {
            "registry": evidence("registry", "available", "official", "https://example.no/registry", value={"organisasjonsnummer": "923609016"}, content_sha256="b" * 64)},
            "observations": [post, {**post, "id": "unapproved", "rights_status": "review_required"},
                             {**post, "id": "foreign", "organisation_number": "999999999"}]}
        result = terminal_envelope(profile, run_id="test", modules=["registry"], started_at="2026-10-08T00:00:00Z", completed_at="2026-10-08T00:01:00Z")
        self.assertEqual([item["id"] for item in result["sections"]["hiring_and_dated_public_activity"]["observations"]], ["post-1"])
        self.assertTrue(validate_envelopes([result], 1)["passed"])

    def test_batch_resume_only_skips_profiles_with_all_terminal_modules(self):
        complete = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_found"}}}
        partial = {"evidence": {"registry": {"status": "available"}, "website": {"status": "not_fetched"}}}
        self.assertTrue(profile_complete_for_modules(complete, ["registry", "website"]))
        self.assertFalse(profile_complete_for_modules(partial, ["registry", "website"]))

    def test_nearest_rank_percentiles_are_deterministic(self):
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.5), 3)
        self.assertEqual(percentile([1, 2, 3, 4, 100], 0.95), 100)
        self.assertEqual(latency_summary([1, 2, 3]), {"n": 3, "p50_ms": 2.0, "p95_ms": 3.0, "max_ms": 3.0})

    def test_domain_fairness_summary_preserves_tail(self):
        result = domain_request_summary(__import__("collections").Counter({"a.no": 1, "b.no": 2, "c.no": 9}))
        self.assertEqual(result["domains"], 3)
        self.assertEqual(result["p50_requests"], 2.0)
        self.assertEqual(result["max_requests"], 9)


class WebsiteTests(unittest.TestCase):
    def test_numbered_footer_preserves_legal_operator_for_identity(self):
        from norway_company_agent.website import _identity_text

        soup = BeautifulSoup('<html><head><title>Example</title></head><body><div id="footer3"><p>© 2026 Example AS · Org nr 999999999</p></div><div id="footer-product"><p>Unrelated product text</p></div></body></html>', "lxml")
        captured = _identity_text(soup)
        self.assertIn("Example AS", captured)
        self.assertIn("999999999", captured)
        self.assertNotIn("Unrelated product text", captured)
        profile = {"organisation_number": "999999999", "name": "Example AS", "evidence": {"website": {"status": "available", "source_url": "https://example.no/", "value": {"title": "Example", "final_url": "https://example.no/", "identity_text_excerpt": captured}}}}
        self.assertTrue(assess_website_identity(profile)["publishable"])
        profile["organisation_number"] = "888888888"
        self.assertFalse(assess_website_identity(profile)["publishable"])

    def test_xml_declared_html_retains_identity_and_structured_data(self):
        import hashlib
        from norway_company_agent import website

        raw = b'<?xml version="1.0" encoding="UTF-8"?><html><head><title>Demo AS</title><script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Demo AS"}</script></head><body><footer>Demo AS Org nr 999999999</footer></body></html>'

        class Response:
            headers = {"content-type": "text/html; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return raw[:limit]

            def geturl(self):
                return "https://demo.no/"

        with patch.object(website, "assert_public_url"), patch.object(website, "_robots_allowed", return_value=True), patch.object(website.SAFE_OPENER, "open", return_value=Response()):
            record, _ = website.fetch_website("https://demo.no/")
            page, _, _, _, _, error = website._fetch_secondary_page("https://demo.no/", homepage_domain="demo.no", timeout=1, max_bytes=2000000)

        self.assertEqual(record["status"], "available")
        self.assertEqual(record["value"]["structured_organisations"][0]["name"], "Demo AS")
        self.assertEqual(record["content_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIsNone(error)
        self.assertEqual(page["title"], "Demo AS")
        self.assertIn("999999999", page["identity_text_excerpt"])
        self.assertEqual(page["content_sha256"], hashlib.sha256(raw).hexdigest())

    def test_interrupted_run_does_not_synthesize_terminal_failures(self):
        profiles = [{"organisation_number": "1", "website": "pending.no"}]
        self.assertEqual(terminal_events_for_run(profiles, [], False), [])
        self.assertEqual(len(terminal_events_for_run(profiles, [], True)), 1)

    def test_missing_seed_gets_explicit_terminal_event(self):
        profiles = [
            {"organisation_number": "1", "website": "example.no"},
            {"organisation_number": "2", "website": "blocked.no"},
            {"organisation_number": "3", "website": ""},
        ]
        existing = [{"organisation_number": "1", "status": "available"}]
        missing = missing_seed_error_events(profiles, existing)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["organisation_number"], "2")
        self.assertEqual(missing[0]["status"], "source_error")
        self.assertIn("robots.txt", missing[0]["error"])

    def test_crawl_event_extraction_and_merge_preserve_page_hashes(self):
        homepage = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/",
            final_url="https://example.no/",
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=b'<html><head><title>Example AS</title><meta name="description" content="Company"></head><body><p>Example AS provides enough substantive company information for extraction and identity review.</p><a href="https://linkedin.com/company/example">LinkedIn</a></body></html>',
            page_kind="homepage",
            retrieved_at="2026-08-22T00:00:00Z",
        )
        secondary = extract_page_event(
            organisation_number="923609016",
            requested_url="https://example.no/contact",
            final_url="https://example.no/contact",
            status_code=200,
            content_type="text/html",
            body=b"<html><title>Contact</title><body>Contact Example AS in Oslo.</body></html>",
            page_kind="priority",
            retrieved_at="2026-08-22T00:00:01Z",
        )
        record = merge_profile_events({"website": "https://example.no"}, [homepage, secondary])
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(record["value"]["pages"]), 2)
        self.assertEqual(record["content_sha256"], homepage["content_sha256"])
        self.assertEqual(record["value"]["scheduler"], "scrapy_resumable_v1")

    def test_footer_identity_is_preserved_for_exact_company_gate(self):
        event = extract_page_event(
            organisation_number="985628572",
            requested_url="https://netsolution.no/",
            final_url="https://netsolution.no/",
            status_code=200,
            content_type="text/html",
            body=(
                b'<html><head><title>IT services</title></head><body><main>Useful services for customers.</main>'
                b'<footer>Netsolution Viken AS, Kobbervikdalen 75 A, 3036 Drammen</footer></body></html>'
            ),
            page_kind="homepage",
            retrieved_at="2026-08-23T00:00:00Z",
        )
        website = merge_profile_events({"website": "https://netsolution.no/"}, [event])
        profile = {
            "organisation_number": "985628572",
            "name": "NETSOLUTION VIKEN AS",
            "evidence": {"website": website},
        }
        self.assertIn("Netsolution Viken AS", website["value"]["identity_text_excerpt"])
        self.assertTrue(assess_website_identity(profile)["publishable"])

    def test_normalizes_registry_hostname(self):
        self.assertEqual(normalize_homepage("example.no"), "https://example.no/")
        self.assertEqual(normalize_homepage("http://example.no"), "http://example.no/")

    def test_international_registry_url_produces_ascii_http_request(self):
        import http.client
        import urllib.request
        import urllib.parse
        url = normalize_homepage("https://WWW.Føniksadvokater.no:8443/blåbær/%C3%B8")
        self.assertEqual(url, "https://www.xn--fniksadvokater-qqb.no:8443/bl%C3%A5b%C3%A6r/%C3%B8")
        self.assertEqual(normalize_homepage(url), url)
        opener = urllib.request.build_opener()
        handler = urllib.request.HTTPHandler()
        handler.add_parent(opener)
        request = handler.do_request_(urllib.request.Request(url))
        request.get_header("Host").encode("ascii")
        connection = http.client.HTTPConnection(request.host)
        connection.putrequest("GET", request.selector)
        self.assertIn(b"GET /bl%C3%A5b%C3%A6r/%C3%B8 HTTP/1.1", connection._buffer)
        self.assertEqual(normalize_homepage("http://[2606:4700:4700::1111]:8080/"), "http://[2606:4700:4700::1111]:8080/")
        self.assertIsNone(normalize_homepage("https://example.no:invalid/"))

    def test_single_bundle_empty_shell_is_a_browser_candidate(self):
        empty_shell = BeautifulSoup('<html><head><title>Company</title></head><body><div id="root"></div><script src="/assets/index.js"></script></body></html>', "html.parser")
        self.assertEqual(_extraction_state("", empty_shell), "js_fallback_candidate")
        static_page = BeautifulSoup('<html><body>' + 'Visible company information. ' * 10 + '<script src="/analytics.js"></script></body></html>', "html.parser")
        self.assertEqual(_extraction_state("", static_page), "static_complete")
        self.assertEqual(_extraction_state("", BeautifulSoup('<html><body>Coming soon</body></html>', "html.parser")), "static_complete")

    def test_rendered_snapshot_keeps_dom_and_http_document_provenance_distinct(self):
        import copy
        import hashlib
        from norway_company_agent.website import _rendered_website_record

        document = b'<html><body><div id="root"></div></body></html>'
        markup = b'<html><head><title>Example</title></head><body><footer>Example AS | Org nr 923609016</footer></body></html>'
        record = _rendered_website_record("https://example.no/en", markup, document, "https://example.no/")
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {}}
        record = apply_website_identity_gate(profile, record)["website"]
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["source_url"], "https://example.no/en")
        self.assertEqual(record["content_sha256"], hashlib.sha256(markup).hexdigest())
        self.assertEqual(record["value"]["http_document"]["content_sha256"], hashlib.sha256(document).hexdigest())
        self.assertNotEqual(record["content_sha256"], record["value"]["http_document"]["content_sha256"])
        profile["evidence"]["website"] = record
        envelope = terminal_envelope(profile, run_id="rendered", modules=["website"], started_at=record["retrieved_at"], completed_at=record["retrieved_at"])
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])
        self.assertTrue(all(item["capture_kind"] == "rendered_dom" for item in envelope["evidence"]))
        forged = copy.deepcopy(envelope)
        forged["profile"]["evidence"]["website"]["value"]["captured_html"] += "tampered"
        self.assertFalse(validate_envelopes([forged], 1)["passed"])
        changed = markup.replace(b"923609016", b"999999999")
        wrong = _rendered_website_record("https://example.no/en", changed, document, "https://example.no/")
        self.assertEqual(apply_website_identity_gate(profile, wrong)["website"]["status"], "ambiguous")

    def test_rendered_legal_pages_prove_seller_within_remaining_budget(self):
        import time
        from norway_company_agent.website import _fetch_rendered_identity_pages, _rendered_website_record

        markup = b'<html><head><title>Example</title></head><body><a href="/policies/terms-of-sale">Terms</a><a href="/contact">Contact</a></body></html>'
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {}}
        record = _rendered_website_record("https://example.no/", markup, b'<html></html>', "https://example.no/")
        page = {"url": "https://example.no/policies/terms-of-sale", "title": "Terms", "content_sha256": "b" * 64,
                "main_text_excerpt": "Selger er Example AS, org.nr. 923 609 016, og betegnes som selger.", "retrieved_at": record["retrieved_at"]}
        metrics = {"requests": 38, "bytes": 100, "latencies_ms": []}
        with patch("norway_company_agent.website._fetch_secondary_page", return_value=(page, [], 2, 150, 1, None)) as fetch:
            _fetch_rendered_identity_pages(record, metrics, deadline=time.monotonic() + 5, max_requests=40)
        fetch.assert_called_once()
        self.assertEqual(metrics["requests"], 40)
        self.assertEqual(metrics["bytes"], 250)
        self.assertEqual(record["value"]["pages"][1]["capture_kind"], "http_response_body")
        gated = apply_website_identity_gate(profile, record)["website"]
        self.assertEqual(gated["status"], "available")
        self.assertEqual(gated["value"]["identity_assessment"]["identity_evidence"][0]["content_sha256"], "b" * 64)
        profile["evidence"]["website"] = gated
        envelope = terminal_envelope(profile, run_id="rendered-seller", modules=["website"], started_at=record["retrieved_at"], completed_at=record["retrieved_at"])
        self.assertTrue(validate_envelopes([envelope], 1)["passed"])
        with patch("norway_company_agent.website._fetch_secondary_page") as fetch:
            _fetch_rendered_identity_pages(record, metrics, deadline=time.monotonic() + 5, max_requests=40)
            _fetch_rendered_identity_pages(record, {"requests": 0, "bytes": 0}, deadline=time.monotonic() - 1, max_requests=40)
            fetch.assert_not_called()

    def test_rendered_legal_page_failures_remain_explicit_and_unverified(self):
        import time
        from norway_company_agent.website import _fetch_rendered_identity_pages, _rendered_website_record
        record = _rendered_website_record("https://example.no/", b'<html><body><a href="/terms">Terms</a></body></html>', b'<html></html>', "https://example.no/")
        with patch("norway_company_agent.website._fetch_secondary_page", return_value=(None, [], 2, 0, 1, "HTTPError: HTTP Error 403")):
            _fetch_rendered_identity_pages(record, {"requests": 0, "bytes": 0}, deadline=time.monotonic() + 5, max_requests=40)
        self.assertEqual(len(record["value"]["pages"]), 1)
        self.assertEqual(record["value"]["crawl_errors"][0]["error"], "HTTPError: HTTP Error 403")
        self.assertEqual(apply_website_identity_gate({"organisation_number": "923609016", "name": "Example AS"}, record)["website"]["status"], "ambiguous")

    def test_browser_skips_ineligible_and_conflicting_candidates_and_preserves_failures(self):
        from norway_company_agent.website import fetch_rendered_website

        record = {"status": "ambiguous", "source_url": "https://example.no/", "value": {"extraction_state": "static_complete"}}
        with patch("norway_company_agent.website.assert_public_url") as guard:
            self.assertEqual(fetch_rendered_website(record)[1]["requests"], 0)
            guard.assert_not_called()
            record["value"].update({"extraction_state": "js_fallback_candidate", "identity_assessment": {"reasons": ["homepage identifies a different legal organisation number"]}})
            self.assertEqual(fetch_rendered_website(record)[1]["requests"], 0)
            guard.assert_not_called()
        record["value"]["identity_assessment"] = {"reasons": []}
        with patch("norway_company_agent.website.assert_public_url", side_effect=ValueError("Private address")):
            failed, metrics = fetch_rendered_website(record)
        self.assertEqual(failed["status"], "ambiguous")
        self.assertEqual(failed["value"]["browser_error"]["status"], "source_error")
        self.assertEqual(metrics["requests"], 0)

    def test_only_extracts_declared_social_links(self):
        soup = BeautifulSoup('<a href="https://www.linkedin.com/company/example/">LinkedIn</a><a href="/about">About</a>', "html.parser")
        self.assertEqual(_social_links("https://example.no", soup), [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}])

    def test_extracts_embedded_company_social_profiles(self):
        soup = BeautifulSoup(
            '<div class="fb-page" data-href="https://www.facebook.com/ExampleCompany"></div>'
            '<iframe src="https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FSecondCompany"></iframe>',
            "html.parser",
        )
        self.assertEqual(
            _social_links("https://example.no/", soup),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "facebook", "url": "https://facebook.com/SecondCompany"},
            ],
        )

    def test_extracts_schema_same_as_company_social_profiles(self):
        value = [{
            "@type": "Organization",
            "sameAs": [
                "https://www.facebook.com/ExampleCompany/",
                "https://instagram.com/examplecompany",
                "https://linkedin.com/in/example-person",
            ],
        }]
        self.assertEqual(
            structured_social_links(value),
            [
                {"platform": "facebook", "url": "https://facebook.com/ExampleCompany"},
                {"platform": "instagram", "url": "https://instagram.com/examplecompany"},
            ],
        )

    def test_social_profiles_reject_share_event_group_and_policy_links(self):
        rejected = (
            "https://facebook.com/sharer.php?u=x", "https://facebook.com/events/123",
            "https://facebook.com/groups/123", "https://facebook.com/policy.php",
            "https://facebook.com/privacy/explanation",
            "https://linkedin.com/shareArticle?url=x", "https://instagram.com/p/abc",
        )
        self.assertTrue(all(normalize_social_url(url) is None for url in rejected))

    def test_social_profiles_canonicalize_www_variants(self):
        self.assertEqual(normalize_social_url("https://www.facebook.com/Example/"), {"platform": "facebook", "url": "https://facebook.com/Example"})
        self.assertEqual(normalize_social_url("https://linkedin.com/company/example/admin/feed/posts"), {"platform": "linkedin", "url": "https://linkedin.com/company/example"})
        self.assertEqual(normalize_social_url("https://youtube.com/channel/abc/featured"), {"platform": "youtube", "url": "https://youtube.com/channel/abc"})
        self.assertIsNone(normalize_social_url("https://facebook.com/profile.php"))
        self.assertIsNone(normalize_social_url("https://[object Object]"))

    def test_priority_pages_stay_on_exact_site(self):
        soup = BeautifulSoup('<a href="/kontakt">Contact</a><a href="https://other.no/about">About</a><a href="/products">Products</a>', "html.parser")
        self.assertEqual(_priority_links("https://example.no/", soup), ["https://example.no/kontakt"])

    def test_legal_pages_take_priority_and_contact_substrings_do_not_match(self):
        soup = BeautifulSoup('<a href="/kontaktlinser">Kontaktlinser</a><a href="/about/team">Team</a><a href="/policies/terms-of-service">Terms</a><a href="/kontakt-oss">Kontakt oss</a><a href="/kjopsvilkar">Kjøpsvilkår</a>', "html.parser")
        links = _priority_links("https://example.no/", soup, limit=2)
        self.assertEqual(links[0], "https://example.no/policies/terms-of-service")
        self.assertNotIn("https://example.no/kontaktlinser", links)
        self.assertEqual(links[1], "https://example.no/kjopsvilkar")

    def test_js_shell_is_only_a_fallback_candidate(self):
        shell = BeautifulSoup('<html><script src="a.js"></script><script src="b.js"></script></html>', "html.parser")
        self.assertEqual(_extraction_state("", shell), "js_fallback_candidate")
        self.assertEqual(_extraction_state("A" * 100, shell), "static_complete")

    def test_blocks_local_network_targets(self):
        for url in ("http://127.0.0.1/admin", "http://localhost/", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                assert_public_url(url)


class DiscoveryTests(unittest.TestCase):
    def test_brave_request_keeps_key_out_of_url_and_parses_in_memory(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"web": {"results": [{
                    "url": "https://example.no", "title": "Example AS", "description": "Example in Oslo",
                }]}}).encode()

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.get_header("X-subscription-token")
            captured["timeout"] = timeout
            return Response()

        with patch("scripts.run_brave_discovery.urllib.request.urlopen", fake_open):
            results, operation = brave_search({
                "name": "Example AS", "organisation_number": "999999999", "municipality": "OSLO",
            }, "secret-test-key", timeout=3.0, count=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(operation["status"], 200)
        self.assertNotIn("secret-test-key", captured["url"])
        self.assertEqual(captured["key"], "secret-test-key")
        self.assertEqual(captured["timeout"], 3.0)

    def test_company_query_contains_exact_name_org_and_location(self):
        query = build_company_search_query({
            "name": "Norsk Fiskeeksport AS", "organisation_number": "923 609 016", "municipality": "NOTODDEN",
        })
        self.assertEqual(query, '"Norsk Fiskeeksport AS" 923609016 NOTODDEN')

    def test_brave_parser_is_provider_neutral_candidate_input(self):
        results = parse_brave_web_results({"web": {"results": [
            {"url": "https://example.no", "title": "Example AS", "description": "Example in Oslo"},
            {"title": "Missing URL"},
        ]}}, query='"Example AS" 999999999')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider"], "brave_search_api")
        self.assertEqual(results[0]["rank"], 1)

    def test_directory_and_social_results_are_not_company_site_candidates(self):
        profile = {"organisation_number": "923609016", "name": "Example Norge AS", "municipality": "OSLO"}
        for url in ("https://proff.no/selskap/example", "https://linkedin.com/company/example"):
            with self.subTest(url=url):
                self.assertEqual(score_search_candidate(profile, {"url": url, "title": "Example Norge AS"})["status"], "rejected")

    def test_exact_name_in_title_and_host_is_only_a_crawl_candidate(self):
        profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "municipality": "NOTODDEN"}
        decision = choose_search_candidate(profile, [{
            "url": "https://norskfiskeeksport.no/",
            "title": "Norsk Fiskeeksport AS",
            "snippet": "Seafood exporter in Notodden",
            "rank": 1,
            "provider": "fixture",
        }])
        self.assertFalse(decision["abstained"])
        self.assertEqual(decision["selected"]["status"], "accepted_for_crawl")
        self.assertIn("Publication still requires", decision["policy"])

    def test_ambiguous_name_match_without_host_support_abstains(self):
        profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "municipality": "NOTODDEN"}
        decision = choose_search_candidate(profile, [{"url": "https://parent-group.no/", "title": "Norsk Fiskeeksport AS - portfolio", "snippet": "Group companies"}])
        self.assertTrue(decision["abstained"])


class SentimentTests(unittest.TestCase):
    @staticmethod
    def item(item_id, label="positive", source="https://news.example/a", **changes):
        item = {
            "id": str(item_id), "label": label, "exact_entity": True,
            "source_class": "licensed_news", "source_url": source,
            "retrieved_at": "2026-08-22T00:00:00Z", "evidence_span": "Exact-company event sentence.",
            "content_sha256": "a" * 64,
        }
        item.update(changes)
        return item

    def test_company_owned_or_unhashed_items_are_not_publishable(self):
        self.assertFalse(publishable_sentiment_item(self.item(1, source_class="company_owned")))
        self.assertFalse(publishable_sentiment_item(self.item(1, content_sha256=None)))

    def test_inference_input_requires_exact_entity_independent_source_and_hash(self):
        item = self.item(1, text="Selskapet vant en ny kontrakt.")
        self.assertTrue(sentiment_input_eligibility(item)[0])
        accepted, reasons = sentiment_input_eligibility({**item, "exact_entity": False, "content_sha256": None})
        self.assertFalse(accepted)
        self.assertIn("exact company identity is not verified", reasons)
        self.assertIn("missing content_sha256", reasons)

    def test_pinned_model_output_normalization_is_closed_set(self):
        self.assertEqual(len(MODEL_REVISION), 40)
        self.assertEqual(normalize_generated_label(" Positive. "), "positive")
        self.assertIsNone(normalize_generated_label("bullish"))

    def test_rollup_requires_two_independent_publishers(self):
        same_publisher = [
            self.item(1, source="https://news.example/a"),
            self.item(2, source="https://news.example/b"),
        ]
        self.assertEqual(aggregate_company_sentiment(same_publisher)["status"], "abstain")
        independent = same_publisher + [self.item(3, source="https://other.example/c")]
        result = aggregate_company_sentiment(independent)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["label"], "positive")

    def test_perfect_balanced_300_item_corpus_passes_poc_gate(self):
        labels = ("positive", "neutral", "negative", "mixed")
        gold = [{"id": str(i), "label": labels[i % 4]} for i in range(300)]
        predictions = [self.item(i, labels[i % 4], source=f"https://news{i % 7}.example/item/{i}") for i in range(300)]
        report = evaluate_predictions(gold, predictions)
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["macro_f1"], 1.0)
        self.assertTrue(report["qualification_passed"])
        self.assertFalse(report["production_scale_gate_passed"])

    def test_wrong_entity_and_company_owned_predictions_fail_gate(self):
        labels = ("positive", "neutral", "negative")
        gold = [{"id": str(i), "label": labels[i % 3]} for i in range(300)]
        predictions = [self.item(i, labels[i % 3], source=f"https://news.example/{i}") for i in range(300)]
        predictions[0]["exact_entity"] = False
        predictions[1]["source_class"] = "company_owned"
        report = evaluate_predictions(gold, predictions)
        self.assertEqual(report["wrong_entity_predictions"], 1)
        self.assertEqual(report["company_owned_predictions"], 1)
        self.assertFalse(report["qualification_passed"])

    def test_qualification_minimum_cannot_be_weakened(self):
        with self.assertRaises(ValueError):
            evaluate_predictions([{"id": "1", "label": "positive"}], [self.item(1)], minimum_items=1)


class OfficialNormalizationTests(unittest.TestCase):
    def test_latest_financials_are_selected_before_limiting_and_keep_company_scope(self):
        from norway_company_agent.official import latest_financial_record

        body = [{"id": year * 10 + scope, "regnskapstype": kind,
                 "regnskapsperiode": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"},
                 "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": year * 10 + scope}}}}
                for year in (2023, 2024, 2025) for scope, kind in ((1, "KONSERN"), (2, "SELSKAP"))]
        records = normalize_financials(body)["records"]
        self.assertEqual(len(records), 6)
        self.assertEqual(records[0]["period"]["tilDato"], "2025-12-31")
        self.assertEqual(records[0]["account_type"], "SELSKAP")
        self.assertEqual(latest_financial_record(list(reversed(records)))["revenue"], 20252)
        company_old = records[-1]
        company_old = {**company_old, "account_type": "SELSKAP"}
        newer_group = {**records[0], "account_type": "KONSERN"}
        self.assertEqual(latest_financial_record([newer_group, company_old]), company_old)
        row = {"organisation_number": "923609016", "evidence": {"financials": evidence("financials", "available", "official", "https://example.no/accounts", value={"records": records})}}
        facts = answer_profile(row, "Show financials")["facts"]
        self.assertEqual(next(f["value"] for f in facts if f["claim"] == "Revenue"), 20252)
        self.assertEqual(next(f["value"] for f in facts if f["claim"] == "Account scope"), "SELSKAP")

    def test_accounting_obligation_is_categorical_for_as_but_not_enk(self):
        company = accounting_obligation_assessment({"organisation_number": "923609016", "legal_form": "AS"})
        sole_trader = accounting_obligation_assessment({"organisation_number": "923609017", "legal_form": "ENK", "employees": 0})
        self.assertEqual(company["value"]["classification"], "required_by_legal_form")
        self.assertEqual(sole_trader["value"]["classification"], "threshold_or_activity_dependent")
        self.assertTrue(company["content_sha256"])
        self.assertEqual(company["source_row_key"], "923609016")

    def test_observed_filing_overrides_rule_path(self):
        record = accounting_obligation_assessment({"organisation_number": "923609018", "legal_form": "ENK", "latest_submitted_accounts": "2024"})
        self.assertEqual(record["value"]["classification"], "filing_observed")

    def test_entity_normalization_keeps_identity(self):
        record = normalize_entity({"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "organisasjonsform": {"kode": "ASA"}})
        self.assertEqual(record["organisation_number"], "923609016")
        self.assertEqual(record["legal_form"], "ASA")

    def test_financial_fields_keep_period_currency_and_zero(self):
        body = [{"id": 1, "valuta": "NOK", "regnskapsperiode": {"tilDato": "2025-12-31"}, "resultatregnskapResultat": {"driftsresultat": {"driftsresultat": 0, "driftsinntekter": {"sumDriftsinntekter": 12}}}}]
        record = normalize_financials(body)["records"][0]
        self.assertEqual(record["revenue"], 12)
        self.assertEqual(record["operating_result"], 0)
        self.assertEqual(record["currency"], "NOK")

    def test_financial_history_is_sorted_and_links_to_official_pdfs(self):
        record = normalize_financial_history(["2024", "2022", "2024", "invalid"], "923609016")
        self.assertEqual(record["years"], ["2022", "2024"])
        self.assertEqual(record["pdfs"][0]["year"], "2024")
        self.assertTrue(record["pdfs"][0]["url"].endswith("/923609016/2024"))

    def test_public_roles_drop_birth_dates(self):
        body = {"rollegrupper": [{"type": {"kode": "STYR"}, "roller": [{"type": {"kode": "LEDE", "beskrivelse": "Chair"}, "person": {"fodselsdato": "1970-01-01", "navn": {"fornavn": "Ada", "etternavn": "Nord"}}}]}]}
        record = normalize_roles(body)["roles"][0]
        self.assertEqual(record["name"], "Ada Nord")
        self.assertNotIn("fodselsdato", json.dumps(record))


class ResearchAgentTests(unittest.TestCase):
    def test_summary_distinguishes_baseline_and_source_linked_unknowns(self):
        row = {"organisation_number": "923609016", "name": "Example AS", "evidence": {
            "jobs": {"status": "blocked", "source_url": "https://pam-stilling-feed.nav.no/api/v1/feed", "retrieved_at": "2026-10-08T00:00:00Z"}}}
        answer = answer_profile(row, "Summarize this company")
        self.assertEqual(answer["refresh_summary"]["state"], "baseline")
        hiring = next(item for item in answer["uncertainties"] if item["module"] == "jobs")
        self.assertEqual(hiring["status"], "blocked")
        self.assertEqual(hiring["source_url"], row["evidence"]["jobs"]["source_url"])
        self.assertIn("does not mean zero", hiring["value"])
        self.assertIsNone(hiring["content_sha256"])
        row["refresh_baseline"] = None  # A prior input can lack a completion date.
        self.assertEqual(answer_profile(row, "Summarize this company")["refresh_summary"]["state"], "no_verified_changes")

    @staticmethod
    def screen_row(org, municipality, employees, revenue):
        return {
            "organisation_number": org,
            "name": f"Company {org}",
            "municipality": municipality,
            "employees": employees,
            "evidence": {
                "registry": evidence("registry", "available", "official_registry_bulk", "https://example.test/registry", content_sha256="a" * 64),
                "financials": evidence("financials", "available", "official_annual_accounts", "https://example.test/accounts", value={"records": [{"revenue": revenue, "annual_result": 1}]}, content_sha256="b" * 64),
            },
        }

    def test_cross_company_screen_has_exact_membership_and_inspectable_plan(self):
        rows = [
            self.screen_row("111111111", "OSLO", 20, 2_000_000),
            self.screen_row("222222222", "OSLO", 5, 2_000_000),
            self.screen_row("333333333", "BERGEN", 20, 2_000_000),
            self.screen_row("444444444", "OSLO", 20, 500_000),
        ]
        result = screen_profiles(rows, "companies in Oslo with more than 10 employees and revenue over 1 million")
        self.assertFalse(result["abstained"])
        self.assertEqual([item["organisation_number"] for item in result["results"]], ["111111111"])
        self.assertEqual(len(result["plan"]["filters"]), 3)
        self.assertTrue(all(citation["content_sha256"] for citation in result["results"][0]["citations"]))

    def test_unsupported_cross_company_criterion_abstains(self):
        plan = parse_screen_query("companies in Oslo with positive Glassdoor sentiment")
        self.assertFalse(plan["executable"])
        result = screen_profiles([], "companies in Oslo with positive Glassdoor sentiment")
        self.assertTrue(result["abstained"])
        self.assertIn("Glassdoor", result["reason"])

    def test_missing_website_is_not_treated_as_proven_absence(self):
        result = screen_profiles([], "companies without a website")
        self.assertTrue(result["abstained"])
        self.assertIn("does not prove", result["reason"])

    def test_workspace_saves_pins_history_and_recovers_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            workspace, warning = load_workspace(path)
            self.assertIsNone(warning)
            updated = record_screen(workspace, {"query": "in Oslo", "plan": {"filters": []}, "result_count": 1, "results": [{"organisation_number": "111111111"}]}, pin_organisations=["111111111"])
            save_workspace(path, updated)
            loaded, warning = load_workspace(path)
            self.assertEqual(loaded["pins"], ["111111111"])
            self.assertEqual(len(loaded["history"]), 1)
            path.write_text("not json", encoding="utf-8")
            recovered, warning = load_workspace(path)
            self.assertEqual(recovered["pins"], [])
            self.assertIn("recovery", warning or "")

    def test_sentiment_abstains_and_every_fact_has_a_source(self):
        row = {
            "organisation_number": "923609016", "name": "Example AS", "legal_form": "AS",
            "municipality": "OSLO", "employees": None,
            "evidence": {"registry": evidence("registry", "available", "official", "https://example.test")},
        }
        result = answer_profile(row, "Give me employee sentiment")
        self.assertTrue(any("Sentiment is not scored" in item for item in result["unsupported_or_uncertain"]))
        self.assertTrue(all(fact["source_url"] for fact in result["facts"]))
        self.assertFalse(any(fact["claim"] == "Registry employee count" for fact in result["facts"]))

    def test_natural_leads_word_routes_to_roles(self):
        roles = evidence("roles", "available", "official", "https://example.test/roles", value={"roles": [{"name": "Ada Nord", "role": "Chair", "inactive": False}]})
        row = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"roles": roles}}
        result = answer_profile(row, "Who leads this company?")
        self.assertEqual(result["facts"][0]["value"], "Ada Nord")

    def test_quarantined_website_claims_are_not_returned(self):
        website = evidence("website", "available", "company_site", "https://parent.test", value={"description": "Parent claim", "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/parent"}], "identity_assessment": {"publishable": False}})
        row = {"organisation_number": "923609016", "name": "Subsidiary AS", "evidence": {"website": website}}
        result = answer_profile(row, "What social information is available?")
        self.assertFalse(result["facts"])
        self.assertTrue(any("quarantined" in item for item in result["unsupported_or_uncertain"]))


class PrototypeTests(unittest.TestCase):
    def test_nav_vacancy_dates_survive_metric_normalization(self):
        row = {"organisation_number": "923609016", "name": "Example AS", "legal_form": "AS", "employees": None,
               "municipality": "OSLO", "industry_code": None, "industry_label": None, "bankrupt": False, "liquidating": False,
               "evidence": {"jobs": {"status": "available", "value": {"job_count": 1}}}}
        job = {"id": "nav-job", "organisation_number": "923609016", "platform": "job_board", "signal_type": "job_posting",
               "source_class": "job_board", "source_url": "https://pam-stilling-feed.nav.no/api/v1/feedentry/job",
               "retrieved_at": "2026-10-08T00:00:00Z", "content_sha256": "a" * 64, "exact_entity": True,
               "identity_proof": [{"type": "employer_organisation_number", "value": "923609016"}],
               "evidence_span": "Engineer", "acquisition_mode": "official_api", "rights_status": "approved",
               "metrics": {"title": "Engineer", "published": "2026-10-01T00:00:00Z", "expires": "2099-12-31T00:00:00Z"}}
        row["observations"] = [job]
        captured = compact_prototype(row)["external"]["jobs"][0]
        self.assertEqual(captured["publishedAt"], job["metrics"]["published"])
        self.assertEqual(captured["expiresAt"], job["metrics"]["expires"])
        job["published_at"] = "2026-10-02T00:00:00Z"
        self.assertEqual(compact_prototype(row)["external"]["jobs"][0]["publishedAt"], job["published_at"])

    def test_prototype_retains_dated_activity_and_blocked_jobs_without_foreign_claims(self):
        row = {"organisation_number": "923609016", "name": "Example AS", "legal_form": "AS", "employees": None,
               "municipality": "OSLO", "industry_code": None, "industry_label": None, "bankrupt": False, "liquidating": False,
               "evidence": {"jobs": {"status": "blocked", "value": None}}}
        post = {"id": "post-1", "organisation_number": "923609016", "platform": "company_site", "signal_type": "public_post",
                "source_class": "company_site", "source_url": "https://example.no/news", "retrieved_at": "2026-10-08T00:00:00Z",
                "published_at": "2023-10-26T15:01:03", "content_sha256": "a" * 64, "exact_entity": True,
                "identity_proof": [{"type": "website_identity"}], "evidence_span": "An older publication",
                "acquisition_mode": "permitted_public_page", "rights_status": "approved", "metrics": {"title": "An older publication"}}
        row["observations"] = [post, {**post, "id": "foreign", "organisation_number": "999999999"},
                               {**post, "id": "unapproved", "rights_status": "review_required"}]
        result = compact_prototype(row)
        self.assertEqual(len(result["external"]["posts"]), 1)
        self.assertEqual(result["external"]["posts"][0]["publishedAt"], "2023-10-26T15:01:03")
        self.assertEqual(result["external"]["posts"][0]["hash"], "a" * 64)
        self.assertEqual(result["jobs"]["status"], "blocked")
        self.assertEqual(result["external"]["jobs"], [])

    def test_qualification_copy_keeps_production_boundary_visible(self):
        header, boundary = qualification_copy({
            "weighted_score": {"verified_points": 100, "maximum_points": 100},
            "qualification": {"poc_qualified": True, "production_qualified": False},
        })
        self.assertIn("100/100", header)
        self.assertIn("not production-qualified", header)
        self.assertIn("remain quarantined", boundary)

    def test_quarantined_social_links_are_counted_but_not_published(self):
        row = {
            "organisation_number": "923609016",
            "name": "Subsidiary AS",
            "legal_form": "AS",
            "employees": None,
            "municipality": "OSLO",
            "industry_code": None,
            "industry_label": None,
            "website": "https://parent.test",
            "bankrupt": False,
            "liquidating": False,
            "evidence": {
                "website": evidence(
                    "website",
                    "available",
                    "company_site",
                    "https://parent.test",
                    value={
                        "identity_assessment": {"publishable": False},
                        "social_links": [],
                        "discovered_social_links": [
                            {"platform": "linkedin", "url": "https://linkedin.com/company/parent"}
                        ],
                    },
                )
            },
        }
        website = compact_prototype(row)["web"]["value"]
        self.assertEqual(website["quarantined_social_count"], 1)
        self.assertEqual(website["social_links"], [])


class RefreshTests(unittest.TestCase):
    def test_publication_refresh_distinguishes_discovery_from_publication_and_no_false_deletion(self):
        import copy
        previous = {"organisation_number": "923609016", "evidence": {"website": {"status": "available", "retrieved_at": "2026-10-01T00:00:00Z"}}, "observations": []}
        item = {"id": "post-1", "organisation_number": "923609016", "platform": "company_site", "signal_type": "public_post", "source_class": "company_site",
                "source_url": "https://example.no/news/post", "retrieved_at": "2026-10-08T00:00:00Z", "published_at": "2025-01-01",
                "content_sha256": "a" * 64, "exact_entity": True, "identity_proof": [{"type": "website_identity"}], "evidence_span": "A publication",
                "acquisition_mode": "permitted_public_page", "rights_status": "approved", "metrics": {"title": "A publication", "published_at": "2025-01-01"}}
        current = copy.deepcopy(previous); current["observations"] = [item]
        self.assertEqual(diff_profile(previous, current)[0]["change_type"], "publication_discovered")
        self.assertEqual(diff_profile(current, copy.deepcopy(current)), [])
        self.assertEqual(diff_profile(current, previous), [])
        item["published_at"] = "2026-10-07"
        item["metrics"]["published_at"] = "2026-10-07"
        self.assertEqual(diff_profile(previous, current)[0]["change_type"], "new_publication")
        answer = answer_profile(current, "Show dated news")
        self.assertEqual(answer["facts"][0]["classification"], "company_reported_publication")
        self.assertEqual(answer["facts"][0]["content_sha256"], "a" * 64)
        current["evidence"]["website"]["status"] = "blocked"
        self.assertEqual(diff_profile(previous, current), [])

    def test_snapshot_fetcher_hashes_evaluator_bytes_and_carries_times(self):
        url = "https://example.test/entity/1"
        fetcher = SnapshotFetcher({"retrieved_at": "2026-01-02T00:00:00Z", "effective_at": "2026-01-01T00:00:00Z", "responses": {url: {"body": {"value": 1}}}})
        result = fetcher(url)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(result.content_sha256 or ""), 64)
        self.assertEqual(result.effective_at, "2026-01-01T00:00:00Z")

    def test_identical_refresh_is_an_idempotent_noop(self):
        row = {"organisation_number": "923609016", "name": "Example AS", "employees": 4}
        self.assertEqual(diff_profile(row, dict(row)), [])

    def test_missing_to_zero_is_a_real_change_with_provenance(self):
        source = evidence("registry", "available", "official", "https://example.test/entity")
        old = {"organisation_number": "923609016", "employees": None, "evidence": {"registry": source}}
        new = {"organisation_number": "923609016", "employees": 0, "evidence": {"registry": source}}
        change = diff_profile(old, new)[0]
        self.assertIsNone(change["old_value"])
        self.assertEqual(change["new_value"], 0)
        self.assertEqual(change["source_url"], "https://example.test/entity")

    def test_refresh_rejects_membership_or_identity_drift(self):
        with self.assertRaises(ValueError):
            diff_datasets([{"organisation_number": "923609016"}], [{"organisation_number": "999999999"}])


class WebsiteIdentityTests(unittest.TestCase):
    def test_registered_contact_proof_requires_homepage_name_and_all_exact_contacts(self):
        import copy
        profile = {"organisation_number": "913006860", "name": "C OPTIKK EIDSVOLL AS", "evidence": {
            "registry_live": evidence("registry_live", "available", "official", "https://data.brreg.no/entity/913006860", content_sha256="a" * 64, value={
                "organisation_number": "913006860", "name": "C OPTIKK EIDSVOLL AS", "telephone": "64 80 98 00",
                "business_address": {"adresse": ["Gladbakkvegen 1"], "postnummer": "2070"},
            }),
            "website": {"status": "available", "value": {"final_url": "https://example.no/", "content_sha256": "b" * 64,
                "title": "Startside | c)optikk Eidsvoll", "identity_text_excerpt": "Kontakt oss 64 80 98 00 Gladbakkvegen 1, 2070 Råholt"}},
        }}
        result = assess_website_identity(profile)
        self.assertTrue(result["publishable"])
        self.assertEqual(result["identity_evidence"][0]["registry_content_sha256"], "a" * 64)
        variants = [
            ("title", "Parent Optikk"),
            ("title", "C Optikk Eidsvoll Holding AS"),
            ("title", "C Optikk Eidsvoll Eiendom"),
            ("identity_text_excerpt", "64 80 98 00 Gladbakkvegen 1, 9999 Råholt"),
            ("identity_text_excerpt", "64 80 98 00 Gladbakkvegen 2, 2070 Råholt"),
            ("identity_text_excerpt", "99 99 99 99 Gladbakkvegen 1, 2070 Råholt"),
            ("identity_text_excerpt", "64 80 98 00 Gladbakkvegen 1, 2070 Råholt Org nr 999888777"),
        ]
        for key, replacement in variants:
            changed = copy.deepcopy(profile)
            changed["evidence"]["website"]["value"][key] = replacement
            changed["evidence"]["website"]["value"]["pages"] = [{"main_text_excerpt": "C Optikk Eidsvoll AS"}]
            with self.subTest(key=key, replacement=replacement):
                self.assertFalse(assess_website_identity(changed)["publishable"])
        profile["evidence"]["registry_live"]["value"]["organisation_number"] = "999888777"
        self.assertFalse(assess_website_identity(profile)["publishable"])

    def test_article_capture_rejects_other_article_and_modified_only_dates(self):
        from norway_company_agent.website import _article_capture, _activity_links, fetch_site_activity_pages

        soup = BeautifulSoup('<h1>Company announcement</h1><a href="/news/item">Read</a><a href="https://other.no/news/item">Other</a>', "html.parser")
        metadata = {"json-ld": [{"@type": "NewsArticle", "headline": "Company announcement", "datePublished": "2026-08-22", "url": "https://example.no/news/item"}]}
        self.assertEqual(_article_capture("https://example.no/news/item", soup, metadata)["published_at"], "2026-08-22")
        self.assertIsNone(_article_capture("https://example.no/news/other", soup, metadata))
        metadata["json-ld"][0].pop("datePublished")
        metadata["json-ld"][0]["dateModified"] = "2026-08-22"
        self.assertIsNone(_article_capture("https://example.no/news/item", soup, metadata))
        self.assertEqual(_activity_links("https://example.no/", soup), ["https://example.no/news/item"])
        cards = BeautifulSoup('<h2 class="wp-block-post-title"><a href="/an-article">Title</a></h2><a href="/post/norsk-å">Blog post</a>', "html.parser")
        self.assertEqual(_activity_links("https://example.no/blog", cards), ["https://example.no/an-article", "https://example.no/post/norsk-%C3%A5"])
        with patch("norway_company_agent.website._fetch_secondary_page") as fetch:
            self.assertEqual(fetch_site_activity_pages({"status": "ambiguous", "value": {}})["requests"], 0)
            fetch.assert_not_called()

    def test_activity_crawl_is_bounded_and_retains_page_errors(self):
        from norway_company_agent.website import fetch_site_activity_pages

        website = {"status": "available", "value": {"final_url": "https://example.no/", "identity_assessment": {"publishable": True},
                   "pages": [{"url": "https://example.no/", "activity_links": ["https://example.no/news/a", "https://example.no/news/b", "https://example.no/news/c"]}]}}
        with patch("norway_company_agent.website._fetch_secondary_page", return_value=(None, [], 2, 0, 8, "HTTP 403")) as fetch:
            metrics = fetch_site_activity_pages(website, max_pages=2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(metrics["requests"], 4)
        self.assertEqual(len(website["value"]["crawl_errors"]), 2)
        other_host_page = {"url": "https://other.example.no/news/a"}
        with patch("norway_company_agent.website._fetch_secondary_page", return_value=(other_host_page, [], 2, 10, 8, None)):
            fetch_site_activity_pages(website, max_pages=1)
        self.assertEqual(len(website["value"]["pages"]), 1)
        self.assertIn("outside verified homepage host", website["value"]["crawl_errors"][-1]["error"])

    def test_advertised_rss_fallback_retains_original_dates_and_response_hash(self):
        import hashlib
        from norway_company_agent.website import _publication_feed_links, fetch_site_activity_pages
        from scripts.extract_company_site_news import observations

        soup = BeautifulSoup('<link rel="alternate" type="application/rss+xml" title="Comments" href="/comments/feed/"><link rel="alternate" type="application/rss+xml" href="https://other.no/feed/"><link rel="alternate" type="application/rss+xml" href="/?feed=rss2">', "html.parser")
        links = _publication_feed_links("https://example.no/", soup)
        self.assertEqual(links, ["https://example.no/?feed=rss2"])
        raw = b'<rss><channel><item><title>New service</title><link>https://example.no/new-service</link><pubDate>Sat, 22 Aug 2026 12:00:00 +0000</pubDate></item></channel></rss>'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, *_args): return raw
            def geturl(self): return links[0]
        website = {"status": "available", "value": {"final_url": "https://example.no/", "identity_assessment": {"publishable": True, "score": 1.0}, "pages": [{"url": "https://example.no/", "activity_links": [], "publication_feed_links": links}]}}
        with patch("norway_company_agent.website.assert_public_url"), patch("norway_company_agent.website._robots_allowed", return_value=True), patch("norway_company_agent.website.SAFE_OPENER.open", return_value=Response()) as fetch:
            metrics = fetch_site_activity_pages(website, max_pages=1)
        self.assertEqual(metrics["requests"], 2)
        self.assertEqual(fetch.call_count, 1)
        profile = {"organisation_number": "923609016", "evidence": {"website": website}}
        items = observations(profile)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["source_url"], links[0])
        self.assertEqual(item["content_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(item["date_evidence_span"], "Sat, 22 Aug 2026 12:00:00 +0000")
        self.assertEqual(item["published_at"], "2026-08-22T12:00:00+00:00")
        self.assertEqual(item["metrics"]["publication_url"], "https://example.no/new-service")
        self.assertTrue(publishable_observation(item))
        website["value"]["publication_feeds"][0]["items"][0]["published_at"] = "2099-01-01T00:00:00Z"
        self.assertEqual(observations(profile), [])

    def test_rss_parser_rejects_unsupported_publication_evidence(self):
        from norway_company_agent.website import _parse_publication_feed

        raw = b'<rss><channel><item><title>Other publisher</title><link>https://other.no/news</link><pubDate>Sat, 22 Aug 2026 12:00:00 +0000</pubDate></item><item><title>No date</title><link>https://example.no/news</link></item><item><title>Comment</title><link>https://example.no/news#comment-1</link><pubDate>Sat, 22 Aug 2026 12:00:00 +0000</pubDate></item><item><title>No link</title><pubDate>Sat, 22 Aug 2026 12:00:00 +0000</pubDate></item></channel></rss>'
        self.assertEqual(_parse_publication_feed(raw, "https://example.no/feed/"), [])
        with self.assertRaises(ValueError):
            _parse_publication_feed(b'<!DOCTYPE rss [<!ENTITY x "value">]><rss/>', "https://example.no/feed/")
        with self.assertRaises(ValueError):
            _parse_publication_feed(b'<html/>', "https://example.no/feed/")

    def test_rss_fallback_respects_activity_budget_and_verified_host(self):
        from norway_company_agent.website import fetch_site_activity_pages

        website = {"status": "available", "value": {"final_url": "https://example.no/", "identity_assessment": {"publishable": True}, "pages": [{"url": "https://example.no/", "activity_links": ["https://example.no/news/a", "https://example.no/news/b"], "publication_feed_links": ["https://example.no/feed/"]}]}}
        with patch("norway_company_agent.website._fetch_secondary_page", return_value=(None, [], 2, 0, 8, "HTTP 403")), patch("norway_company_agent.website.SAFE_OPENER.open") as fetch:
            self.assertEqual(fetch_site_activity_pages(website, max_pages=2)["requests"], 4)
            fetch.assert_not_called()
        website["value"]["pages"][0]["activity_links"] = []
        website["value"]["pages"][0]["publication_feed_links"] = ["https://other.no/feed/"]
        with patch("norway_company_agent.website.SAFE_OPENER.open") as fetch:
            self.assertEqual(fetch_site_activity_pages(website)["requests"], 0)
            fetch.assert_not_called()
        self.assertNotIn("publication_feeds", website["value"])

    def test_social_suffixes_accept_official_handles_without_accepting_parent_variants(self):
        profile = {"name": "NOVOOI AS"}
        for handle in ("novooi", "novooi.official", "novooi.com"):
            self.assertTrue(assess_social_identity(profile, {"url": "https://instagram.com/" + handle})["publishable"])
        for handle in ("novooi.holding", "novooi.group", "novooi-norway", "othernovooi"):
            self.assertFalse(assess_social_identity(profile, {"url": "https://instagram.com/" + handle})["publishable"])

    def test_social_observation_uses_captured_page_and_abstains_without_capture(self):
        from scripts.run_competition_batch import site_social_observations
        from norway_company_agent.website import _page_social_captures

        soup = BeautifulSoup('<a href="https://www.linkedin.com/company/novooi/">LinkedIn</a>', "html.parser")
        captures = _page_social_captures("https://novooi.com/contact", soup, [{"sameAs": ["https://www.instagram.com/novooi.com"]}])
        profile = {"organisation_number": "923609016", "name": "NOVOOI AS", "evidence": {"website": {
            "status": "available", "retrieved_at": "2026-10-09T00:00:00Z", "content_sha256": "a" * 64,
            "value": {"identity_assessment": {"publishable": True}, "social_links": [{"platform": x["platform"], "url": x["url"]} for x in captures],
                      "pages": [{"url": "https://novooi.com/contact", "content_sha256": "b" * 64, "social_link_evidence": captures}]},
        }}}
        observations = site_social_observations(profile)
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(x["source_url"] == "https://novooi.com/contact" and x["content_sha256"] == "b" * 64 for x in observations))
        self.assertEqual({x["evidence_span"] for x in observations}, {"https://www.linkedin.com/company/novooi/", "https://www.instagram.com/novooi.com"})
        profile["evidence"]["website"]["value"]["pages"][0]["social_link_evidence"] = []
        self.assertEqual(site_social_observations(profile), [])

    def test_legal_seller_proof_accepts_exact_operator_and_rejects_unsafe_variants(self):
        import copy

        profile = {"organisation_number": "830961682", "name": "LESKE AS", "evidence": {"website": {"status": "available", "value": {
            "final_url": "https://leske.no/", "title": "Leske.no", "pages": [{
                "url": "https://leske.no/policies/terms-of-service", "title": "Salgsbetingelser",
                "content_sha256": "a" * 64,
                "main_text_excerpt": "2. Partene\nSelger er LESKE AS, hei@leske.no, Oslo org nr830961682, og betegnes som selger.",
            }],
        }}}}
        assessment = assess_website_identity(profile)
        self.assertTrue(assessment["publishable"])
        self.assertEqual(assessment["identity_evidence"][0]["content_sha256"], "a" * 64)
        variants = [
            ("main_text_excerpt", "Vårt datterselskap LESKE AS, org nr830961682"),
            ("main_text_excerpt", "Selger er LESKE HOLDING AS, org nr830961682"),
            ("main_text_excerpt", "Selger er LESKE AS, org nr999888777"),
            ("main_text_excerpt", "Selger er LESKE AS, org nr830961682 og org nr999888777"),
            ("url", "https://other.no/policies/terms-of-service"),
            ("url", "https://leske.no:8443/policies/terms-of-service"),
            ("content_sha256", ""),
        ]
        for key, replacement in variants:
            changed = copy.deepcopy(profile)
            changed["evidence"]["website"]["value"]["pages"][0][key] = replacement
            with self.subTest(key=key, replacement=replacement):
                self.assertFalse(assess_website_identity(changed)["publishable"])
        profile["evidence"]["website"]["value"]["identity_text_excerpt"] = "Org nr999888777"
        self.assertFalse(assess_website_identity(profile)["publishable"])

    def test_registered_domain_contact_address_requires_all_identity_anchors(self):
        import copy

        profile = {"organisation_number": "930290998", "name": "IZOMAX AS", "evidence": {
            "registry_live": {"status": "available", "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/930290998",
                              "content_sha256": "a" * 64, "retrieved_at": "2026-10-09T00:00:00Z",
                              "value": {"organisation_number": "930290998", "name": "IZOMAX AS", "website": "izomax.com",
                                        "business_address": {"adresse": ["Lagerveien 17"], "postnummer": "4033"}}},
            "website": {"status": "available", "value": {"final_url": "https://izomax.com/", "title": "Izomax | Engineering", "content_sha256": "c" * 64,
                         "pages": [{"url": "https://izomax.com/contact/", "title": "Contact", "content_sha256": "b" * 64,
                                    "retrieved_at": "2026-10-09T00:00:01Z", "identity_text_excerpt": "Izomax HQ Lagerveien 17, 4033 Stavanger"}]}}
        }}
        assessment = assess_website_identity(profile)
        self.assertTrue(assessment["publishable"])
        self.assertEqual(assessment["identity_evidence"][0]["source_url"], "https://izomax.com/contact/")
        international = copy.deepcopy(profile)
        international["evidence"]["registry_live"]["value"]["website"] = "www.føniks.no"
        international_site = international["evidence"]["website"]["value"]
        international_site["final_url"] = "https://xn--fniks-vua.no/"
        international_site["pages"][0]["url"] = "https://xn--fniks-vua.no/contact/"
        self.assertTrue(assess_website_identity(international)["publishable"])
        international_site["final_url"] = "https://foniks.no/"
        self.assertFalse(assess_website_identity(international)["publishable"])
        for variant in ("foreign_domain", "parent_title", "wrong_address", "missing_hash", "missing_homepage_hash", "foreign_contact", "portfolio", "conflicting_org", "wrong_live_org"):
            changed = copy.deepcopy(profile)
            site = changed["evidence"]["website"]["value"]
            page = site["pages"][0]
            if variant == "foreign_domain": site["final_url"] = "https://other.no/"
            elif variant == "parent_title": site["title"] = "Izomax Holding | Engineering"
            elif variant == "wrong_address": page["identity_text_excerpt"] = "Izomax HQ Lagerveien 18, 4033 Stavanger"
            elif variant == "missing_hash": page["content_sha256"] = None
            elif variant == "missing_homepage_hash": site["content_sha256"] = None
            elif variant == "foreign_contact": page["url"] = "https://other.izomax.com/contact/"
            elif variant == "portfolio": page["url"] = "https://izomax.com/portfolio/"; page["title"] = "Portfolio"
            elif variant == "conflicting_org": page["identity_text_excerpt"] += " Org nr999888777"
            elif variant == "wrong_live_org": changed["evidence"]["registry_live"]["value"]["organisation_number"] = "999888777"
            with self.subTest(variant=variant):
                self.assertFalse(assess_website_identity(changed)["publishable"])

    def test_merchant_legal_contact_policy_requires_exact_name_org_and_trade_domain(self):
        import copy

        profile = {"organisation_number": "935664330", "name": "GOLF UNITY AS", "evidence": {"website": {"status": "available", "value": {
            "final_url": "https://golfunity.no/", "title": "Golfunity", "pages": [{"url": "https://golfunity.no/policies/contact-information",
            "content_sha256": "a" * 64, "main_text_excerpt": "Handelsnavn: Golfunity.no\nFysisk adresse: Golf Unity AS, Hadelandsveien 835\nOrganisasjonsnummer: 935 664 330"}]}}}}
        self.assertTrue(assess_website_identity(profile)["publishable"])
        for variant in ("wrong_domain", "wrong_name", "foreign_number", "extra_number", "group_contact"):
            changed = copy.deepcopy(profile); page = changed["evidence"]["website"]["value"]["pages"][0]
            if variant == "wrong_domain": page["main_text_excerpt"] = page["main_text_excerpt"].replace("Golfunity.no", "parent.no")
            elif variant == "wrong_name": page["main_text_excerpt"] = page["main_text_excerpt"].replace("Golf Unity AS", "Golf Unity Holding AS")
            elif variant == "foreign_number": page["main_text_excerpt"] = page["main_text_excerpt"].replace("935 664 330", "999 888 777")
            elif variant == "extra_number": page["main_text_excerpt"] += "\nOrganisasjonsnummer: 999888777"
            elif variant == "group_contact": page["url"] = "https://golfunity.no/contact"
            with self.subTest(variant=variant): self.assertFalse(assess_website_identity(changed)["publishable"])

    def test_contact_priority_and_address_capture_preserve_identity_within_page_budget(self):
        from norway_company_agent.website import _identity_text, _priority_links

        soup = BeautifulSoup('<a href="/about/team">Team</a><a href="/about/board">Board</a>'
                             '<a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/contact/">Contact</a>'
                             '<div class="address-data"><p>Izomax HQ</p><p>Lagerveien 17,<br>4033 Stavanger</p></div>', "html.parser")
        self.assertEqual(len(_priority_links("https://izomax.com/", soup)), 4)
        self.assertEqual(_priority_links("https://izomax.com/", soup)[1], "https://izomax.com/contact/")
        self.assertIn("Lagerveien 17, 4033 Stavanger", _identity_text(soup))

    def test_car_business_name_is_not_a_business_sports_club(self):
        row = {"organisation_number": "936337473", "name": "BIL OG HJELP AS", "evidence": {"website": {"status": "available", "value": {"title": "BIL OG HJELP AS"}}}}
        self.assertTrue(assess_website_identity(row)["publishable"])

    def test_group_contact_page_listing_subsidiary_org_number_is_not_exact_homepage_identity(self):
        profile = {
            "organisation_number": "915637353",
            "name": "SKS PRODUKSJON AS",
            "evidence": {"website": evidence("website", "available", "company_site", "https://sks.no", value={
                "final_url": "https://sks.no/",
                "title": "Konsern - SKS - Forside",
                "main_text_excerpt": "SKS is a power group with multiple subsidiaries.",
                "pages": [{"title": "Contact", "main_text_excerpt": "SKS Produksjon AS organisation number 915 637 353"}],
            })},
        }
        assessment = assess_website_identity(profile)
        self.assertFalse(assessment["publishable"])
        self.assertNotEqual(assessment["score"], 1.0)

    def test_shared_identity_gate_quarantines_parent_social_links(self):
        website = evidence("website", "available", "company_site", "https://parent.test", value={
            "title": "Parent Group", "main_text_excerpt": "Parent Group portfolio",
            "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/parent"}],
        })
        profile = {"organisation_number": "923609016", "name": "Exact Subsidiary AS", "evidence": {}}
        result = apply_website_identity_gate(profile, website)
        self.assertFalse(result["assessment"]["publishable"])
        self.assertEqual(result["website"]["value"]["social_links"], [])
        self.assertEqual(result["quarantined_social_links"], 1)

    def test_exact_legal_name_is_publishable(self):
        row = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "evidence": {"website": {"status": "available", "value": {"title": "Norsk Fiskeeksport AS"}}}}
        self.assertTrue(assess_website_identity(row)["publishable"])

    def test_parent_brand_without_legal_name_is_quarantined(self):
        row = {"organisation_number": "988412406", "name": "Tevlingveien 23 Invest AS", "evidence": {"website": {"status": "available", "value": {"title": "Ragde Eiendom"}}}}
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_parked_domain_and_parent_company_sports_site_are_quarantined(self):
        parked = {"organisation_number": "996081001", "name": "Condalign AS", "evidence": {"website": {"status": "available", "value": {"title": "CondAlign.com is for sale | HugeDomains"}}}}
        sports = {"organisation_number": "996242692", "name": "Primulator B.I.L.", "evidence": {"website": {"status": "available", "value": {"title": "Primulator", "description": "Premium products for HoReCa"}}}}
        self.assertFalse(assess_website_identity(parked)["publishable"])
        self.assertFalse(assess_website_identity(sports)["publishable"])

    def test_hosting_placeholder_and_generic_link_page_are_quarantined(self):
        hosting = {"organisation_number": "917568278", "name": "HJELMEN AS", "evidence": {"website": {"status": "available", "value": {"title": "www.Hjelmen-as.no is parked at Miss Hosting Web Hosting", "main_text_excerpt": "Hjelmen " * 100}}}}
        links = {"organisation_number": "986606009", "name": "KOALA ANS", "evidence": {"website": {"status": "available", "value": {"title": "koala.no", "description": "Find the best information and most relevant links on all topics related to"}}}}
        self.assertFalse(assess_website_identity(hosting)["publishable"])
        self.assertFalse(assess_website_identity(links)["publishable"])

    def test_broader_umbrella_site_is_not_exact_when_name_only_appears_in_body(self):
        row = {
            "organisation_number": "976994027",
            "name": "AVALDSNES SOKN",
            "evidence": {"website": {"status": "available", "value": {
                "title": "Kirken i Karmøy",
                "final_url": "https://www.karmoykirken.no/",
                "main_text_excerpt": "Avaldsnes sokn is one of several parishes represented on this umbrella site.",
            }}},
        }
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_social_handle_requires_exact_entity_name_evidence(self):
        aon = {"name": "Aon Norway AS"}
        fish = {"name": "Norsk Fiskeeksport AS"}
        self.assertFalse(assess_social_identity(aon, {"platform": "linkedin", "url": "https://linkedin.com/company/aon"})["publishable"])
        self.assertTrue(assess_social_identity(fish, {"platform": "linkedin", "url": "https://linkedin.com/company/norsk-fiskeeksport"})["publishable"])


class VerifiedSiteSeedTests(unittest.TestCase):
    def test_verified_seed_is_applied_and_unknown_org_is_rejected(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles.jsonl"
            seeds = root / "seeds.json"
            output = root / "output.jsonl"
            report = root / "report.json"
            profiles.write_text(json.dumps({"organisation_number": "123456789", "name": "Example AS"}) + "\n")
            seeds.write_text(json.dumps([{
                "organisation_number": "123456789",
                "website": "https://example.no/",
                "proof_url": "https://source.example/proof",
                "proof": "Exact name and organisation number",
            }]))
            command = [
                sys.executable, str(ROOT / "scripts" / "apply_verified_site_seeds.py"),
                "--profiles", str(profiles), "--seeds", str(seeds),
                "--output", str(output), "--report", str(report),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            row = json.loads(output.read_text().strip())
            self.assertEqual(row["website"], "https://example.no/")
            self.assertEqual(row["website_seed_source"], "independently_verified_exact_entity")
            self.assertEqual(json.loads(report.read_text())["applied"], 1)

            seeds.write_text(json.dumps([{
                "organisation_number": "987654321",
                "website": "https://unknown.no/",
                "proof_url": "https://source.example/proof",
            }]))
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("unknown organisations", failed.stderr)


if __name__ == "__main__":
    unittest.main()
