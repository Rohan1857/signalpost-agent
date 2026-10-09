# Sources and evidence

## Official records

The runner joins records by organisation number.

| Data | Brreg endpoint |
| --- | --- |
| Registry snapshot | `https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv` |
| Live company record | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}` |
| Roles | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller` |
| Group structure | `https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}` |
| Subunits | `https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}` |
| Accounts | `https://data.brreg.no/regnskapsregisteret/regnskap/{org}` |

Accounts retain the reporting period, currency and company/consolidated scope. The latest company account takes priority when both scopes exist. Missing accounts don't mean zero revenue. Role normalization drops dates of birth.

The frozen CSV is the declared local data snapshot. Its hash and row identifier travel with registry evidence. Blank extra CSV cells produce warnings; nonempty unlabelled cells make the row a source error rather than silently shifting fields.

## Company websites

Discovery and ownership checks are separate. Registered URLs, email domains and guessed domains provide candidates. Publication requires the exact organisation number, a complete legal name or one of the tested legal/contact proofs in `identity.py`. Conflicting organisation numbers, parent-company matches and parked sites stop publication.

The crawler checks public addresses, robots rules and redirects. Some JavaScript shells get a bounded browser attempt. Evidence records distinguish rendered DOM hashes from HTTP response hashes.

Social links describe links on the company site, not verified platform accounts or follower counts. Dated posts require explicit publication dates. Company-owned text isn't independent news or sentiment.

## Vacancies and failures

NAV jobs need a matching employer organisation number or a registry-verified subunit number. Expired and inactive ads don't count. An ordinary careers page isn't a vacancy; a company-site job needs dated structured JobPosting data and the exact hiring legal name.

Blocked requests, source errors and uncertain matches remain separate outcomes. A bounded search with no verified result doesn't prove the company has no vacancies. [Connector notes](external-connectors.md) explain the optional feed.
