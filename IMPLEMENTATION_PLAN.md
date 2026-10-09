# Submission readiness plan

## Implemented audit fixes

- Exact organisation-number matching for jobs, including verified Brreg subunits, formatted-ID normalization, conflicting-ID rejection and expiry/status checks.
- Ordered legal-name website verification, conflicting-number rejection, ambiguous-site quarantine and exact social-handle verification.
- No career-page vacancy inference; only explicit, dated structured vacancies from verified company sites qualify as site job evidence.
- Field-level claims, retained normalized snapshots, original-response hashes, exact JSON spans, reporting periods and referential-integrity validation.
- Actual batch refresh, retained prior evidence and protection against false deletions caused by source failures.
- Worker-failure envelopes, input-derived company counts and explicit coverage/error reporting.
- Quarantined news/review discovery; opt-in bounded transient Brave discovery.

## Required before claiming qualification

1. Improve discovery using official public registry URLs and bounded independently fetched domain candidates. Personal Brave credentials are optional for local development, not an evaluator prerequisite; official Brave use requires confirmed evaluator access.
2. Audit the opt-in documented NAV feed and verified company-site vacancies. The feed supports NAV's public experimental token or a registered consumer token; official evaluator use of the chosen access mode remains unconfirmed. Current website search failures and partial feed coverage remain explicit.
3. Audit actual published websites, jobs and evidence on both batches. Broader recall must not come from weaker identity matching.
4. Local desktop/mobile prototype verification and current-profile regeneration passed; official UX scoring remains unmeasured.
5. Supply a public repository URL, final commit, contact details, source declarations and evaluator command.
6. Obtain Builderr's official run and score. 65/100 is not established by passing local tests, a fixture replay, an internal completeness score or a predicted score.

No competitor implementation or hidden benchmark outcome is assumed. The local report deliberately leaves the official score unmeasured.

## Verified evaluator constraints (8 October 2026)

Rechecked on 9 October against the participant brief, evaluation contract, agent playbook, learning harness and downloaded starter. The supplied frozen-universe archive matches both published hashes and contains 411,160 rows. All original smoke companies are members. The earlier registry-derived held-out set contains one nonmember (`970167935`); those measurements are engineering diagnostics, not a clean frozen-universe evaluation. Future local batches use `select_entry_batch.py --universe ... --exclude ...` against the verified archive. The starter's older 55-point external rubric and 80-point control-loop objective do not supersede current scoring version 2.

- Qualification is 65/100 on Builderr's official fresh batch: recall/coverage 50, precision/evidence 30, synthesis 12, UX 8. Even perfect non-recall scores require at least 15/50 recall points; this is arithmetic, not a prediction of our score.
- One terminal result per supplied company, explicit missing/blocked states, evidence dates and relevant reporting periods, idempotent refresh, retained prior evidence, pinned setup and one command are required.
- Official registry records, independently verified company-owned pages and permitted/licensed external sources are allowed. Unofficial restricted-platform connectors remain quarantined.
- Personal account credentials cannot be assumed for reproducible evaluation. Builderr explicitly offers model keys on request; no equivalent promise of Brave or NAV credentials is published.
- Declared external caches are allowed with evidence cutoffs and refresh checks; precomputed profiles are not ranked. A discovery cache would need fresh independent verification and cannot substitute for discovery on unseen companies. No new cache is used by this change.
- Source policy: https://builderr.ai/starter-briefs/signalpost-sources.md
- Evaluation contract: https://builderr.ai/docs/signalpost-evaluation-harness.md
- Challenge and credential rule: https://builderr.ai/challenges/signalpost

## Final handoff additions

Latest-company account selection was corrected after discovering oldest-first API arrays and premature truncation. Current tracked smoke shows 2025 company accounts for all 100 inputs. Exact registered-domain/contact-address and merchant legal-contact-policy identity routes were independently exercised on live positive cases. Current checks: 180 tests and 33 subtests passed. The user requested a context handoff and a stop after these checks; consult REVIEW_HANDOFF.md before further work. The 65-point objective is not complete.
