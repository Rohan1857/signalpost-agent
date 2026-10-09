# Optional connectors

The default evaluator command uses public registry APIs, company websites and NAV vacancy search. It needs no personal API key. The extra connectors below are off unless requested.

## Brave search

Set `BRAVE_SEARCH_API_KEY` in the process environment and add `--search-limit 100` to allow at most 100 discovery requests. Don't commit the key.

Search results only suggest websites. The runner discards provider snippets and independently fetches candidate pages before checking company identity. The report records gross API cost even if account credits cover it.

Builderr can't reproduce a run with an entrant's personal account credential, so Brave isn't enabled in version 1.

## NAV feed

NAV documents its feed at https://navikt.github.io/pam-stilling-feed/. Registered access uses `NAV_FEED_TOKEN` with `--nav-feed token`. The public experimental token can be tried with:

```sh
--nav-feed public --nav-feed-days 30 --nav-feed-pages 10 --nav-feed-details 30 --nav-feed-seconds 120
```

The runner fetches the feed once per batch, selects possible matches and checks employer numbers in vacancy details. A partial scan leaves unmatched companies ambiguous. It never establishes a total vacancy count.

The public token is for experiments; approval for official judging hasn't been confirmed. Feed access is disabled in the submitted command. Feed mode also rejects `--resume`; refresh requires fresh source fetches.

## Other experiments

Google News RSS and Fagfolkguiden connectors remain under rights review. `--experimental-connectors` can run them, but their results stay out of published claims. Standalone social and sentiment scripts are development tools, not part of the default evaluator run.
