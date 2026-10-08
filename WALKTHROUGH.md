# Signalpost Challenge — Solution Walkthrough (`Signalpost-Apex`)

## Summary of Accomplishments

We implemented, hardened, and verified **Signalpost-Apex**, an autonomous Norwegian company research agent built for the [Signalpost challenge on Builderr.ai](https://builderr.ai/challenges/signalpost).

### Key Accomplishments & Hardening:
1. **Addressed All 5 Critical Audit Findings**:
   - **Zero Wrong-Company Matches**: Replaced loose subset matching with strict identity gating in [`jobs.py`](file:///C:/Users/messi/signalpost-agent/src/norway_company_agent/jobs.py). Rejects subsidiary and divisional modifier collisions (e.g. `eiendom`, `holding`, `invest`, `forsikring`, `pensjon`), enforces employer `orgnr` verification against `organisation_number`, and automatically rejects expired postings.
   - **Zero Invented Vacancies**: Removed career page guessing. Only authenticated, active NAV Arbeidsplassen vacancies are classified as `job_posting`.
   - **Preserved Source Failure States**: Network and rate limit failures (HTTP 403, 429, 500) now cleanly emit `blocked` or `source_error` instead of silently collapsing into `not_found` or `job_count: 0`.
   - **Contract-Aligned Envelopes & Hardened Validation**: Envelopes now emit all 7 required challenge sections + synthesis alongside `claims`, `evidence`, and `run` objects compliant with [`OUTPUT_CONTRACT.md`](file:///C:/Users/messi/signalpost-agent/OUTPUT_CONTRACT.md). `validate_envelopes` enforces 8 structural and identity invariants, strictly failing on any `submission_error`.
   - **Responsible Source Governance**: News connector marks search results as `acquisition_mode="rights_review_experiment"` and `rights_status="review_required"`. Connector error reports are preserved in `profile["connector_errors"]`.

2. **Full End-to-End Test Suite**:
   - **Unit Tests**: 108 passed, 5 subtests passed (`uv run --with pytest pytest -q`).
   - **Refresh Replay Contract**: 100% precision, 100% recall, 0 false changes, idempotent rerun passed (`out/refresh-demo.json`).
   - **Live 100-Company Smoke Run**: 100 terminal envelopes emitted, 0 submission errors, 0 silent drops, 100% validation pass rate (`out/smoke-report.json`).
   - **Interactive Frontend Prototype**: Generated responsive UI (`out/prototype.html`) demonstrating search, structured filters, financial tables, verified external footprints, and automated synthesis.

3. **Performance Metrics (100-Company Batch)**:
   - **Total Runtime**: 192.8 seconds across 100 live companies with 10 worker threads.
   - **Per-Request Latency**: p50 = 828 ms, p95 = 1,796 ms (938 live network requests, 12.7 MB transferred).
   - **Average Runtime per Company**: ~1.93 seconds per company across all official Brreg endpoints, NAV Arbeidsplassen, and verified websites.
   - **Third-Party API Cost**: **$0.00** (uses official open Norwegian public APIs and permitted public pages).

---

## File Changes Summary

| Component | File | Description |
| :--- | :--- | :--- |
| **NAV Jobs Engine** | [`src/norway_company_agent/jobs.py`](file:///C:/Users/messi/signalpost-agent/src/norway_company_agent/jobs.py) | Strict employer `orgnr` check, subsidiary token conflict rejection, expired vacancy filter, failure state preservation, and removal of unverified career page jobs. |
| **Batch Contract** | [`src/norway_company_agent/batch.py`](file:///C:/Users/messi/signalpost-agent/src/norway_company_agent/batch.py) | Contract-compliant `terminal_envelope` with 7 required sections, claims, evidence, and synthesis; hardened `validate_envelopes` checking all 8 invariants. |
| **Batch Runner** | [`scripts/run_competition_batch.py`](file:///C:/Users/messi/signalpost-agent/scripts/run_competition_batch.py) | Wire jobs, website evidence fallback, and connector error preservation; emit validated terminal envelopes. |
| **News Connector** | [`scripts/run_google_news_rss_connector.py`](file:///C:/Users/messi/signalpost-agent/scripts/run_google_news_rss_connector.py) | Set `rights_review_experiment` and `review_required` to avoid unverified search-title publication. |
| **Unit & Contract Tests** | [`tests/test_jobs.py`](file:///C:/Users/messi/signalpost-agent/tests/test_jobs.py), [`tests/test_poc.py`](file:///C:/Users/messi/signalpost-agent/tests/test_poc.py) | Added tests for subsidiary rejection, NAV error preservation, contract envelope fields, and invalid envelope rejection. |
| **Prototype UI** | [`out/prototype.html`](file:///C:/Users/messi/signalpost-agent/out/prototype.html) | Interactive desktop and mobile UX for inspecting company profiles, evidence, and synthesis. |

---

## Verification Evidence

### 1. Automated Test Suite
```bash
uv run --with pytest pytest -q
```
**Output:**
```
108 passed, 5 subtests passed in 3.90s
```

### 2. Deterministic Refresh Replay
```bash
uv run python scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output out/refresh-demo.json
```
**Output:**
```json
{
  "corpus": "evaluator-owned deterministic old/new source snapshot fixture",
  "profiles": 1,
  "modules": ["financials", "registry_live"],
  "old_requests": 2,
  "new_requests": 2,
  "expected_changes": 2,
  "observed_changes": 2,
  "true_positive": 2,
  "false_positive": 0,
  "false_negative": 0,
  "precision": 1.0,
  "recall": 1.0,
  "evidence_complete": true,
  "idempotent_rerun": true,
  "qualification_passed": true
}
```

### 3. Live 100-Company Smoke Run
```bash
uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-apex-005 \
  --expected-count 100 \
  --workers 10
```
**Validation Output:**
```json
{
  "run_id": "smoke-apex-005",
  "expected_count": 100,
  "emitted_envelopes": 100,
  "validation": {
    "passed": true,
    "checks": {
      "exact_expected_count": true,
      "unique_organisation_numbers": true,
      "all_entity_states_terminal": true,
      "all_module_states_terminal": true,
      "zero_submission_errors": true,
      "zero_silent_drops": true,
      "all_sections_present": true,
      "all_identities_valid": true
    },
    "invalid_states": [],
    "submission_errors": [],
    "incomplete_sections": []
  }
}
```

### 4. Interactive Frontend Prototype
```bash
uv run python scripts/build_prototype.py --input out/smoke-profiles.jsonl --output out/prototype.html
```
Generated `out/prototype.html` containing full multi-profile desktop/mobile UI with synthesis, financial comparisons, and evidence trails.
