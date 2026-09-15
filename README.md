# Cruise fare and inventory panel

Daily/weekly collection of cruise pricing **and cabin availability** from public
booking engines, to support equity research on Norwegian Cruise Line Holdings
(NCLH).

The sell-side standard dataset tracks only the *minimum* advertised price per
cabin category, which conflates price with inventory mix: when cheap cabins sell
out, the minimum rises and reads as pricing strength when it is actually
depletion. This panel captures two things that series cannot:

1. **the full price distribution**, every cabin category, not the floor
2. **availability**, which no published series covers

Every design decision below protects those two properties.

## Status

| Build step | State |
|---|---|
| 1. robots.txt audit across all domains | done |
| 2. Schema, storage, logging | done |
| 3. Norwegian end-to-end, weekly-full, tested | **done — this is what ships now** |
| 4. Generalize to remaining lines | not started |
| 5. daily-marker tier | scaffolded, sailing list empty |
| 6. Analysis module | not started |

77 tests pass. A live smoke run collected 260 observations across 4 itineraries
and 41 sailings with zero errors and zero unmapped cabin labels.

## Quick start

```bash
pip install -r requirements.txt

# see what is in scope without fetching anything
python -m panel.collect --tier weekly-full --dry-run

# small live run
python -m panel.collect --tier weekly-full --limit-itineraries 5

# full weekly panel
python -m panel.collect --tier weekly-full
```

Both tiers are safe to re-run: observations upsert on
`(line, sailing_id, cabin_subcategory, market, scrape_date)`, and completed
itineraries are recorded in `run_progress`, so an interrupted run resumes where
it stopped rather than refetching.

## Layout

```
panel/
  schema.py        SQLite DDL
  storage.py       Store (upserts, run log, resume), RawArchive (gzipped, dated)
  normalize.py     cabin mapping, pppn, promo hashing, windows  <- most tests here
  http_client.py   polite GET client: robots gate, rate limit, backoff
  config.py        YAML loading
  collect.py       CLI (--tier)
  sources/ncl.py   Norwegian: URL building, parsing, collection loop
config/panel.yaml  lines, regions, date ranges, rate limits, cabin map
tests/             77 tests, run against archived real responses in fixtures/
data/panel.sqlite  the panel
data/raw/          every raw response, gzipped by line and date
```

## What NCL actually exposes

One request returns an entire itinerary's grid:

```
GET /api/vacations/sailings/{itineraryCode}
  -> pricingStateRooms[]: one row per (sail date x cabin category)
```

For the test itinerary that is 15 sail dates x 6 categories = 90 cells in a
single response, each with its own status and price vector. Search is used only
to enumerate itinerary codes.

Per cell: `combinedPrice`, `basePrice`, `xcatPrice`, `fasPrice`, `status`,
`currencyCode`, `hasSingleSupplement`, and structured `offerGroups[]`.

**Availability** is a real field, not an inference:

| Vendor `status` | `availability_status` | Meaning |
|---|---|---|
| `AVAILABLE` | `available` | open |
| `SOLO_GUEST_ONLY` | `limited` | depleted enough to be solo-occupancy only — a strong late-stage depletion marker |
| `SOLD_OUT` | `sold_out` | gone |
| anything else | `unknown` | logged, never guessed |

The verbatim vendor value is always kept in `availability_status_raw`, so a new
status value cannot be silently flattened.

`units_remaining` is always NULL for NCL — no inventory count is exposed
anywhere. Sold-out and solo-only cells also carry NULL prices, which is correct:
there is no purchasable fare, and NULL must not be read as zero.

## Decisions worth knowing

**Taxes are never folded into price.** `price_total` and `price_pppn` are
fare-only; `taxes_fees` is its own column. Enforced by test.

**`price_pppn` = `combinedPrice` / nights.** `combinedPrice` is the published
per-person double-occupancy fare, so `price_total` = `combinedPrice` x 2 and the
spec's `(price_total for 2 pax) / 2 / nights` is the same number. The vendor
field used is recorded per row in `price_basis`, so changing the basis later
does not silently rewrite history.

**MINISUITE maps to `balcony`, not `suite`.** NCL sells it as "Club Balcony
Suite"; it is physically a balcony stateroom with suite branding. Mapping it to
`suite` would inflate the suite tier by roughly the size of the balcony tier and
corrupt `peer_gap` against lines with no mini-suite product. This is a judgement
call, it is one line in `config/panel.yaml`, and re-running normalization over
`data/raw/` rebuilds the panel if you disagree.

**Cabin mapping is exact, never fuzzy.** An unrecognised label yields a NULL
`cabin_category`, keeps its raw label, and is recorded in `unmapped_labels` for a
human to resolve. `BALCONY_PLUS` will not silently become `balcony`.

**BAHAMAS folds into `Caribbean`.** Otherwise those sailings drop out entirely.

**The market actually served is recorded, not the one requested.** See below.

## Open TODOs

### 1. US egress — blocks the market requirement

The spec calls for US site, USD only. **This machine resolves to Toronto and is
served CAD.** All in-band workarounds were tested and all 15 failed:

- query params `currency`, `currencyCode`, `market`, `country`, `countryCode`, `locale`, `site`
- headers `Accept-Language: en-US`, `x-market`, `x-currency`, `x-locale`, `x-country`, `Referer`
- path prefixes `/us/en/` and `/en/us/` — both 404

Akamai EdgeScape resolves market from client IP **at the edge**, before origin
sees the request (`ak_country=CAN`, `ak_location=CA,ON,TORONTO`). No header or
parameter overrides it. Spoofing `X-Forwarded-For` would be circumventing a geo
control and is deliberately not implemented.

This matters beyond the currency label: **on the CAD market
`taxesAndFees.amount` is absent entirely**, so the "taxes captured separately"
rule cannot be satisfied from this egress. The US response does populate it
(`{"text": "Includes taxes, fees and port expenses", "amount": 146}`).

*Fix:* run the collector from a US VPS, or route through a US VPN. Confirmation
is one request — check `currencyCode == "USD"` and that `taxes_fees` is
non-NULL. Until then every row is stamped `market='CA'`, `currency='CAD'` so a
Canadian run cannot silently contaminate a USD panel.

### 2. Carnival — untested

Carnival has the most permissive robots.txt of any domain audited (only
`/CMS/SiteSearch/`, `/error/`, `/Errors/` and two logon query params disallowed)
and returned 200 to a plain client. It was never actually tested end to end. It
is the most likely second line to work and the natural next build step.

### 3. Other lines — dropped or degraded

From the robots/access audit:

| Line | Finding |
|---|---|
| Oceania | 403 to polite automated access (Akamai) — dropped |
| Regent | 403 to polite automated access (Akamai) — dropped |
| MSC | 401 on everything incl. robots.txt — dropped |
| Princess | robots.txt disallows exactly the pricing surface — dropped |
| Royal Caribbean | blocks headless browsers; SSR exposes only `lowestPrice`; cabin ladder reachable only via build-scoped Next.js server actions — floor price and promo tags only |
| Celebrity | same architecture as RC, untested |

Dropping Oceania and Regent means the panel speaks to **NCL-brand pricing, not
NCLH consolidated yield**. Both are small in capacity but disproportionate in
yield mix. Worth stating explicitly in any writeup built on this data.

### 4. daily-marker sailing list is empty

The tier works but `marker_itineraries` in `config/panel.yaml` is `[]`. Populate
it once the weekly panel has run and strata can be chosen: ~20-30 itineraries
weighted to Southern Europe and Caribbean, spanning all four standard categories,
plus whatever is open within +/- 14 days of 2026-11-04. With an empty list and
`marker_only: true` the tier enumerates the near-term window instead.

### 5. Analysis module not built

`cohort_index`, `depletion_rate`, `peer_gap`, `promo_diff`,
`earnings_window_compare` are all still to write. The schema supports them:
`ix_obs_cohort` covers `(line, region, sail_date, cabin_category)` and
`promo_hash` is ready for week-over-week diffing.

## Conduct

- robots.txt is fetched per host and **every** request path is checked before it
  is sent; a disallowed path raises rather than proceeding. Unreachable
  robots.txt fails closed.
- minimum 2s between requests to a host, honouring a longer `Crawl-delay` if
  declared; exponential backoff on 429/503 respecting `Retry-After`; one
  connection at a time; requests issued serially.
- honest, contactable `User-Agent`. No browser impersonation, no fingerprint
  spoofing, no CAPTCHA handling, no proxy rotation. Where a site blocks polite
  automated access it is dropped and the coverage gap is recorded above.
