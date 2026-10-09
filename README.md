# Signalpost company research agent

Researches evaluator-supplied Norwegian organisation numbers using a frozen Brreg registry, live official records, and verified company websites. Qualification at 65/100 has **not** been measured; the local smoke test checks operation and evidence integrity, not the official score.

## Setup and verification

Requires Python 3.12+ and uv. Install the versions in the committed lockfile:

```powershell
uv sync --locked
uv run playwright install chromium
uv run --with pytest==9.1.1 pytest -q
uv run python scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output out/refresh-demo.json
```

On a minimal Linux image, use `uv run playwright install --with-deps chromium` to install browser system dependencies. Browser installation is a setup step; it does not use a paid API. Local checks may use an installed Chrome with the process environment variable `SIGNALPOST_BROWSER_CHANNEL=chrome`; the default uses Playwright's installed Chromium.

## Evaluator command

For local tests, select companies from the published frozen universe rather than inferring eligibility from a newer registry download:

```powershell
uv run python select_entry_batch.py --universe "D:\Downloads\signalpost-company-universe-2025.jsonl.gz" --count 100 --seed 20261009 --exclude smoke-companies.jsonl --output out/heldout-companies.jsonl
```

The supplied archive was verified on 9 October 2026: 411,160 rows; compressed SHA-256 `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384`; uncompressed SHA-256 `b82d6a3e7231d1759a958c282bc4366b80ec2fab8095053d8ed7fa9cd01bc838`. The selector does not automatically authenticate arbitrary input files. The Windows path above is for local development only.

The downloaded starter's `docs/external-footprint-loop.md` and `docs/external-connectors.md` describe an older 55-point external-footprint rubric; its control-loop document also targets 80 points and precomputed submissions. Those are historical development guidance. Use the current [participant brief](https://builderr.ai/starter-briefs/signalpost.md) and [evaluation contract](https://builderr.ai/docs/signalpost-evaluation-harness.md): 50 recall, 30 precision/evidence, 12 synthesis and 8 UX; qualification requires 65/100 on an official fresh batch. Local proxy scores and starter precision/recall targets are not additional qualification thresholds.

Builderr supplies the company batch and frozen registry snapshot. The company count is inferred from the input; `--expected-count` is an optional assertion.

```powershell
uv run python scripts/run_competition_batch.py --organisations <batch.jsonl> --bulk <registry.csv> --profiles-output out/profiles.jsonl --output out/envelopes.jsonl --report out/run-report.json --run-id official-run --workers 8
```

To rerun the saved smoke input, replace the batch and registry paths with `smoke-companies.jsonl` and `brreg-enheter.csv`. The committed report uses ten workers; live source content and availability can change between runs. The large registry CSV is deliberately excluded from Git; the evaluator supplies it.

## Refresh

Pass `--previous-profiles <previous.jsonl>` to the same command, with separate output files. Sources are fetched again, verified changes are emitted, and earlier evidence is retained in `profile.evidence_history`. Failed or blocked lookups do not become real-world deletions. Replaying identical snapshots does not create changes or duplicate evidence. `--resume` only resumes interrupted work; it cannot be combined with live refresh.

## Output and evidence

Frozen CSV rows with unlabelled empty trailing cells retain their named fields and report parsing warnings. Nonempty unlabelled cells quarantine the registry row as a source error, preserving diagnostics without publishing registry claims from it.

One envelope is emitted per supplied company, including worker failures and companies absent from the registry snapshot. Availability uses `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, and `failed`. JSON claims reference specific extraction paths and exact serialized values in retained normalized source snapshots. Original response hashes and normalized snapshot hashes are distinct. Financial claims include the reporting period. Account normalization retains all returned filings and sorts by period before presentation. Synthesis, screening and the UI select the latest company (`SELSKAP`) account when available; consolidated (`KONSERN`) figures are labeled separately and are only the fallback when no company account is returned. Validation rejects broken references, corrupted spans/hashes, mismatched company numbers, invalid states and empty claim arrays. Original raw response bytes are not archived; normalized snapshots retain the published values without restoring discarded personal birth dates.

Website publication requires a complete ordered legal name, homepage organisation-number evidence, or same-host sales terms explicitly naming the exact legal seller and its organisation number. A further route accepts an exact homepage title fragment without the legal suffix only when at least two distinctive name tokens, the street, postcode and phone all match the exact live registry entity. Prefix matches with extra company modifiers are rejected. Contact corroboration retains both source hashes and the registry date; matching group contact details without the homepage name is insufficient. Another route requires the exact live-registered root domain, an exact homepage brand fragment retaining all non-legal modifiers, and the full registered street and postcode captured on a same-host contact page. Its proof retains registry, homepage and contact-page source hashes. Contact pages with conflicting labelled organisation numbers are rejected. Legal pages remain first in the four-page budget; contact pages precede privacy and team pages, and semantic address blocks are retained. Seller proof retains the page URL, original content hash and exact declaration. A same-host `/policies/contact-information` disclosure can also prove the operator when its trade name equals the storefront hostname, its physical-address line names the exact legal entity, and all labelled organisation numbers equal that entity. Conflicting homepage IDs still reject the site; a subsidiary listed on a group contact page is insufficient. Uncertain websites remain quarantined. NAV matches require an organisation number belonging to the company or a Brreg-verified subunit; names alone do not establish a match. A generic careers page is never counted as a vacancy. A company-site vacancy requires an explicit schema.org JobPosting with the exact hiring legal name, content, posting date and unexpired deadline on a verified website.

Default website discovery also tries the domain of a publicly registered contact email (excluding common consumer email services) and bounded `.no`/`.com` name candidates. At most two additional hints cover three-to-five-word initial acronyms, brands ending in Media, and named FUS kindergarten location paths. These precede secondary full-name domain variants; the complete discovery list contains at most eight unique candidates, including two email hints. A guessed path, acronym or email domain never proves ownership: the same exact-entity gate applies to every fetched candidate. Registered activity, purpose and contact details are included in synthesis with the frozen registry's own source and snapshot date.

An unresolved website candidate classified as an incomplete JavaScript shell can receive one browser attempt per company, with registered/subunit URLs considered before discovered domain candidates. Search-provider results remain static-only. Static conflicting organisation numbers, parked pages and parent sites rejected for sports-club entities cannot be overridden. The renderer uses at most two concurrent isolated headless browser processes, a twenty-second acquisition budget and forty request attempts including document robots checks. Images, media and fonts are blocked; public-address, document robots and candidate-domain checks remain enforced. Missing browser runtime, timeout or blocked acquisition stays an explicit browser source error, without publishing an unverified website.

Rendered acquisitions retain the serialized DOM capture and its SHA256, identify it as `rendered_dom`, and separately record the original browser HTTP-document URL and body SHA256. These digests describe different artifacts. Envelope validation verifies retained DOM bytes against the published digest. Up to eight of the forty request attempts are reserved for four robots/page pairs using the existing same-site legal/contact priorities exposed by the DOM. These secondary pages use ordinary HTTP acquisition, retain their own response hashes and capture kind, and share the twenty-second acquisition budget. Rendered pages still pass the existing exact-company gate before activity, links or structured vacancies can be published. Browser byte metrics cover the captured HTTP document, DOM and secondary-page responses, rather than all asset/network traffic; its latency metric is the complete acquisition attempt. The browser component's wider population coverage and resource cost still require measurement.

The email route considers at most two hints, first from the exact live registry entity and then from the frozen bulk record. Consumer-mail hosts and invalid addresses are excluded. Retaining live phone/email fields adds no API request beyond the existing registry fetch.

Social observations report links declared by the verified company site, not verified destination-platform content. Each link retains its actual source page, page-response hash and observed attribute or organisation `sameAs` value. No observation is emitted from an aggregate link list without a captured origin. Handles require the full normalized legal-name sequence, optionally followed by `official`, `com` or `no`; parent, group and unrelated-name variants remain rejected. Platform pages, follower counts and posts are not fetched by this route.

After website identity passes, dated activity discovery follows at most two additional same-site pages, with an 8-second request timeout and 2 MB response cap per page. Archive links and explicit article cards generate candidates. Publication requires an article headline and explicit `datePublished` or `article:published_time`; modification dates, undated archive titles and future dates do not become posts. If no dated HTML article is captured and the crawl budget permits, an explicitly advertised same-host RSS feed supplies at most two publication candidates with item title, same-host publication link and explicit `pubDate`. Comment feeds, comment links, foreign-host items, undated items and XML document types are rejected. RSS observations cite the feed URL and original feed-response hash, retain the original date text, and separately identify the publication URL. Each observation retains retrieval and publication dates plus title/date evidence. These are company-owned publications; embedded event statements are not independently verified facts or sentiment. Old publication dates are preserved as old dates, with no claim that they earn recent-activity recall. HTML and RSS together add at most four ordinary requests per verified site, including robots checks; redirects outside the verified homepage host are rejected.

Synthesis includes those sourced titles and dates. Refresh labels an older post found for the first time `publication_discovered`; it uses `new_publication` only when the stated publication date follows the previous website observation. Identical snapshots produce no new event, and a bounded crawl losing a post never proves removal.

## Free search access

Brave Search offers $5 monthly credit, about 1,000 Search requests at the published $5/1,000 rate. A card is required. Use a $0 prepaid balance, disable auto-reload and set a usage limit before testing. Configure `BRAVE_SEARCH_API_KEY` in the process environment and add `--search-limit 100` to cap discovery at 100 requests. Search is disabled by default; keys never enter URLs or artifacts. Search results remain transient: only independently fetched, identity-verified website evidence is retained. Gross request cost is reported even when account credits offset billing. Attribution: Brave Search API, https://brave.com/search/api/ . Provider terms: https://api-dashboard.search.brave.com/documentation/resources/help-feedback .

Brave is optional local discovery, not a prerequisite for submission. Builderr cannot reproduce evaluation using an entrant's personal account credential; official Brave use requires confirmed evaluator access. The default evaluator command makes no Brave requests, even when a key exists in the environment. No Brave/NAV scoring credentials have been confirmed. Evaluator rules: https://builderr.ai/challenges/signalpost .

## Jobs access and source governance

The current NAV website search endpoint can return 403/429. These are reported as blocked, never as zero vacancies. NAV's documented vacancy feed is free: https://navikt.github.io/pam-stilling-feed/ . It provides a public token explicitly for experiments; registered consumers can request their own token from nav.team.arbeidsplassen@nav.no. Company-site structured vacancies provide an independent fallback.

An HTTP-200 legal-name search without verified matches also remains `ambiguous`: a bounded name query cannot establish that a company has zero vacancies. Positive NAV search observations retain the actual query-response hash and URL rather than pretending the public vacancy detail page was fetched.

The documented feed is opt-in. Add `--nav-feed public --nav-feed-days 30 --nav-feed-pages 10 --nav-feed-details 30 --nav-feed-seconds 120` for a bounded experiment using NAV's rotating public token. Alternatively, `--nav-feed token` reads `NAV_FEED_TOKEN` from the environment. Registered personal credentials are not assumed for evaluator access; public experimental access is not a claim of production permission or organiser approval.

The feed is scanned once per batch. Registered names supply candidate hints; exact normalized names receive priority, followed by complete registered names of at least two tokens with an advertised branch suffix. These hints only select details to fetch: latest vacancy details must confirm a parent or Brreg-verified subunit organisation number. Inactive and expired ads are excluded, original detail-response hashes retained, and tokens never enter artifacts. Scans have page, detail, byte and time limits. No match remains `ambiguous`, because older vacancies, alternate brands and unscanned pages remain unknown. Published job counts mean observed verified vacancies, not the total company vacancy count. Shared feed requests are counted once in the batch report. Feed mode rejects `--resume` so old active ads cannot silently survive a new run. Use fresh fetches and `--previous-profiles` for refresh.

Google News RSS titles and Fagfolkguiden aggregate ratings remain experiments with `review_required` rights. They are disabled by default, and `--experimental-connectors` does not permit their publication or score them. Missing source rights are not silently promoted to approval. Company websites use public-URL/redirect guards and robots checks. Public Brreg data is the identity anchor; company-owned descriptions are attributed promotional claims, not independent sentiment. No external LLM or user-account credentials are required for the default run. Default third-party API cost is $0; hosting/network costs are excluded.

## Prototype

```powershell
uv run python scripts/build_prototype.py --input out/smoke-profiles.jsonl --output out/prototype.html
```

Open `out/prototype.html` locally. It is a frozen inspection UI with filters, pinned companies, side-by-side comparison of up to three pins, saved views, JSON export and source links; it does not perform background research or prove official UX points.

## Submission status

See [SUBMISSION.md](SUBMISSION.md) for version-1 installation, the evaluator command, smoke evidence, APIs, dependency licences and expected third-party cost. The submission email identifies the public repository, frozen commit and contact details. Only Builderr's verified reference collection and official run can establish the 65/100 threshold. The report exposes actual coverage, failures, request latency and company runtime rather than inventing a competition score.
