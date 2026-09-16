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
| 3. Norwegian end-to-end, weekly-full, tested | done |
| 4. Generalize to remaining lines | **Carnival done; RC/Celebrity blocked, see gaps** |
| 5. daily-marker tier | **done; 26 markers selected from the near-term universe** |
| 6. Analysis module | **done; 5 spec functions + availability snapshot** |

193 tests pass. The 2026-09-15 weekly-full run on GitHub Actions collected 3,475
NCL and 1,276 Carnival observations, 100% USD / market=US, zero unmapped cabin
labels and zero unmapped regions. (That Carnival figure is one search page per
destination -- see gaps.)

## Quick start

```bash
pip install -r requirements.txt

# see what is in scope without fetching anything
python -m panel.collect --tier weekly-full --dry-run

# small live runs
python -m panel.collect --tier weekly-full --line ncl      --limit-itineraries 5
python -m panel.collect --tier weekly-full --line carnival --limit-pages 2

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
  export.py        JSONL export / rebuild -- the durable artifact
  analysis.py      the 5 spec analyses + availability snapshot, each carrying
                   the evidentiary basis it rests on (see below)
  report.py        runs the whole suite and writes one XLSX per collection;
                   every scheduled run ends here
  sources/ncl.py   Norwegian: URL building, parsing, collection loop
  sources/carnival.py      Carnival: paged search, parsing, collection loop
  sources/capabilities.py  what each source actually resolves (see below)
config/panel.yaml  lines, regions, date ranges, rate limits, cabin map
scripts/
  verify_us_market.py   gate: confirm the egress resolves USD before collecting
  probe_sail_dates.py   build data/sail_dates.json, the marker sampling frame
  pick_markers.py       choose daily-marker itineraries from that frame
  backfill_packages.py  classify pre-v4 rows as cruise-only vs land+cruise
tests/             296 tests, run against archived real responses in fixtures/
reports/           one workbook per collection, committed with the JSONL it was
                   computed from. Derived, regenerable, and never rewritten
                   silently -- see REGENERATIONS.jsonl
data/panel.sqlite  the panel (derived; rebuild with `panel.export rebuild`)
data/sail_dates.json  itinerary calendar: dates, region, ship, categories,
                   product type. Committed -- it makes marker selection
                   reproducible without re-probing.
data/raw/          every raw response, gzipped by line and date
```

## Dated files are immutable, and the code enforces it

`data/observations/YYYY/MM/<date>__<tier>__<line>.jsonl.gz` records what was
observed that day. A normal export may **add** a dated file; it may not change
one. If the rows for an existing (date, tier, line) would produce different
content, `panel.export export` prints what changed, exits 2, and writes nothing
at all -- a refusal never leaves a half-written export behind.

This is enforced because it was once violated. `export --tier weekly-full`
selected every (scrape_date, tier, line) group in the database and overwrote
each path, so correcting rows locally and re-exporting silently rewrote
2026-09-15. The only trace was an unexplained binary diff.

Correcting history is now an explicit, named, logged action:

```bash
python -m panel.export export --amend --reason "what changed and why"
```

`--amend` without a non-blank `--reason` is rejected. An amended write appends
to a plain-text ledger committed beside the data:

    data/observations/CORRECTIONS.jsonl

Each entry records the UTC timestamp, the reason, the tool, and per file: the
row count before and after, the sha256 before and after, and which fields
changed in how many rows. Someone hitting a binary diff in `git log` can read
that commit's entry and learn what was corrected and why.

Two supporting properties:

- **Deterministic output.** gzip is written with `mtime=0`, so identical rows
  produce identical bytes. Re-exporting unchanged data is a no-op in git rather
  than a spurious diff, which is what makes "did this file change?" a
  meaningful question at all.
- **Scheduled runs can never amend.** `collect.yml` calls `export` without
  `--amend`, so a cron run that would rewrite a past day fails the job instead.

The promo index is deliberately exempt: `promos.jsonl.gz` is an index whose
`last_seen`/`n_seen` counters are meant to move. What is *not* exempt is a
promo **body** changing under an existing hash, which would mean the same offer
id now says something different -- that still requires `--amend`.

## Evidentiary basis: the two tiers are not one sample

Every analysis returns a `Result` carrying the `Basis` it was computed on, and
`Result.basis.label()` prints it. This is not decoration -- the tiers cover
different populations:

| | weekly-full | daily-marker |
|---|---|---|
| sail window | Oct 2026 - Aug 2027 | Oct - Dec 2026 |
| breadth | every configured region and line | ~26 curated itineraries |
| cadence | weekly | daily |
| cross-line? | yes: Caribbean, Southern Europe, Bermuda | **Caribbean only** |

The windows overlap by design: weekly-full is the broad snapshot, daily-marker
re-reads a curated slice of its near-term end every day. They are still never
pooled, because breadth and cadence differ -- a region can carry hundreds of
weekly rows and a handful of daily ones, so a pooled "change" would mostly
measure which itineraries the weekly sweep happened to add.

Measured from the near-term universe on 2026-09-15: Caribbean has 104 eligible
itineraries (85 NCL / 19 Carnival); Southern Europe has 15, **all NCL**, all
departing October to early November, **0 in December and 0 on Carnival**;
Alaska has 0. The Mediterranean season ends and the ships reposition to the
Caribbean. So the daily tier cannot evidence Southern Europe pricing against a
peer, and its Southern Europe rows are a single-line October series.

Consequently:

- no function pools tiers; `tier` is a required argument everywhere
- `combine_bases()` raises on mixed tiers unless `allow_mixed_basis=True`, which
  stamps a `MIXED EVIDENTIARY BASIS` caveat onto the output
- regions carrying only one line are named in the basis as not peer-comparable

## Land+cruise packages are a different product

NCL sells "cruisetours" (Denali, London and Reykjavik land tours) at a package
price while stamping the row with the **cruise segment only** -- a Denali
itinerary carries `duration {"itinerary": 14, "cruising": 7}`. Dividing that
package fare by 7 cruise nights reads roughly twice the true nightly cruise
rate, and puts a bundled land tour up against a peer's ship-only fare.

Left unmarked this inflated NCL's price level by region: before the fix, NCL
Alaska balcony showed a median $712 pppn against Carnival's $228, which was
mostly the Denali cruisetours. Schema v4 captures `is_package` and
`itinerary_nights`; cross-line analysis defaults to `product="cruise_only"` and
reports how many rows it excluded and how many it could not classify. **The
price itself is never rewritten** -- prorating would fabricate a cruise-only
fare NCL never published.

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

## What Carnival exposes

One paged endpoint, robots-allowed, no crawl-delay declared:

```
GET /cruisesearch/api/search?pagesize=&pagenumber=&numadults=2&dest=
  -> results.itineraries[].sailings[].rooms.{interior,oceanview,balcony,suite}
```

The grid is nested inside each itinerary; `results.sailings` is always null and
is a red herring. One page of 20 itineraries returns hundreds of
(sailing x cabin) cells, so the whole fleet is roughly 40 requests.

Carnival resolves **finer than NCL**. Each cell carries `categoryCode`
(25 distinct observed: `8A`, `GS`, `6K`, `BL`, `JS`…) and `rateCode`
(8 distinct: `OB7`, `PSV`, `OTR`…). Those populate `vendor_category_code` and
`rate_code`, which stay NULL for NCL.

### Three traps, each covered by a test

**Sold-out cells report `price=0`, not null**, with `categoryCode`,
`priceCurrency` and `taxesAndFees` all null. Writing that through would put a
stream of $0 fares into the panel and crater every distribution statistic.
Sold-out cells are stored with NULL prices, and a test asserts no row ever has
`availability_status='sold_out'` together with a non-null price. A second test
guards that the fixture still contains sold-out cells, so the first cannot pass
vacuously.

**The USD result comes from a cache we do not control.** `locality` is echoed
back as `"1"` whatever we send (`locality=3&currency=GBP` still returns USD),
and `source` reads `"From Redis Cache"`. So we get US pricing, but not because
we asked. Every priced row is asserted to be USD; a mismatch raises
`CurrencyMismatch`, which is recorded in `collection_log`, printed as
`!! CURRENCY MISMATCH`, and makes the run exit non-zero. It is never dropped
and never converted. Fault injection confirms it fires: flipping 163 cells to
CAD raised on the first one.

**`cabin_subcategory` holds the meta code, not `categoryCode`.** `categoryCode`
goes null on sold-out cells, so it cannot carry the natural key. The stable
per-sailing label (`IS`/`OS`/`OB`/`SU`) is the key; the finer code lives in
`vendor_category_code`.

### Region mapping: regionCode, not dest, not port

Carnival's `dest` filter collapses all of Europe into a single `E`, which is
useless for a thesis concentrated on Southern Europe. `regionCode` is far finer
and is authoritative:

| regionCode | Region |
|---|---|
| `ME` `GI` `CG` `IB` `EC` | Southern Europe |
| `EN` `ES` `BI` | Northern Europe |
| `CE` `CW` `CS` `BH` | Caribbean |
| `BM` / `GL` | Bermuda / Alaska |

**Port is only a fallback, and London is deliberately excluded from it.** LON is
the embark port for Northern itineraries (`EN`, `ES`, `BI`) *and* for Iberian
ones (`IB`, `EC`), so a port-based guess would misclassify Southern Europe —
the one region the thesis depends on. The live run confirms this matters:
`EU3` ("10-Day Spain, Portugal & France from London") and `JU2` ("11-Day
Eclipse, Spain, Portugal & France") both sail from London and are correctly
classified Southern Europe. Only unambiguous ports (`BCN`, `CIV`, `LIS`) are in
the fallback map. Tests pin every region code, the port fallback, and the
precedence between them.

An unknown `regionCode` yields a NULL region and is logged to
`unmapped_labels`, never guessed. That is how `CS` (Southern Caribbean) was
found after the first run and added.

## Cross-line comparison and granularity

Sources do not resolve equally, and the panel does not pretend otherwise:

| | NCL | Carnival |
|---|---|---|
| finest granularity | `category` | `rate_code` |
| `vendor_category_code` | NULL | populated |
| `rate_code` | NULL | populated |
| availability states | available / limited / sold_out | available / sold_out |
| tax amount | never published | never (always 0.0) |
| promo detail | structured `offerGroups[]` | none |

`panel/sources/capabilities.py` declares this and enforces it. The failure it
exists to prevent is a cross-line comparison silently run at a resolution only
one side has — comparing Carnival's real sub-categories against NCL's NULLs
would return a confident, meaningless number.

```python
lines = ["ncl", "carnival"]
level = require_granularity(lines, shared_granularity(lines))  # -> "category"
column = comparison_column(level)                              # -> "cabin_category"
```

`shared_granularity(["ncl", "carnival"])` returns `category`;
`require_granularity(["ncl", "carnival"], "subcategory")` raises
`GranularityError` naming the source that cannot support it. **`peer_gap()` and
every other cross-line function must route through these** — comparisons run at
category level only. Carnival-only analysis may legitimately use the finer
levels.

## Persistence and scheduling

CI runners are ephemeral, so each run must write somewhere durable. The panel is
**not** stored as a committed SQLite file: SQLite is binary, git stores a full
copy per commit, ~37,800 rows/week compounds to GBs of repo growth within a
year, and two concurrent runs cannot merge a binary file.

Instead each run writes an immutable gzipped JSONL file at a path unique to
(date, tier, line):

```
data/observations/2027/03/2027-03-08__weekly-full__ncl.jsonl.gz
data/observations/promos.jsonl.gz      # bodies, deduped by hash
```

Concurrent weekly and daily runs never touch the same path, so they cannot
conflict. SQLite is a derived artifact, rebuilt on demand:

```bash
python -m panel.export export          # after a collection run
python -m panel.export rebuild         # reconstruct data/panel.sqlite
```

The round trip is verified lossless field-by-field, including the exact sum of
all prices. Measured cost: **26 bytes/observation gzipped, ~950 KB/week,
~68 MB/year** - comfortable for git indefinitely.

**Promo bodies are stored once.** `promo_text` used to sit on every observation:
289 rows carried ~5,900 bytes each for only 24 distinct offer sets, 38% of the
database. Bodies now live in a `promos` table keyed by `promo_hash`, read back
via `Store.promo_text(hash)`. That alone took the panel from 1,597 to 901
bytes/row, and the saving grows with NCL volume since most NCL rows carry promos.

### Schedule

`.github/workflows/collect.yml`

| Tier | Cron (UTC) | Scope |
|---|---|---|
| `weekly-full` | `10 6 * * 1` (Mondays) | all lines, all regions, Oct 2026 - Aug 2027 |
| `daily-marker` | `40 6 * * *` (daily) | near-term cohort + earnings window |

Each run: tests -> **confirm US market** -> rebuild from JSONL history ->
collect -> export -> commit. Rebuilding first is what makes resume and
idempotency work against real history rather than an empty database. The US
check runs before collection so a non-US egress fails the job instead of
writing a CAD panel. Raw responses upload as a 14-day artifact rather than
being committed.

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

### 1. US egress - RESOLVED

The spec calls for US site, USD only. A developer machine in Toronto is served
CAD, and no in-band override exists: 15 attempts (query params `currency`,
`market`, `country`, `locale`, `site`; headers `x-market`, `x-currency`,
`x-country`, `Referer`; path prefixes `/us/en/`, `/en/us/`) all returned CAD or
404. Akamai EdgeScape resolves market from client IP at the edge.

**A US-hosted GitHub Actions runner resolves correctly.** Confirmed on Azure
`westus2` (13.77.158.3, Moses Lake WA): `currencyCode == "USD"` on both the
search and sailings endpoints, every pricing cell. No proxy needed, and none is
used. Run `.github/workflows/verify-us-market.yml` to re-confirm.

Rows are stamped with the market actually served (`market`, `currency`), so a
CAD-served run can never be mistaken for a USD panel.

### 1b. NCL publishes no tax amount - by design, not by market

An earlier version of this README claimed the US market exposes
`taxesAndFees.amount` and CAD omits it. **That was wrong.** The evidence was a
hardcoded literal in NCL's JS bundle, not a live response. Verified since:

- `taxesAndFees` appears on **0 of 1,276** archived `pricingStateRooms` rows,
  and no key containing "tax" or "fee" exists anywhere in those payloads.
- It is equally absent from `/api/vacations/search/{code}` and
  `/api/vacations/events/{id}/package/{id}`.
- The itinerary-level `taxesAndFees` on the search endpoint is `{"text": ""}`
  on the **US** market as well as CAD - a vestigial field.

NCL's own `/api/vacations/disclaimers` settles the basis:

> "Fares shown are in US dollars and are per person, based on double occupancy
> ... Government taxes, fees, port expenses, and fuel supplement (where
> applicable) **are additional**."

So the published fare is **tax-exclusive**. `price_total` and `price_pppn` are
fare-only and the "never fold taxes into price" rule holds. `taxes_fees` is
NULL for NCL because the public API does not publish the amount - a missing
column, not a contaminated price. The collector still reads the field
defensively, so the panel would pick it up for free if NCL ever sends it.

### 2. Carnival tax amount

Carnival's price is tax-exclusive — its own itinerary page says "Taxes, fees,
and port expenses are an additional {taxesAndFees} per person" — so the basis
matches NCL and `peer_gap` is comparable. But the search endpoint returns
`taxesAndFees: 0.0` in every sampled cell, so `taxes_fees` is NULL for Carnival.
A zero is stored as NULL rather than a genuine zero-tax sailing. The amount may
be available from the itinerary or booking endpoint; one probe would settle it.

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

### 4. daily-marker markers -- selected, with a known limit

26 markers are live in `config/panel.yaml`: 20 NCL + 6 Carnival, all four cabin
categories, 112 near-term departures and 39 inside the earnings window.

The sampling frame is `data/sail_dates.json`, the calendar written by
`scripts/probe_sail_dates.py`.

Originally this was because the panel could not see the near term at all: the
weekly window was Jan-Aug 2027, NCL itinerary codes are season-specific, and of
the 152 NCL itineraries sailing Oct-Dec 2026 only 35 appeared anywhere in that
panel -- 1 of 15 in the Mediterranean. Picking from the panel produced a list
where 14 of 24 markers had no near-term departure at all and would have
collected nothing, every day, silently.

Widening weekly-full to start 2026-10-01 removes most of that gap: the panel
now covers the near-term window directly. The calendar is still the frame,
because it carries two things the panel does not -- sail dates beyond the
collection window (so a marker chosen today can be checked for departures after
it), and the `is_package` product flag per itinerary. Re-select with:

```bash
python scripts/probe_sail_dates.py     # refresh the calendar (~310 requests)
python scripts/pick_markers.py         # propose
python scripts/pick_markers.py --write # write into config/panel.yaml
```

Known limit: the Southern Europe markers go dark after early November when the
Mediterranean season ends. That is deployment, not a collection failure.

### 5. Carnival was collected at one page per destination

The 2026-09-15 weekly-full run passed `limit_pages: 1`, so Carnival landed 46
itineraries against NCL's 195, and its Southern Europe coverage is 10
itineraries. Carnival markers are provisional until a full run. `collect.yml`
now takes a `line` input, so this backfills without re-collecting NCL:
Actions -> Collect panel -> Run workflow -> tier `weekly-full`, line `carnival`.

## Getting raw rows out

```bash
python -m panel.export csv --date 2026-09-16
python -m panel.export csv --date 2026-09-16 --region "Southern Europe"        --line "Norwegian Cruise Line" --product cruise_only
python -m panel.export csv --region Alaska --with-promos        --columns line,ship,sail_date,nights,cabin_category,price_pppn
```

Filters: `--tier --date --line --region --category --product --sail-from
--sail-to --columns --with-promos`. `--line` / `--region` / `--category` repeat
and OR together. `--product cruise_only` applies the same strict rule as the
analysis module: `is_package = 0`, never "not 1", so an unclassified row is
excluded rather than assumed to be a cruise.

The CSV is deliberately plain -- a bare header row, UTF-8 BOM so Excel reads it
without a text-import dance, and no comment preamble. Provenance goes to a
`<name>.meta.json` sidecar recording the filters, row count, columns and exact
SQL, because a filtered slice detached from what produced it is how a regional
subset ends up quoted as though it were the whole panel. `--no-meta` skips it.

## Running an analysis

```bash
python -m panel.analysis availability    --tier weekly-full
python -m panel.analysis peer-gap        --tier weekly-full
python -m panel.analysis cohort-index    --tier weekly-full
python -m panel.analysis depletion       --tier daily-marker
python -m panel.analysis promo-diff      --tier weekly-full
python -m panel.analysis earnings-window --tier daily-marker
```

Each prints its basis and caveats above the table; `--json` emits the same
structure for downstream use. A function that cannot be computed on the data
available says so -- `depletion_rate` with one collection date returns
`NOT COMPUTABLE` -- rather than returning a fabricated figure.

## Every run ends in a workbook

The analysis suite is not something to remember to run. Every scheduled
collection finishes by running all six analyses across every configured region
and committing the result next to the JSONL it was computed from, in the same
commit:

    reports/2026-09-16__weekly-full.xlsx
    reports/2026-09-16__weekly-full.manifest.json

An `Index` sheet carries the provenance (row counts, collection dates present,
currencies, markets, sail window), a directory of the six sheets, region
coverage **including regions that returned nothing**, and the scope
comparability table described below. Then one sheet per analysis, each with its
basis line and caveats above a frozen, auto-filtered table.

By hand:

```bash
python -m panel.report --tier weekly-full
python -m panel.report --tier daily-marker --scrape-date 2026-11-04
```

**Only half the workbook is about the date in its name.** The three
cross-sectional analyses are pinned to that scrape date; the three time-series
analyses span every collection date in the tier, because that is what makes
them series. Every sheet states which it is, and the Index says so in a warning
row, because a file called `2026-09-16__weekly-full.xlsx` otherwise invites the
reading that all of it describes 16 September.

### Reports are derived, so they may be regenerated -- but never silently

`data/observations/*.jsonl.gz` is the observed record and is immutable. A
report is a pure function of that record plus this code, so regenerating it is
legitimate: a same-day single-line backfill, or a corrected analysis, genuinely
*should* move the numbers. The rule is that the movement has to be visible.

- The workbook is rewritten only when its **content** changes, compared through
  a digest of the result rows rather than of the file bytes. An XLSX is a zip
  whose bytes differ on every write, so a byte comparison would make every
  rerun a diff and make a real change indistinguishable from noise.
- When content does change, the per-sheet row-count deltas are appended to
  `reports/REGENERATIONS.jsonl` with the reason and the before/after digests.
  A future reader hitting a binary diff in `git log` can look up what moved.

### A time series across a change in scope is not a time series

`scope_drift` compares each pair of adjacent collection dates on what the
collector actually took: sail window, region set, line set, and sailing count.
Where those differ, every slope and index spanning the pair is measuring the
change to the collector as well as the market, and the workbook says so on the
sheets it invalidates rather than in a footnote.

This is live right now. Between 2026-09-15 and 2026-09-16 the weekly sweep
gained six months of near-term sailings, a fixed Carnival destination sweep and
the Transatlantic bucket -- sailings went 887 to 1,934. The Cohort index,
Depletion and Promo diff sheets are therefore stamped
`COLLECTION SCOPE CHANGED BETWEEN DATES` and carry no interpretable movement
until a run of stable-scope dates accumulates. The cross-sectional sheets are
unaffected: one date is one date.

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
