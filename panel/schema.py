"""SQLite schema.

One row per observation: line x sailing x cabin subcategory x market x scrape day.

The natural key deliberately truncates scrape_ts_utc to a date so that re-running
a tier on the same day updates rather than duplicates.
"""

SCHEMA_VERSION = 4

DDL = """
CREATE TABLE IF NOT EXISTS observations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_ts_utc          TEXT    NOT NULL,
    scrape_date            TEXT    NOT NULL,   -- UTC date, part of the natural key
    line                   TEXT    NOT NULL,
    brand                  TEXT,
    ship                   TEXT,
    ship_code              TEXT,
    sailing_id             TEXT    NOT NULL,
    itinerary_code         TEXT,
    package_id             TEXT,
    sail_date              TEXT,
    return_date            TEXT,
    nights                 INTEGER,            -- CRUISE nights, always

    -- Land+cruise packages (NCL "cruisetours": Denali, London/Reykjavik tours)
    -- are a different product sold at a package price. NCL stamps them with the
    -- cruise segment length, so a naive price/nights overstates the nightly
    -- cruise fare by roughly the ratio of package days to cruise nights, and
    -- compares a bundled land tour against a peer's cruise-only fare. Captured
    -- so analysis can separate the products instead of averaging across them.
    is_package             INTEGER,            -- 1 = land+cruise package, NULL = unknown
    itinerary_nights       INTEGER,            -- total package length where published
    itinerary_name         TEXT,
    embark_port            TEXT,
    disembark_port         TEXT,
    region                 TEXT,
    market                 TEXT    NOT NULL,
    currency               TEXT,

    cabin_category         TEXT,               -- inside/oceanview/balcony/suite, NULL if unmapped
    cabin_subcategory      TEXT    NOT NULL,   -- raw vendor label, stable per sailing

    -- Finer granularity, only where a source actually exposes it. NULL elsewhere;
    -- never synthesised, so cross-line analysis can tell real resolution from
    -- padding. See panel/sources/capabilities.py.
    vendor_category_code   TEXT,               -- e.g. Carnival 8A/GS/6K. NULL for NCL.
    rate_code              TEXT,               -- e.g. Carnival OB7/PSV/OTR. NULL for NCL.
    offer_id               TEXT,               -- vendor offer id where exposed

    price_total            REAL,               -- 2 pax, taxes/fees EXCLUDED
    price_pppn             REAL,               -- per person per night, taxes/fees EXCLUDED
    price_per_person       REAL,               -- raw per-person voyage fare as published
    price_basis            TEXT,               -- which vendor field price_per_person came from
    taxes_fees             REAL,               -- captured SEPARATELY, never folded into price
    taxes_fees_text        TEXT,

    is_guarantee           INTEGER,            -- NULL where the source does not expose it
    availability_status    TEXT,               -- available / limited / sold_out / unknown
    availability_status_raw TEXT,              -- verbatim vendor status, never lossy
    units_remaining        INTEGER,            -- NULL unless the source exposes a count

    -- Promo bodies live in `promos`, keyed by this hash. Storing the verbatim
    -- payload per observation duplicated ~6 KB across thousands of rows for a
    -- couple of dozen distinct offers; the hash is the join key and the body is
    -- stored once. See Store.promo_text().
    promo_hash             TEXT,               -- stable hash for week-over-week diffing

    tier                   TEXT    NOT NULL,   -- 'weekly-full' | 'daily-marker'
    source_url             TEXT,
    raw_response_path      TEXT,

    UNIQUE (line, sailing_id, cabin_subcategory, market, scrape_date)
);

CREATE INDEX IF NOT EXISTS ix_obs_cohort
    ON observations (line, region, sail_date, cabin_category);
CREATE INDEX IF NOT EXISTS ix_obs_sailing
    ON observations (line, sailing_id, scrape_date);
CREATE INDEX IF NOT EXISTS ix_obs_tier_date
    ON observations (tier, scrape_date);
CREATE INDEX IF NOT EXISTS ix_obs_itinerary
    ON observations (line, itinerary_code, scrape_date);

CREATE TABLE IF NOT EXISTS collection_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    NOT NULL,
    run_ts              TEXT    NOT NULL,
    finished_ts         TEXT,
    tier                TEXT    NOT NULL,
    line                TEXT    NOT NULL,
    sailings_attempted  INTEGER NOT NULL DEFAULT 0,
    sailings_captured   INTEGER NOT NULL DEFAULT 0,
    observations_written INTEGER NOT NULL DEFAULT 0,
    errors_json         TEXT
);

CREATE INDEX IF NOT EXISTS ix_log_run ON collection_log (run_id);

-- Resumability: which itinerary codes are already done for (tier, line, day).
CREATE TABLE IF NOT EXISTS run_progress (
    tier            TEXT NOT NULL,
    line            TEXT NOT NULL,
    scrape_date     TEXT NOT NULL,
    itinerary_code  TEXT NOT NULL,
    completed_ts    TEXT NOT NULL,
    n_observations  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tier, line, scrape_date, itinerary_code)
);

-- Vendor labels we could not map. Logged, never guessed at.
CREATE TABLE IF NOT EXISTS unmapped_labels (
    line        TEXT NOT NULL,
    raw_label   TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    n_seen      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (line, raw_label)
);

-- Promo bodies, stored once per distinct offer set rather than per observation.
CREATE TABLE IF NOT EXISTS promos (
    promo_hash  TEXT PRIMARY KEY,
    promo_text  TEXT NOT NULL,   -- RAW offer payload, verbatim
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    n_seen      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_obs_promo ON observations (promo_hash);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Additive migrations, applied in order for databases created before the
# current SCHEMA_VERSION. Each entry is (version_introduced, table, column, ddl).
MIGRATIONS = [
    (2, "observations", "vendor_category_code", "TEXT"),
    (2, "observations", "rate_code", "TEXT"),
    (2, "observations", "offer_id", "TEXT"),
    (4, "observations", "is_package", "INTEGER"),
    (4, "observations", "itinerary_nights", "INTEGER"),
]
