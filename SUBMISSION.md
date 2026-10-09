# Signalpost Agent — version 1

Researches evaluator-supplied Norwegian organisation numbers, verifies exact-company identity, retains claim-level sources and dates, and produces terminal envelopes with synthesis and explicit unknowns. The frozen inspection UI supports desktop/mobile filtering, source inspection and comparison of up to three companies.

## Installation

Requires Python 3.12+, uv and network access. Install only from the pinned checkout:

```sh
uv sync --locked
uv run playwright install chromium
```

On a minimal Linux image, substitute `uv run playwright install --with-deps chromium` for the second command; it installs browser system dependencies and may require system package permissions.

## One evaluator command

Builderr supplies `batch.jsonl` and `registry.csv`. The registry parser accepts CSV or gzip content, and the input company count is inferred. No preselected company list is used for evaluation.

```sh
uv run python scripts/run_competition_batch.py --organisations batch.jsonl --bulk registry.csv --profiles-output out/profiles.jsonl --output out/envelopes.jsonl --report out/run-report.json --run-id official-v1 --workers 10
```

For refresh, append `--previous-profiles previous-profiles.jsonl` and use new output paths. Source snapshots and earlier evidence are retained; repeated identical snapshots are idempotent. The runner has no official-cutoff CLI flag; source retrieval/publication dates are retained for evaluator cutoff checks.

## Smoke evidence

- Input: `smoke-companies.jsonl`, 100 previously untested companies selected from the frozen eligible universe, seed 20261012, excluding 409 prior research IDs.
- Report: `out/smoke-report.json`, run `untouched-holdout-036`, 9 October 2026.
- Results: `out/smoke-envelopes.jsonl` and `out/smoke-profiles.jsonl`; 100/100 terminal envelopes, all nine validation checks passed, no silent drops or submission errors.
- Coverage: seven verified websites; zero verified job postings, with all legacy NAV requests explicitly blocked. Missing facts are not replaced with zero.
- Runtime: 213 seconds with ten workers; 1,192 requests, p95 2,014 ms. This is local operational evidence, not compliance with an undisclosed official resource allowance.
- Browser: one verified rendered website; at most two simultaneous isolated browser contexts.
- Deterministic refresh evidence: `out/refresh-demo.json`.
- Prototype: `out/prototype.html`, generated from the same smoke profiles. It is an inspection sample, not a background research service.
- Official score and 65/100 qualification: not measured.

## Models, APIs, licences and cost

No external model or entrant-owned API credential is required. The evaluator command makes zero Brave requests, leaves the experimental NAV feed disabled, and leaves Google News/Fagfolkguiden experiments disabled. No cached external profiles feed the official run; committed smoke results are demonstration artifacts only. The evaluator-provided frozen registry is the declared identity cache.

Default sources are public Brreg registry, roles, accounts, group and subunit endpoints; guarded company-owned websites; and NAV's public vacancy search endpoint, whose access errors remain explicit. Website acquisition checks public addresses, robots and redirects. Company content is attributed to its source, social links are company-site declarations rather than verified destination content, and financial claims retain company/consolidated scope and reporting period. See README for source restrictions and experimental connectors.

Expected default third-party API charges per official batch: **$0**. Compute, browser installation, bandwidth and hosting are excluded; no claim is made about their cost. Optional paid search is not part of this submission command.

Direct runtime dependencies and declared installed-package licences:

| Dependency | Licence |
| --- | --- |
| beautifulsoup4 | MIT |
| extruct | BSD (package metadata) |
| lxml | BSD-3-Clause |
| pydantic | MIT |
| pypdf | BSD-3-Clause |
| playwright | Apache-2.0 |
| tldextract | BSD-3-Clause |
| trafilatura | Apache-2.0 |

Exact dependency versions are in `uv.lock`; transitive dependencies retain their own licences. This repository has no standalone project licence file; this list describes dependencies and does not grant a new licence over the supplied starter or third-party source content.

## Verification

```sh
uv run --with pytest==9.1.1 pytest -q
uv run python scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output out/refresh-demo.json
```

Contact, public repository URL and exact frozen commit are supplied in the submission email.
