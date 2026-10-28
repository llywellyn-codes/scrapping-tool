PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,          -- e.g. "FL DBPR licensee file", "Google Places text search"
    source_url TEXT,
    terms_note TEXT,                    -- what the provider's terms allow
    export_permitted INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_created INTEGER NOT NULL DEFAULT 0,
    rows_merged INTEGER NOT NULL DEFAULT 0,
    rows_suppressed INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    email TEXT,
    website TEXT,
    street TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    google_place_id TEXT UNIQUE,
    osm_id TEXT UNIQUE,
    -- normalized keys for duplicate detection
    name_key TEXT,
    phone_key TEXT,
    domain_key TEXT,
    primary_source TEXT NOT NULL,
    field_sources TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    last_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_businesses_phone_key ON businesses(phone_key);
CREATE INDEX IF NOT EXISTS idx_businesses_domain_key ON businesses(domain_key);
CREATE INDEX IF NOT EXISTS idx_businesses_name_key ON businesses(name_key);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    signal_key TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('met', 'not_met', 'unknown')),
    evidence_type TEXT NOT NULL CHECK (evidence_type IN ('observed', 'inferred', 'confirmed_failure', 'unknown')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    source_type TEXT NOT NULL,
    source_url TEXT,
    observation TEXT,
    observed_at TEXT,
    test_name TEXT,
    test_result TEXT,
    expires_at TEXT,
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_business ON findings(business_id, signal_key);

CREATE TABLE IF NOT EXISTS outreach (
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',
    last_contact_date TEXT,
    response TEXT,
    opted_out INTEGER NOT NULL DEFAULT 0,
    opted_out_at TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL
);

-- Opt-outs outlive the prospect record so a re-import cannot resurrect them.
CREATE TABLE IF NOT EXISTS suppression (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('phone', 'domain', 'email', 'google_place_id', 'osm_id')),
    value_key TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (kind, value_key)
);

CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail TEXT
);

-- Latest website check per prospect. "checked_at" is the last completed check (ok or
-- blocked); a later failed attempt does not erase it.
CREATE TABLE IF NOT EXISTS website_checks (
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('ok', 'blocked', 'failed', 'no_website')),
    checked_at TEXT,
    last_attempt_at TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);

-- Bulk website check runs. Each item is saved as soon as it finishes.
CREATE TABLE IF NOT EXISTS check_jobs (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('unevaluated', 'selected')),
    status TEXT NOT NULL CHECK (status IN ('running', 'stopping', 'stopped', 'interrupted', 'completed')),
    batch_size INTEGER NOT NULL,
    force INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    heartbeat_at TEXT,
    finished_at TEXT,
    remaining_after INTEGER,
    extra TEXT NOT NULL DEFAULT '{}'      -- JSON: prospects left out and why, selections over the batch size
);
-- At most one running batch at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_check_job
    ON check_jobs ((status IN ('running', 'stopping'))) WHERE status IN ('running', 'stopping');

CREATE TABLE IF NOT EXISTS check_job_items (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES check_jobs(id) ON DELETE CASCADE,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('queued', 'processing', 'completed', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    detail TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (job_id, business_id)
);
CREATE INDEX IF NOT EXISTS idx_check_job_items_state ON check_job_items(job_id, state);
