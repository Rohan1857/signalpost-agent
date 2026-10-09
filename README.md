# Signalpost

Company research for Norwegian businesses, built for the [Signalpost challenge](https://builderr.ai/challenges/signalpost).

Give it a batch of organisation numbers and a Brønnøysund registry snapshot. It fetches official records, looks for company websites and collects facts with source links and dates. The output includes financials, roles, locations, dated activity and jobs where the sources allow it. If a source is blocked or a company match is uncertain, the result says so.

## Run it

You'll need Python 3.12+, [uv](https://docs.astral.sh/uv/) and network access.

```sh
uv sync --locked
uv run playwright install chromium
```

On Linux, use `uv run playwright install --with-deps chromium` if browser system packages are missing.

Put the company batch in `batch.jsonl` and the registry snapshot in `registry.csv`, then run:

```sh
uv run python scripts/run_competition_batch.py --organisations batch.jsonl --bulk registry.csv --profiles-output out/profiles.jsonl --output out/envelopes.jsonl --report out/run-report.json --run-id research-001 --workers 10
```

The batch can be JSON, JSONL or a text list of organisation numbers. The registry reader handles plain CSV and gzip content. Builderr supplies both inputs for judging; the large registry download isn't in this repo.

Each input gets one result, including companies with no usable external information. `out/envelopes.jsonl` holds the results and their evidence; `out/profiles.jsonl` holds the collected records. The report lists validation failures, coverage and request timings.

For a local run with the included 100-company sample, use `smoke-companies.jsonl` as the batch and your own registry download as the bulk input.

## Browse the results

Open [out/prototype.html](out/prototype.html) in a browser. You can search, filter, pin up to three companies for comparison and follow the source links. It shows the saved smoke run; it doesn't fetch new data.

To build it from another run:

```sh
uv run python scripts/build_prototype.py --input out/profiles.jsonl --output out/prototype.html
```

## Where the data comes from

Brønnøysund provides the official company records. Website discovery starts with registered URLs and email domains, then tries a small set of name-based candidates. A candidate still needs evidence that it belongs to the company before its content appears in a profile. Playwright handles some sites that need JavaScript.

Financial records keep their reporting period and distinguish company accounts from consolidated accounts. Social links mean the company website linked to an account; they don't establish ownership of the destination. Company posts keep their original publication dates.

[Source notes](docs/norway-sources.md) explain the endpoints and evidence rules. [Optional connectors](docs/external-connectors.md) covers Brave and NAV feed access. The default command needs no API key and incurs no third-party API charges. Compute and bandwidth costs are separate.

## Refresh and tests

Add `--previous-profiles previous-profiles.jsonl` to compare a new run with an earlier one, using different output paths. The runner keeps earlier evidence and reports supported changes. A failed request doesn't mean a fact disappeared. Use `--resume` only to finish an interrupted run; it can't be combined with refresh.

```sh
uv run --with pytest==9.1.1 pytest -q
uv run python scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output out/refresh-demo.json
```

## Current results

The saved [100-company smoke report](out/smoke-report.json) from 9 October returned 100 valid envelopes with no dropped companies. It found seven verified websites and no verified vacancies; NAV blocked all searches in that run. Runtime was about 213 seconds with ten workers.

A clean checkout passed 203 tests and 33 subtests. These checks establish that the code runs and the outputs pass validation. Builderr hasn't returned an official score yet.

[Version 1 submission details](SUBMISSION.md) include the frozen commit, evaluator command and dependency licences.
