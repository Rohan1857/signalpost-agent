# Signalpost Autonomous Research Agent (`Signalpost-Apex`)

Autonomous Norwegian company research agent built for the [Signalpost Challenge](https://builderr.ai/challenges/signalpost).

## Architecture Highlights

1. **Identity Anchor**: Anchors entity identity in official Brønnøysundregistrene (Brreg) bulk and live records.
2. **Official Financials & Roles**: Collects Regnskapsregisteret accounts, governance roles, subunits, and parent-subsidiary hierarchies.
3. **NAV Arbeidsplassen Open Jobs Engine**: Official Norwegian public employment platform integration (`arbeidsplassen.nav.no`) querying exact-entity vacancies with strict token gates and NLOD open-data compliance.
4. **Verified Website & Footprint**: Crawls verified company sites for management, locations, career pages, contacts, and social handles (LinkedIn, Facebook, Instagram, YouTube) with deterministic exact-entity verification (`identity.py`).
5. **Evaluation Contract Compliance**: Emits exactly one terminal JSONL envelope per input company with explicit availability states (`complete`, `not_found`, `not_applicable`, `blocked_robots`, `source_error`), content SHA-256 hashes, zero silent drops, and idempotent replay.

## Setup & Verification

Requires Python 3.12+ and `uv`.

```bash
# 1. Install dependencies
uv sync

# 2. Run unit & contract test suite (106 tests)
uv run --with pytest pytest -q

# 3. Verify deterministic refresh replay
uv run python scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

## Evaluator Run Command (Official Batch)

Run the agent on any supplied company batch with one command:

```bash
uv run python scripts/run_competition_batch.py \
  --organisations <path_to_organisations.jsonl> \
  --bulk <path_to_brreg_enheter.csv> \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id official-run \
  --expected-count <count> \
  --workers 8
```

## 100-Company Smoke Test

A 100-company verified smoke test is included in `out/smoke-report.json` and `out/smoke-envelopes.jsonl`.
- **Emitted Envelopes**: 100 / 100 (100% terminal)
- **Silent Drops**: 0
- **Validation**: Passed (100% contract compliance)
- **Third-Party API Cost**: $0.00 (Powered by official Norwegian open public APIs)
