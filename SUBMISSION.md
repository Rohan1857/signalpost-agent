# Version 1

Submitted to Builderr on 9 October 2026.

- Repository: https://github.com/Rohan1857/signalpost-agent
- Frozen commit: `5ae132ae2acad510a5d42f195c60ec6657d98dae`
- Contact: Rohan, rohan.185712@gmail.com

Later documentation edits on `main` don't change the submitted version. Check out the commit above to reproduce it.

## Setup and command

Requires Python 3.12+, uv and network access:

```sh
uv sync --locked
uv run playwright install chromium
```

On minimal Linux, use `uv run playwright install --with-deps chromium` for the second step.

Builderr supplies `batch.jsonl` and `registry.csv`:

```sh
uv run python scripts/run_competition_batch.py --organisations batch.jsonl --bulk registry.csv --profiles-output out/profiles.jsonl --output out/envelopes.jsonl --report out/run-report.json --run-id official-v1 --workers 10
```

The runner reads the supplied batch rather than a fixed company list. For refresh, add `--previous-profiles previous-profiles.jsonl` and choose new output paths. Source dates are retained, but the runner has no cutoff CLI flag.

## Checks

The committed [smoke report](out/smoke-report.json) covers 100 companies selected from the eligible universe with seed 20261012, excluding 409 previously researched IDs. All 100 envelopes passed validation with no dropped inputs or submission errors. It found seven verified websites and no verified jobs; all NAV searches were blocked. The run took 213 seconds with ten workers.

A clean clone installed the locked dependencies, passed 203 tests and 33 subtests, and completed another 100-company run in 211.8 seconds. That run also returned 100 valid envelopes. The [refresh fixture](out/refresh-demo.json) recovered both expected changes without false changes or duplicate events.

These are local checks. Official scoring and qualification remain pending. The saved [prototype](out/prototype.html) lets reviewers browse and compare the smoke profiles.

## APIs, cost and licences

No external model or personal credential is required. The command uses public Brreg APIs, company websites and NAV vacancy search. Brave, the experimental NAV feed and news/review experiments stay disabled. Committed profiles are sample outputs, not a discovery cache for evaluation.

Expected third-party API charges: $0 per default batch. Compute, hosting and bandwidth are separate; the official resource allowance hasn't been confirmed.

| Runtime dependency | Licence |
| --- | --- |
| beautifulsoup4 | MIT |
| extruct | BSD, as declared in package metadata |
| lxml | BSD-3-Clause |
| pydantic | MIT |
| pypdf | BSD-3-Clause |
| playwright | Apache-2.0 |
| tldextract | BSD-3-Clause |
| trafilatura | Apache-2.0 |

`uv.lock` pins dependency versions. Transitive dependencies keep their own licences. The repository has no standalone project licence file; this table doesn't grant rights over the supplied starter or third-party content.
